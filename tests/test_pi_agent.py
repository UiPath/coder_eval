"""Tests for the Pi agent harness.

The CLI is never invoked: ``asyncio.create_subprocess_exec`` is patched with a
fake process that replays a newline-delimited JSON event stream, so the whole
reduction path (nd-JSON -> standardized events -> ``TurnRecord``) is exercised
offline and without credentials.

The happy fixture (``tests/fixtures/pi_happy_stream.jsonl``) is a BYTE-REAL
capture from ``pi -p --mode json`` 0.84.4 (write hello.txt + read it back, 3
agent-loop turns). The expected token/cost totals below are derived from that
fixture's per-``turn_end`` usages, summed — not free-standing magic numbers.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coder_eval.agents.pi_agent import PiAgent, _PiDecoder, _result_text
from coder_eval.errors.agent import format_timeout_reason
from coder_eval.models import AgentKind, AssistantMessage, PiAgentConfig
from coder_eval.orchestration.plugin_staging import stage_plugins
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.emitter import TurnEmitter, TurnOutcome
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StopReason,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.testing import Replay, ScriptedClock, Tick, assert_identity_closes, assert_stream_balanced, replay
from coder_eval.timing import TurnClock
from tests._bracket_clock import AnchoredClock, assert_bracket_on_the_clock, assert_overhead_is_measured
from tests._fixtures.golden_streams.pi_fixtures import (
    EXPECTED_CACHE_READ,
    EXPECTED_COST,
    EXPECTED_INPUT,
    EXPECTED_OUTPUT,
    HAPPY_STREAM,
    _ExplodingRunningProcess,
    _FakeProcess,
    _RunningProcess,
    _tool_end,
    _tool_start,
    _turn_end,
    _turn_start,
)


@pytest.fixture
def patch_exec(monkeypatch: pytest.MonkeyPatch):
    """Patch subprocess spawn; return a dict capturing the argv used."""
    captured: dict[str, Any] = {"killpg": []}

    def _install(proc: _FakeProcess) -> dict[str, Any]:
        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            proc.stderr = proc  # type: ignore[assignment]
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/pi")
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: captured["killpg"].append((pgid, sig)), raising=False)
        return captured

    return _install


async def _run(agent: PiAgent, tmp_path: Any, prompt: str = "do the thing", *, iteration: int = 1, **kwargs: Any):
    await agent.start(str(tmp_path))
    return await agent.communicate(prompt, iteration=iteration, **kwargs)


def _agent(**overrides: Any) -> PiAgent:
    config = PiAgentConfig(type="pi", **{"model": "openrouter/moonshotai/kimi-k3", **overrides})
    return PiAgent(config, task_id="t1")


class _EventRecorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


_SPAN_BASE = datetime(2026, 3, 1, 9, 0, 0)


def _ms(at_ms: float) -> datetime:
    return _SPAN_BASE + timedelta(milliseconds=at_ms)


def _event(line: str) -> dict[str, Any]:
    return json.loads(line)


def _start() -> dict[str, Any]:
    return {"type": "turn_start"}


def _end(*, inp: int = 10, out: int = 5) -> dict[str, Any]:
    return {
        "type": "turn_end",
        "message": {"role": "assistant", "usage": {"input": inp, "output": out}, "stopReason": "stop"},
    }


def _open(call_id: str) -> dict[str, Any]:
    return {"type": "tool_execution_start", "toolCallId": call_id, "toolName": "bash", "args": {}}


def _close(call_id: str) -> dict[str, Any]:
    return {"type": "tool_execution_end", "toolCallId": call_id, "result": "ok"}


def _replay(
    stream: list[Any], *, status: AgentEndStatus = AgentEndStatus.COMPLETED, reason: str | None = None
) -> tuple[Replay, _PiDecoder]:
    """Drive a `_PiDecoder` through `coder_eval.testing.replay` from `_SPAN_BASE`; return the decoder too."""
    decoders: list[_PiDecoder] = []

    def end(decoder: _PiDecoder) -> TurnOutcome:
        decoders.append(decoder)
        return decoder.end(status, reason=reason)

    return replay(stream, _PiDecoder, clock=ScriptedClock(_SPAN_BASE), end=end), decoders[0]


def _assistants(result: Replay) -> list[AssistantMessage]:
    return [m for m in result.record.messages if isinstance(m, AssistantMessage)]


class TestHappyPath:
    async def test_builds_turn_record(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record

        assert record.crashed is False
        # 3 turn_start steps in the fixture (write, read, summarize).
        assert record.assistant_turn_count == 3
        assert record.model_used == "openrouter/moonshotai/kimi-k3"
        assert "hi" in record.agent_output

    async def test_token_buckets_sum_across_turns(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == EXPECTED_INPUT
        assert usage.output_tokens == EXPECTED_OUTPUT
        assert usage.cache_read_input_tokens == EXPECTED_CACHE_READ
        assert usage.cache_creation_input_tokens == 0
        assert usage.total_cost_usd == pytest.approx(EXPECTED_COST)

    async def test_reconciliation_invariant(self, patch_exec, tmp_path):
        """Summing the four buckets across messages must equal token_usage exactly."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record

        usage = record.token_usage
        assert usage is not None
        assert sum(m.input_tokens for m in record.messages) == usage.uncached_input_tokens
        assert sum(m.output_tokens for m in record.messages) == usage.output_tokens
        assert sum(m.cache_read_tokens for m in record.messages) == usage.cache_read_input_tokens

    async def test_tool_calls_captured(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record

        # write + read, normalized to the canonical vocabulary.
        assert [c.tool_name for c in record.commands] == ["Write", "Read"]
        write = record.commands[0]
        assert write.tool_id == "write:0"
        assert write.result_status == "success"
        # `path` renamed to Claude's `file_path`.
        assert write.parameters == {"file_path": "hello.txt", "content": "hi"}
        assert write.result_summary == "Successfully wrote 2 bytes to hello.txt"

    async def test_messages_attributed_to_turns(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record

        assistants = [m for m in record.messages if isinstance(m, AssistantMessage)]
        assert len(assistants) == 3
        assert assistants[0].tool_use_ids == ["write:0"]
        assert assistants[1].tool_use_ids == ["read:1"]
        assert assistants[2].tool_use_ids == []  # final summary turn

    async def test_event_order_is_balanced(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        recorder = _EventRecorder()
        await _run(_agent(), tmp_path, stream_callback=recorder)

        seen = recorder.events
        assert len([e for e in seen if isinstance(e, AgentStartEvent)]) == 1
        assert len([e for e in seen if isinstance(e, AgentEndEvent)]) == 1
        starts = len([e for e in seen if isinstance(e, TurnStartEvent)])
        ends = len([e for e in seen if isinstance(e, TurnEndEvent)])
        assert starts == ends == 3
        tstarts = len([e for e in seen if isinstance(e, ToolStartEvent)])
        tends = len([e for e in seen if isinstance(e, ToolEndEvent)])
        assert tstarts == tends == 2


class TestResultTextFlatten:
    def test_flattens_content_list(self):
        assert _result_text({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "ab"

    def test_none_stays_none(self):
        assert _result_text(None) is None

    def test_other_shapes_stringify(self):
        assert _result_text(42) == "42"


class TestToolNormalization:
    async def test_bash_maps_to_canonical(self, patch_exec, tmp_path):
        stream = [
            _turn_start(),
            _tool_start("bash:0", "bash", {"command": "pytest -q"}),
            _tool_end("bash:0", "bash", "ok"),
            _turn_end(inp=10, out=5),
            json.dumps({"type": "agent_settled"}),
        ]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record
        assert record.commands[0].tool_name == "Bash"
        assert record.commands[0].parameters == {"command": "pytest -q"}

    async def test_find_maps_to_glob(self, patch_exec, tmp_path):
        # Pi's search-by-pattern tool is `find` (not `glob`); it must normalize to
        # the canonical `Glob` so command_executed / commands_efficiency criteria
        # written against `Glob` score on a Pi run that searched.
        stream = [
            _turn_start(),
            _tool_start("f:0", "find", {"pattern": "**/*.py"}),
            _tool_end("f:0", "find", "ok"),
            _turn_end(inp=10, out=5),
            json.dumps({"type": "agent_settled"}),
        ]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record
        assert record.commands[0].tool_name == "Glob"

    async def test_unknown_tool_passes_through(self, patch_exec, tmp_path):
        stream = [
            _turn_start(),
            _tool_start("x:0", "some_new_tool", {"whatever": 1}),
            _tool_end("x:0", "some_new_tool", "ok"),
            _turn_end(inp=10, out=5),
        ]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record
        assert record.commands[0].tool_name == "some_new_tool"
        assert record.commands[0].parameters == {"whatever": 1}

    async def test_edit_arg_keys_map_to_canonical(self, patch_exec, tmp_path):
        stream = [
            _turn_start(),
            _tool_start("e:0", "edit", {"path": "a.py", "oldString": "a", "newString": "b", "replaceAll": True}),
            _tool_end("e:0", "edit", "ok"),
            _turn_end(inp=10, out=5),
        ]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record
        assert record.commands[0].parameters == {
            "file_path": "a.py",
            "old_string": "a",
            "new_string": "b",
            "replace_all": True,
        }


class TestArgvConstruction:
    async def test_base_flags_and_prompt(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)

        argv = captured["argv"]
        assert argv[:6] == ["pi", "-p", "--mode", "json", "--no-context-files", "--no-approve"]
        assert argv[argv.index("--model") + 1] == "openrouter/moonshotai/kimi-k3"
        assert argv[argv.index("--thinking") + 1] == "medium"
        assert "--session-dir" in argv and "--session-id" in argv
        assert "--no-session" not in argv
        assert argv[-2] == "--"
        assert argv[-1] == "do the thing"

    async def test_tool_flags_and_system_prompt_are_forwarded(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(allowed_tools=["Read", "Bash"], system_prompt="be terse"), tmp_path)
        argv = captured["argv"]
        assert argv[argv.index("--tools") + 1] == "bash,read"
        assert argv.index("--tools") > argv.index("--thinking")
        assert argv[argv.index("--append-system-prompt") + 1] == "be terse"

    async def test_user_input_is_a_post_dashdash_argv_element(self, patch_exec, tmp_path):
        """Shell-safety: the prompt is never interpolated into a shell string."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path, prompt="rm -rf / ; echo $(whoami)")
        argv = captured["argv"]
        assert argv[-1] == "rm -rf / ; echo $(whoami)"
        assert argv[argv.index("--") - 1] != "rm -rf / ; echo $(whoami)"

    async def test_explicit_line_limit_is_passed(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["limit"] > 64 * 1024

    async def test_the_cli_never_inherits_stdin(self, patch_exec, tmp_path):
        """Pi reads a non-TTY stdin to EOF before it emits; an inherited open stdin stalls the turn."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL


class TestToolFlags:
    def test_allowlist_maps_claude_names_to_pi_tools(self):
        assert _agent(allowed_tools=["Bash", "Read"])._tool_flags() == ["--tools", "bash,read"]

    def test_denylist_expands_to_every_equivalent(self):
        assert _agent(disallowed_tools=["Edit"])._tool_flags() == ["--exclude-tools", "edit,multiedit,patch"]

    def test_plan_denies_write_edit_and_bash(self):
        assert _agent(permission_mode="plan")._tool_flags() == [
            "--exclude-tools",
            "bash,edit,multiedit,patch,write",
        ]

    def test_allowlist_with_no_pi_equivalent_disables_every_tool(self):
        assert _agent(allowed_tools=["Skill"])._tool_flags() == ["--no-tools"]

    def test_deny_wins_over_allow_without_a_separate_exclude(self):
        assert _agent(allowed_tools=["Bash", "Read"], disallowed_tools=["Bash"])._tool_flags() == ["--tools", "read"]

    def test_plan_wins_over_an_allowed_bash(self):
        assert _agent(allowed_tools=["Bash", "Read"], permission_mode="plan")._tool_flags() == ["--tools", "read"]

    def test_no_fields_emit_no_flags(self):
        assert _agent()._tool_flags() == []

    def test_empty_allowlist_restricts_nothing(self):
        assert _agent(allowed_tools=[])._tool_flags() == []

    def test_tool_names_cover_the_canonical_vocabulary(self):
        from coder_eval.models import CANONICAL_TOOL_NAMES

        assert PiAgent.tool_names is not None
        assert set(PiAgent.tool_names.names) == CANONICAL_TOOL_NAMES
        assert PiAgent.tool_names.names["Edit"] == ("edit", "multiedit", "patch")
        assert PiAgent.tool_names.names["Skill"] == ()


class TestSessionContinuity:
    async def test_successive_calls_reuse_the_same_session(self, patch_exec, tmp_path):
        """Multi-turn / simulation stitching: both invocations carry the SAME id+dir."""
        captured1 = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await _run(agent, tmp_path)
        argv1 = captured1["argv"]
        sid1 = argv1[argv1.index("--session-id") + 1]
        sdir1 = argv1[argv1.index("--session-dir") + 1]

        captured2 = patch_exec(_FakeProcess(HAPPY_STREAM))
        await agent.communicate("follow up", iteration=2)
        argv2 = captured2["argv"]
        assert argv2[argv2.index("--session-id") + 1] == sid1
        assert argv2[argv2.index("--session-dir") + 1] == sdir1

    async def test_start_creates_and_stop_removes_the_session_dir(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await agent.start(str(tmp_path))
        session_dir = agent._session_dir
        assert session_dir is not None and Path(session_dir).is_dir()

        await agent.stop()
        assert not Path(session_dir).exists()
        assert agent._session_dir is None

    async def test_restart_does_not_leak_the_prior_session_dir(self, patch_exec, tmp_path):
        """Re-starting the same agent instance must remove the previous tempdir."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await agent.start(str(tmp_path))
        first = agent._session_dir
        assert first is not None and Path(first).is_dir()

        await agent.start(str(tmp_path))
        second = agent._session_dir
        assert second is not None and second != first
        assert not Path(first).exists()  # the first dir was cleaned up, not leaked
        await agent.stop()


class TestEnvironmentInfo:
    def test_carries_semantics_and_pi_fields(self):
        info = _agent(thinking_level="high").get_environment_info()
        assert info["system_prompt_semantics"] == "append"
        assert info["pi_model"] == "openrouter/moonshotai/kimi-k3"
        assert info["pi_thinking_level"] == "high"

    async def test_session_id_recorded_after_start(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        assert "pi_session_id" not in agent.get_environment_info()
        await agent.start(str(tmp_path))
        assert agent.get_environment_info()["pi_session_id"].startswith("coder-eval-t1-")

    async def test_dataset_row_task_id_is_sanitized_in_session_id(self, patch_exec, tmp_path):
        # Dataset-row tasks have path-shaped ids ("suite/row_1"); pi derives its
        # session file from --session-id under --session-dir, so a raw '/' would
        # resolve to a non-existent subdir and fail the row. The id must carry no
        # path separator.
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        config = PiAgentConfig(type="pi", model="openrouter/moonshotai/kimi-k3")
        agent = PiAgent(config, task_id="suite/row_1")
        await _run(agent, tmp_path)
        sid = captured["argv"][captured["argv"].index("--session-id") + 1]
        assert "/" not in sid and "\\" not in sid
        assert sid.startswith("coder-eval-suite_row_1-")


class TestSandboxEnvironment:
    async def test_prepends_mock_dirs_ahead_of_the_inherited_path(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", "/parent/bin")
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await agent.start(str(tmp_path), env_path_prepend=["/sandbox/mocks", "/sandbox/bins"])
        await agent.communicate("do the thing", iteration=1)

        assert captured["kwargs"]["env"]["PATH"] == os.pathsep.join(["/sandbox/mocks", "/sandbox/bins", "/parent/bin"])

    async def test_host_environment_is_inherited_whole(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["env"]["OPENROUTER_API_KEY"] == "sk-test"


class TestAutoRetry:
    async def test_two_agent_cycles_reduce_to_one_agent_end(self, patch_exec, tmp_path):
        """Pi retries a transient error internally: agent_end(willRetry:true) then a
        clean second cycle then agent_settled. The reducer must finalize ONCE."""
        stream = [
            json.dumps({"type": "agent_start"}),
            _turn_start(),
            _turn_end(inp=100, out=10, cost=0.001),
            json.dumps({"type": "agent_end", "messages": [], "willRetry": True}),
            json.dumps({"type": "agent_start"}),
            _turn_start(),
            _tool_start("w:0", "write", {"path": "a.txt", "content": "x"}),
            _tool_end("w:0", "write", "ok"),
            _turn_end(inp=200, out=20, cost=0.002),
            json.dumps({"type": "agent_end", "messages": [], "willRetry": False}),
            json.dumps({"type": "agent_settled"}),
        ]
        patch_exec(_FakeProcess(stream))
        recorder = _EventRecorder()
        outcome = await _run(_agent(), tmp_path, stream_callback=recorder)
        record = outcome.record

        assert record.crashed is False
        assert len([e for e in recorder.events if isinstance(e, AgentEndEvent)]) == 1
        # Both cycles' usage merged (SUM): input 100+200, output 10+20.
        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 300
        assert record.token_usage.output_tokens == 30
        assert record.assistant_turn_count == 2


def _stop_after(calls: int, reason: StopReason):
    """A ``should_stop`` that returns ``reason`` from its ``calls``-th check on (one check per dispatched line)."""
    seen = 0

    def should_stop() -> StopReason | None:
        nonlocal seen
        seen += 1
        return reason if seen >= calls else None

    return should_stop


class TestCooperativeStop:
    def test_capability_flag_is_declared(self):
        assert PiAgent.contract.cooperative_stop is True

    async def test_early_criterion_ends_turn_stopped_early(self, patch_exec, tmp_path):
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        recorder = _EventRecorder()
        outcome = await _run(
            _agent(), tmp_path, should_stop=lambda: StopReason.EARLY_CRITERION, stream_callback=recorder
        )
        record = outcome.record

        assert record.crashed is False
        assert proc.terminated is True
        # The first check lands after the first streamed line (the `session`
        # header), before any turn completes.
        assert record.assistant_turn_count == 0
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert [e.status for e in ends] == [AgentEndStatus.STOPPED_EARLY]

    async def test_tool_call_cap_ends_turn_tool_calls_exhausted(self, patch_exec, tmp_path):
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        recorder = _EventRecorder()
        outcome = await _run(_agent(), tmp_path, should_stop=lambda: StopReason.TOOL_CALL_CAP, stream_callback=recorder)
        record = outcome.record

        assert proc.terminated is True
        assert record.crashed is False
        assert record.tool_calls_exhausted is True
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert [e.status for e in ends] == [AgentEndStatus.TOOL_CALLS_EXHAUSTED]

    async def test_token_budget_ends_turn_token_budget_exceeded(self, patch_exec, tmp_path):
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        recorder = _EventRecorder()
        outcome = await _run(_agent(), tmp_path, should_stop=lambda: StopReason.TOKEN_BUDGET, stream_callback=recorder)
        record = outcome.record

        assert proc.terminated is True
        assert record.crashed is False
        assert record.tool_calls_exhausted is False
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert [e.status for e in ends] == [AgentEndStatus.TOKEN_BUDGET_EXCEEDED]

    async def test_the_deciding_turn_is_kept_whole(self, patch_exec, tmp_path):
        """A stop that lands on turn 2's `turn_start` keeps turn 1 complete."""
        second_turn_start = [i for i, line in enumerate(HAPPY_STREAM) if json.loads(line)["type"] == "turn_start"][1]
        patch_exec(_RunningProcess(HAPPY_STREAM))
        outcome = await _run(
            _agent(), tmp_path, should_stop=_stop_after(second_turn_start + 1, StopReason.TOOL_CALL_CAP)
        )
        record = outcome.record

        assert record.tool_calls_exhausted is True
        assert len(record.commands) == 1  # turn 1's write
        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 406  # turn 1's input exactly
        assert usage.output_tokens == 77  # 69 + 8 reasoning

    async def test_an_intentional_stop_is_exempt_from_a_non_zero_exit(self, patch_exec, tmp_path):
        """Killing the CLI makes it exit non-zero; that must not crash an intentional stop."""
        patch_exec(_RunningProcess(HAPPY_STREAM, returncode=-15, stderr=b"terminated"))
        outcome = await _run(_agent(), tmp_path, should_stop=lambda: StopReason.TOOL_CALL_CAP)
        record = outcome.record
        assert record.crashed is False
        assert record.tool_calls_exhausted is True

    async def test_an_intentional_stop_is_exempt_from_no_recognized_events(self, patch_exec, tmp_path):
        """A stop can land before the first recognized event; that is not vocabulary drift."""
        patch_exec(_RunningProcess([json.dumps({"type": "not_a_pi_event"}), *HAPPY_STREAM]))
        outcome = await _run(_agent(), tmp_path, should_stop=lambda: StopReason.TOKEN_BUDGET)
        record = outcome.record
        assert record.crashed is False

    async def test_no_stop_is_uncapped(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        outcome = await _run(_agent(), tmp_path, should_stop=lambda: None)
        record = outcome.record
        assert record.tool_calls_exhausted is False
        assert record.assistant_turn_count == 3


class _HangingProcess(_FakeProcess):
    def __init__(self, lines: list[str], **kwargs: Any) -> None:
        super().__init__(lines, **kwargs)
        self._exited = asyncio.Event()

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await self._exited.wait()
        return b""

    async def read(self) -> bytes:
        await self._exited.wait()
        return self._stderr

    async def wait(self) -> int:
        await self._exited.wait()
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self._exited.set()


class TestTimeoutContract:
    async def test_deadline_returns_a_timeout_outcome_with_the_partial(self, patch_exec, tmp_path):
        proc = _HangingProcess([_turn_start()])
        patch_exec(proc)
        agent = _agent()
        recorder = _EventRecorder()
        ends_seen_at_kill: list[int] = []
        real_kill = agent.kill

        async def spy_kill() -> None:
            ends_seen_at_kill.append(len([e for e in recorder.events if isinstance(e, AgentEndEvent)]))
            await real_kill()

        agent.kill = spy_kill  # type: ignore[method-assign]

        outcome = await _run(agent, tmp_path, timeout=0.2, stream_callback=recorder)

        assert outcome.status is AgentEndStatus.TIMEOUT
        assert outcome.error == format_timeout_reason(0.2)
        partial = outcome.record
        assert partial is not None
        assert partial.crashed is True
        assert proc.terminated is True
        assert ends_seen_at_kill[:1] == [0], "the CLI is killed BEFORE the turn ends"
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].status is AgentEndStatus.TIMEOUT


class _EofButAliveProcess(_HangingProcess):
    """Stdout reaches EOF, but the process never exits until it is signalled."""

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class TestSettleWaitsForTheExit:
    async def test_no_exit_before_the_deadline_is_a_timeout(self, patch_exec, tmp_path):
        proc = _EofButAliveProcess([_turn_start()])
        patch_exec(proc)
        recorder = _EventRecorder()
        outcome = await _run(_agent(), tmp_path, timeout=0.3, stream_callback=recorder)
        assert outcome.status is AgentEndStatus.TIMEOUT
        assert proc.terminated is True
        assert [e.status for e in recorder.events if isinstance(e, AgentEndEvent)] == [AgentEndStatus.TIMEOUT]

    async def test_no_exit_without_a_deadline_is_a_crash(self, patch_exec, tmp_path, monkeypatch):
        from coder_eval.agents import pi_agent

        monkeypatch.setattr(pi_agent, "_TERM_GRACE_SECONDS", 0.1)
        proc = _EofButAliveProcess([_turn_start()])
        patch_exec(proc)
        recorder = _EventRecorder()
        outcome = await _run(_agent(), tmp_path, stream_callback=recorder)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "did not exit within" in outcome.error
        assert proc.terminated is True
        assert [e.status for e in recorder.events if isinstance(e, AgentEndEvent)] == [AgentEndStatus.CRASHED]


class _ExplodingProcess(_FakeProcess):
    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        raise ValueError("Separator is not found, and chunk exceed the limit")


class TestFailurePaths:
    async def test_nonzero_exit_crashes(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess([], returncode=1, stderr=b"boom: bad model"))
        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "boom: bad model" in outcome.error

    async def test_empty_clean_exit_crashes_on_no_recognized_events(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess([], returncode=0))
        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "no recognized events" in outcome.error

    async def test_drift_crash_names_the_unrecognized_types(self, patch_exec, tmp_path):
        """A clean exit whose events are all unrecognized (schema drift) crashes and
        names what it saw, for diagnosis."""
        stream = [
            json.dumps({"type": "some.new.event", "foo": 1}),
            json.dumps({"type": "another.unknown", "bar": 2}),
        ]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None
        assert "another.unknown, some.new.event" in outcome.error
        assert "no recognized events" in outcome.error

    async def test_stream_error_becomes_a_crash_with_the_crashed_partial(self, patch_exec, tmp_path):
        stream = [_turn_start(), _tool_start("w:0", "write", {"path": "a.txt", "content": "x"})]
        patch_exec(_ExplodingProcess(stream))
        agent = _agent()

        outcome = await _run(agent, tmp_path)

        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "Pi turn failed" in outcome.error
        partial = outcome.record
        assert partial is not None
        assert partial.crashed is True
        # The in-flight tool was force-closed rather than dropped.
        assert [c.result_status for c in partial.commands] == ["unknown"]

    async def test_spawn_failure_becomes_a_crash(self, monkeypatch, tmp_path):
        async def boom(*_argv: str, **_kwargs: Any):
            raise OSError("no fork for you")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/pi")

        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "no fork for you" in outcome.error

    async def test_malformed_line_is_skipped(self, patch_exec, tmp_path):
        stream = ["not json at all", *HAPPY_STREAM]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record
        assert record.crashed is False
        assert record.assistant_turn_count == 3

    async def test_missing_cli_is_actionable(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(RuntimeError, match="@earendil-works/pi-coding-agent"):
            await _agent().start(str(tmp_path))


class TestUnexpectedErrorContract:
    async def test_terminal_event_emitted_exactly_once_on_crash(self, patch_exec, tmp_path):
        patch_exec(_ExplodingProcess([_turn_start()]))
        recorder = _EventRecorder()

        outcome = await _run(_agent(), tmp_path, stream_callback=recorder)
        assert outcome.status is AgentEndStatus.CRASHED

        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].crashed is True
        assert ends[0].status is AgentEndStatus.CRASHED


class TestTurnEventsAreBalanced:
    @staticmethod
    def _pairs(recorder: _EventRecorder) -> tuple[int, int]:
        starts = len([e for e in recorder.events if isinstance(e, TurnStartEvent)])
        ends = len([e for e in recorder.events if isinstance(e, TurnEndEvent)])
        return starts, ends

    async def test_a_clean_turn_is_balanced(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        recorder = _EventRecorder()
        await _run(_agent(), tmp_path, stream_callback=recorder)
        assert self._pairs(recorder) == (3, 3)

    async def test_a_timeout_closes_the_open_turn(self, patch_exec, tmp_path):
        proc = _HangingProcess([_turn_start()])
        patch_exec(proc)
        recorder = _EventRecorder()

        outcome = await _run(_agent(), tmp_path, timeout=0.2, stream_callback=recorder)
        assert outcome.status is AgentEndStatus.TIMEOUT

        assert self._pairs(recorder) == (1, 1)
        end = next(e for e in recorder.events if isinstance(e, TurnEndEvent))
        assert end.status is TurnEndStatus.TIMEOUT


class TestFactoryContract:
    def test_route_accepted_positionally_and_by_keyword(self):
        config = PiAgentConfig(type="pi", model="openrouter/moonshotai/kimi-k3")
        assert PiAgent(config, route=None).route is None
        assert PiAgent(config, None).route is None

    def test_undeclared_kwarg_raises(self):
        config = PiAgentConfig(type="pi", model="openrouter/moonshotai/kimi-k3")
        with pytest.raises(TypeError):
            PiAgent(config, not_a_kwarg={"x": "y"})  # type: ignore[call-arg]


class TestRegistry:
    def test_create_agent_returns_pi_agent(self):
        from coder_eval.agents import AgentRegistry, create_agent, register_builtins

        register_builtins(AgentRegistry)
        agent = create_agent(AgentKind.PI, PiAgentConfig(type="pi"))
        assert isinstance(agent, PiAgent)

    def test_list_kinds_includes_pi(self):
        from coder_eval.agents import AgentRegistry, register_builtins

        register_builtins(AgentRegistry)
        assert "pi" in AgentRegistry.list_kinds()

    def test_parse_agent_config_dispatches_to_pi(self):
        from coder_eval.models import parse_agent_config

        cfg = parse_agent_config(type="pi", model="openrouter/moonshotai/kimi-k3")
        assert isinstance(cfg, PiAgentConfig)
        assert cfg.model == "openrouter/moonshotai/kimi-k3"


@pytest.mark.skipif(os.name != "posix", reason="process-group teardown is POSIX-only by design")
class TestProcessGroupTeardown:
    async def test_spawn_uses_its_own_session(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["start_new_session"] is (os.name == "posix")

    async def test_stop_sweeps_the_spawned_group(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await _run(agent, tmp_path)
        await agent.stop()
        assert (4242, signal.SIGKILL) in captured["killpg"]

    async def test_kill_sync_signals_pid_and_group(self, patch_exec, monkeypatch, tmp_path):
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
        captured = patch_exec(_HangingProcess([]))
        agent = _agent()
        await agent.start(str(tmp_path))
        proc = _HangingProcess([])
        agent._process = proc  # type: ignore[assignment]
        agent._spawned_pgids = [proc.pid]

        agent.kill_sync()

        assert (4242, signal.SIGKILL) in killed
        assert (4242, signal.SIGKILL) in captured["killpg"]


class TestToolFailureCapture:
    async def test_tool_error_is_captured(self, patch_exec, tmp_path):
        stream = [
            _turn_start(),
            _tool_start("b:0", "bash", {"command": "ls /root"}),
            _tool_end("b:0", "bash", "boom: command exploded", is_error=True),
            _turn_end(inp=10, out=5),
        ]
        patch_exec(_FakeProcess(stream))
        recorder = _EventRecorder()
        outcome = await _run(_agent(), tmp_path, stream_callback=recorder)
        record = outcome.record

        [cmd] = record.commands
        assert cmd.result_status == "error"
        assert cmd.error_message == "boom: command exploded"
        [end] = [e for e in recorder.events if isinstance(e, ToolEndEvent)]
        assert end.status is ToolEndStatus.ERROR

    async def test_permission_denial_gets_its_own_status(self, patch_exec, tmp_path):
        stream = [
            _turn_start(),
            _tool_start("b:0", "bash", {"command": "cat /etc/shadow"}),
            _tool_end("b:0", "bash", "Permission denied by policy", is_error=True),
            _turn_end(inp=10, out=5),
        ]
        patch_exec(_FakeProcess(stream))
        recorder = _EventRecorder()
        await _run(_agent(), tmp_path, stream_callback=recorder)
        [end] = [e for e in recorder.events if isinstance(e, ToolEndEvent)]
        assert end.status is ToolEndStatus.PERMISSION_DENIED

    def test_orphan_result_is_never_dropped(self):
        result, _ = _replay([_close("ghost")])

        [event] = [e for e in result.events if isinstance(e, ToolEndEvent)]
        assert event.tool.tool_id == "ghost"
        assert event.tool.tool_name == "unknown"
        assert [c.tool_id for c in result.record.commands] == ["ghost"]


class TestZeroUsageTurn:
    async def test_zero_usage_turn_is_scored_not_crashed(self, patch_exec, tmp_path):
        """A provider reporting no usage yields an all-zero token_usage; Pi scores
        it (no require_token_telemetry hard-fail) since its multi-provider surface
        makes that brittle."""
        stream = [
            _turn_start(),
            _turn_end(inp=0, out=0),
            json.dumps({"type": "agent_settled"}),
        ]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record
        assert record.crashed is False


# --- review fixes: token-drift warn (#1), dangling-turn close (#2), error capture (#3) ---


def _turn_end_error(msg: str = "404: blocked by guardrail") -> str:
    """A `turn_end` whose provider errored (the willRetry / terminal-failure shape)."""
    return json.dumps(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "usage": {"input": 5, "output": 2, "totalTokens": 7, "cost": {"total": 0.0}},
                "stopReason": "error",
                "errorMessage": msg,
            },
            "toolResults": [],
        }
    )


class TestTurnLifecycleAndTokenTelemetry:
    """Turn open/close balance, terminal-error surfacing, and the token-shape
    guards that keep a CLI-schema drift from silently zeroing tokens/cost."""

    async def test_dangling_turn_start_is_closed_on_next_turn_start(self, patch_exec, tmp_path):
        """#2: a turn_start with no turn_end (a mid-turn retry abort) must be closed
        when the next turn_start arrives, so TurnStart/TurnEnd stay balanced."""
        stream = [
            _turn_start(),  # turn 1 opens, then aborts mid-generation (no turn_end)
            _turn_start(),  # turn 2 opens -> must close turn 1 first
            _turn_end(inp=10, out=5),  # turn 2 ends cleanly
            json.dumps({"type": "agent_settled"}),
        ]
        patch_exec(_FakeProcess(stream))
        recorder = _EventRecorder()
        await _run(_agent(), tmp_path, stream_callback=recorder)
        starts = [e for e in recorder.events if isinstance(e, TurnStartEvent)]
        ends = [e for e in recorder.events if isinstance(e, TurnEndEvent)]
        assert len(starts) == len(ends) == 2  # balanced despite the aborted turn

    async def test_terminal_error_crashes_the_turn(self, patch_exec, tmp_path):
        """A terminal provider error (stopReason=error that survived pi's internal
        retries) crashes the turn so it books as ERROR (retryable / excluded from
        outcomes), not a clean COMPLETED FAILURE. Mirrors opencode_agent."""
        stream = [_turn_start(), _turn_end_error("404: blocked by guardrail"), json.dumps({"type": "agent_settled"})]
        patch_exec(_FakeProcess(stream))
        agent = _agent()
        outcome = await _run(agent, tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "blocked by guardrail" in outcome.error
        partial = outcome.record
        assert partial is not None
        assert partial.crashed is True

    async def test_a_stop_after_an_error_turn_finalizes_cleanly(self, patch_exec, tmp_path):
        """A stop landing right after an error turn_end (pi still retrying, so
        error_message is set but not yet cleared) must finalize with the stop's
        status — NOT crash on the stale error."""
        stream = [_turn_start(), _turn_end_error("transient 429"), _turn_start(), _turn_end(inp=1, out=1)]
        patch_exec(_RunningProcess(stream))
        outcome = await _run(_agent(), tmp_path, should_stop=_stop_after(3, StopReason.TOOL_CALL_CAP))
        record = outcome.record
        assert record.tool_calls_exhausted is True
        assert record.crashed is False

    def test_error_message_resets_on_a_recovered_turn(self):
        """#3: an intermediate error a later cycle recovers from must not leak into the result."""
        _, errored = _replay([_start(), _event(_turn_end_error("transient"))])
        assert errored.error == "transient"
        _, recovered = _replay([_start(), _event(_turn_end_error("transient")), _start(), _end()])
        assert recovered.error is None

    def test_dangling_turn_is_closed_crashed_at_the_next_turn_start(self):
        result, _ = _replay([_start(), _start(), _end()])

        turn_ends = [e for e in result.events if isinstance(e, TurnEndEvent)]
        assert [(e.turn_id, e.status) for e in turn_ends] == [
            ("turn_1", TurnEndStatus.CRASHED),
            ("turn_2", TurnEndStatus.COMPLETED),
        ]
        assert_stream_balanced(result.events)

    def test_a_duplicate_turn_end_publishes_no_second_turn_end_but_books_its_tokens(self):
        result, _ = _replay([_start(), _end(inp=10, out=5), _end(inp=7, out=3)])

        assert len([e for e in result.events if isinstance(e, TurnEndEvent)]) == 1
        assert len(_assistants(result)) == 2
        [agent_end] = [e for e in result.events if isinstance(e, AgentEndEvent)]
        assert agent_end.usage.uncached_input_tokens == 17
        assert agent_end.usage.output_tokens == 8
        assert_stream_balanced(result.events)

    def test_token_shape_warns_once_per_turn(self, caplog):
        no_usage = {"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}}
        bad_total = {
            "type": "turn_end",
            "message": {"role": "assistant", "usage": {"input": 1, "output": 1, "totalTokens": 99}},
        }

        def warnings() -> int:
            return len([r for r in caplog.records if "unexpected token accounting" in r.getMessage()])

        with caplog.at_level("WARNING"):
            _replay([_start(), no_usage, _start(), bad_total])
            assert warnings() == 1
            _replay([_start(), bad_total])
            assert warnings() == 2  # the flag is per turn, not per agent

    def test_total_tokens_mismatch_warns_at_the_decoder(self, caplog):
        bad_total = {
            "type": "turn_end",
            "message": {"role": "assistant", "usage": {"input": 10, "output": 5, "totalTokens": 999}},
        }
        with caplog.at_level("WARNING"):
            _, decoder = _replay([_start(), bad_total])
        assert decoder.warned_token_shape is True
        assert any("does not reconcile" in r.getMessage() for r in caplog.records)

    async def test_bad_token_bucket_warns_once(self, patch_exec, tmp_path, caplog):
        """#1: a bucket whose type drifted (here a dict) coerces to 0 but warns, once."""
        bad = json.dumps(
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "usage": {"input": {"nested": 1}, "output": 2, "cost": {"total": 0.0}},
                    "stopReason": "stop",
                },
                "toolResults": [],
            }
        )
        stream = [_turn_start(), bad, _turn_start(), bad, json.dumps({"type": "agent_settled"})]
        patch_exec(_FakeProcess(stream))
        with caplog.at_level("WARNING"):
            await _run(_agent(), tmp_path)
        warns = [r for r in caplog.records if "unexpected token accounting" in r.getMessage()]
        assert len(warns) == 1  # warn-once, not once per bucket/step

    async def test_missing_usage_object_warns(self, patch_exec, tmp_path, caplog):
        """#1: a completed turn_end with no `usage` object at all (a rename) warns."""
        no_usage = json.dumps(
            {"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}, "toolResults": []}
        )
        stream = [_turn_start(), no_usage, json.dumps({"type": "agent_settled"})]
        patch_exec(_FakeProcess(stream))
        with caplog.at_level("WARNING"):
            await _run(_agent(), tmp_path)
        assert any("no usage object" in r.getMessage() for r in caplog.records)

    async def test_all_zero_usage_object_warns(self, patch_exec, tmp_path, caplog):
        """A turn_end that DID carry a usage object but whose every bucket is 0 (the
        rename-each-key-to-0 drift shape) warns once — otherwise tokens and cost
        silently vanish and max_usd / max_total_tokens go blind."""
        zero = json.dumps(
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "stopReason": "stop",
                },
                "toolResults": [],
            }
        )
        stream = [_turn_start(), zero, json.dumps({"type": "agent_settled"})]
        patch_exec(_FakeProcess(stream))
        with caplog.at_level("WARNING"):
            outcome = await _run(_agent(), tmp_path)
            record = outcome.record
        assert record.crashed is False  # score, don't crash (documented Pi policy)
        assert any("all-zero token buckets" in r.getMessage() for r in caplog.records)

    async def test_total_tokens_mismatch_warns(self, patch_exec, tmp_path, caplog):
        """The stream's own totalTokens must reconcile with input+output+cacheRead+
        cacheWrite; a mismatch (a bucket renamed/moved under a CLI upgrade) warns
        once instead of silently mis-booking tokens/cost."""
        bad = json.dumps(
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    # buckets sum to 15, but the stream claims 999 — schema drift.
                    "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 999},
                    "stopReason": "stop",
                },
                "toolResults": [],
            }
        )
        stream = [_turn_start(), bad, json.dumps({"type": "agent_settled"})]
        patch_exec(_FakeProcess(stream))
        with caplog.at_level("WARNING"):
            outcome = await _run(_agent(), tmp_path)
            record = outcome.record
        assert record.crashed is False
        assert any("does not reconcile" in r.getMessage() for r in caplog.records)


class TestSkillInjection:
    """The staged plugin root reaches Pi as ``--skill <root>/skills``."""

    def _staged_root(self, tmp_path: Path) -> Path:
        skill = tmp_path / "authored" / "skills" / "demo-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n# Demo\n")
        return stage_plugins([{"type": "local", "path": str(tmp_path / "authored")}], tmp_path / "plugin_root").root

    async def test_staged_root_emits_skill_arg(self, patch_exec, tmp_path):
        root = self._staged_root(tmp_path)
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await agent.start(str(tmp_path), plugin_root=root)
        await agent.communicate("do the thing", iteration=1)
        argv = captured["argv"]
        assert argv[argv.index("--skill") + 1] == str(root / "skills")
        assert "pi_skill_paths" not in agent.get_environment_info()

    async def test_no_plugin_root_means_no_skill_arg(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert "--skill" not in captured["argv"]


class TestTurnAlwaysReapsTheCli:
    """No exit from ``communicate()`` may leave the CLI running. A crash is
    categorized AGENT_CRASH (max_retries=2), and the orchestrator only appends the
    crashed record — it never kills the agent. An abandoned CLI
    therefore means attempt 2 spawns a SECOND ``pi`` editing the very files the
    criteria are about to score. The graceful ``kill()`` covers the intentional cuts
    and the timeout; these pin the two paths that reach ``finally`` with a live child.
    """

    async def test_read_loop_crash_kills_the_cli(self, patch_exec, tmp_path):
        """A read-loop crash ends the turn as an outcome, and ``finally`` still reaps the live CLI."""
        proc = _ExplodingRunningProcess([_turn_start()])
        patch_exec(proc)
        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "Pi turn failed" in outcome.error
        assert proc.killed is True

    async def test_external_cancel_kills_the_cli(self, patch_exec, tmp_path):
        """Teardown must survive a CancelledError in flight (no await), or the child
        outlives an interrupted turn."""
        proc = _HangingProcess([_turn_start()])
        patch_exec(proc)
        agent = _agent()
        await agent.start(str(tmp_path))
        task = asyncio.ensure_future(agent.communicate("do the thing", iteration=1))
        await asyncio.sleep(0.05)  # let it spawn and read the first event
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task  # the await re-raises the cancellation; no value ever exists
        assert proc.killed is True

    async def test_a_clean_turn_kills_nothing(self, patch_exec, tmp_path):
        """The happy path is unchanged: the CLI exited, so the reaper is a no-op."""
        proc = _FakeProcess(HAPPY_STREAM)
        captured = patch_exec(proc)
        await _run(_agent(), tmp_path)
        assert proc.killed is False
        assert captured["killpg"] == []


class TestExternalCancel:
    async def test_cancel_ends_the_turn_and_reraises(self, patch_exec, tmp_path):
        """The watchdog's CancelledError must not swallow captured telemetry: the
        turn is ended, the terminal event says CRASHED, and the cancellation
        still propagates."""
        proc = _HangingProcess([_turn_start()])
        patch_exec(proc)
        agent = _agent()
        await agent.start(str(tmp_path))
        recorder = _EventRecorder()
        task = asyncio.ensure_future(agent.communicate("do the thing", iteration=1, stream_callback=recorder))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task  # the await re-raises the cancellation; no value ever exists
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].status is AgentEndStatus.CRASHED
        assert ends[0].crashed is True
        assert ends[0].crash_reason == "turn cancelled"
        assert proc.killed is True  # not abandoned mid-stream — see TestTurnAlwaysReapsTheCli


def _turn_end_no_cost(*, inp: int, out: int) -> str:
    """A `turn_end` whose usage object omits the `cost` key (provider/auth mode that
    reports no cost) — so `price_turn` must fall back to the rate card."""
    return json.dumps(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "usage": {"input": inp, "output": out, "totalTokens": inp + out},
                "stopReason": "stop",
            },
            "toolResults": [],
        }
    )


class TestCostFallsBackToTheRateCard:
    _SETTLED = json.dumps({"type": "agent_settled"})

    async def test_stream_cost_wins_when_reported(self, patch_exec, tmp_path):
        """The provider's own accounting beats a static headline rate."""
        stream = [_turn_start(), _turn_end(inp=1000, out=500, cost=0.5), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(0.5)

    async def test_missing_cost_is_priced_from_the_rate_card(self, patch_exec, tmp_path):
        """No `cost` key at all — without the fallback the turn books tokens with no money."""
        stream = [_turn_start(), _turn_end_no_cost(inp=1000, out=500), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record
        expected = calculate_cost("openrouter/moonshotai/kimi-k3", uncached_input_tokens=1000, output_tokens=500)
        assert expected is not None and expected > 0
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(expected)

    async def test_unpriced_model_reports_no_cost(self, patch_exec, tmp_path):
        """`None` (not 0.0) so "unpriceable" stays distinct from "ran for free"."""
        stream = [_turn_start(), _turn_end_no_cost(inp=10, out=5), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(model="nowhere/not-a-real-model"), tmp_path)
        record = outcome.record
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd is None

    async def test_zero_reported_cost_on_a_priced_model_uses_the_rate_card(self, patch_exec, tmp_path, caplog):
        """A reported $0 on a model the rate card prices is a subscription/registry
        gap, not a free run — latching on it would book real tokens with no money."""
        stream = [_turn_start(), _turn_end(inp=1000, out=500, cost=0.0), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        with caplog.at_level("DEBUG"):
            outcome = await _run(_agent(), tmp_path)
            record = outcome.record
        expected = calculate_cost("openrouter/moonshotai/kimi-k3", uncached_input_tokens=1000, output_tokens=500)
        assert expected is not None and expected > 0
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(expected)
        assert "using the rate card" in caplog.text

    async def test_zero_reported_cost_on_an_unpriced_model_stays_zero(self, patch_exec, tmp_path):
        """With no rate to fall back to, the stream's 0 is the best information we have."""
        stream = [_turn_start(), _turn_end(inp=10, out=5, cost=0.0), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        outcome = await _run(_agent(model="nowhere/not-a-real-model"), tmp_path)
        record = outcome.record
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == 0.0


class TestGenerationWindowExcludesToolExecution:
    """A tool running inside a turn is not model time — asserted where it is now DECIDED.

    The decoder no longer subtracts anything. It publishes the RAW window, and
    `timing.subtract_tool_time` takes the tool union back out of it
    once, for all five harnesses. So these cases replay the decoder through a
    real emitter and assert the PUBLISHED number — the one that reaches
    `task.json` — rather than an intermediate the decoder used to own.

    They are not duplicates of
    `tests/test_event_collector.py::TestSubtractToolTime`: those pin the
    arithmetic, these pin that THIS decoder hands the collector a window and a
    span set the arithmetic can be right about.
    """

    def _finish_turn(self, spans: list[tuple[float, float]], open_starts: tuple[float, ...] = ()) -> AssistantMessage:
        """Replay one 0 -> 1000 ms turn with tool calls at the given ms offsets.

        `spans` are RESOLVED calls (both bounds); `open_starts` are calls that
        never returned. An unresolved call contributes NO span — it has no
        `execution_completed_at`, and inventing one is what `None` exists to
        prevent. The collector sees every span at once, so a call straddling a
        boundary is clipped to each window it actually overlapped.
        """
        timeline: list[tuple[float, dict[str, Any]]] = [(0.0, _start()), (1000.0, _end(inp=100, out=20))]
        for i, (started, completed) in enumerate(spans):
            timeline += [(started, _open(f"closed-{i}")), (completed, _close(f"closed-{i}"))]
        timeline += [(started, _open(f"open-{i}")) for i, started in enumerate(open_starts)]
        stream: list[Any] = []
        for at_ms, event in sorted(timeline, key=lambda item: item[0]):
            stream += [Tick(at_ms), event]

        result, _ = _replay(stream)
        published = _assistants(result)
        assert len(published) == 1
        return published[0]

    def test_tool_time_inside_the_turn_is_subtracted(self):
        message = self._finish_turn([(200, 700)])
        span_ms = (message.completed_at - message.started_at).total_seconds() * 1000.0
        assert span_ms == pytest.approx(1000.0), "the decoder still publishes the whole window as its bounds"
        assert message.generation_duration_ms == pytest.approx(500.0)

    def test_a_turn_with_no_tools_keeps_its_whole_window(self):
        assert self._finish_turn([]).generation_duration_ms == pytest.approx(1000.0)

    def test_concurrent_tools_are_subtracted_once(self):
        # Two overlapping 500ms tools occupy 600ms, not 1000ms. Summing them
        # would leave 0 generation for a turn that generated 400.
        message = self._finish_turn([(100, 600), (200, 700)])
        assert message.generation_duration_ms == pytest.approx(400.0)

    def test_the_window_never_goes_negative(self):
        message = self._finish_turn([(-30_000, 31_000)])
        assert message.generation_duration_ms == 0.0

    def test_a_tool_still_open_at_the_boundary_contributes_no_span(self):
        """A call with no `execution_completed_at` was never timed.

        Its time is subtracted when it RESOLVES, from whichever windows its real
        interval overlaps — never bounded at the window's end.
        """
        message = self._finish_turn([], open_starts=(600,))
        assert message.generation_duration_ms == pytest.approx(1000.0)

    def test_a_resolved_tool_overlapping_an_unresolved_one_counts_only_the_resolved(self):
        message = self._finish_turn([(200, 700)], open_starts=(500,))
        assert message.generation_duration_ms == pytest.approx(500.0)

    def test_the_published_window_reconciles_to_its_own_bounds(self):
        """The collector subtracted exactly the spans the record carries.

        `scripts/timing/decompose_run.py` and the evalboard's Unaccounted cell
        both recompute the tool UNION from the recorded command spans and
        subtract it from the recorded window bounds. This asserts the published
        record is internally consistent under that recomputation, so a span
        silently added or dropped on the way in shows up here.
        """
        from coder_eval.timing import busy_ms

        message = self._finish_turn([(200, 700)])
        span_ms = (message.completed_at - message.started_at).total_seconds() * 1000.0
        expected = span_ms - busy_ms([(_ms(200), _ms(700))], message.started_at, message.completed_at)
        assert message.generation_duration_ms == pytest.approx(expected)


class TestGenerationWindowsTileTheTurn:
    """Each window runs from the PREVIOUS `turn_end`, not from its own `turn_start`.

    Measured from its own turn start, the wall clock between one `turn_end` and
    the next `turn_start` — the model time that PRODUCED the next turn — fell
    into no bucket at all.

    The gap is small in practice (measured across 25 real window pairs: median
    0.25 ms, max 0.75 ms). The value here is that it closes, and that the tool
    spans keep working once it does — see TestToolSpansSurviveTheTurnBoundary,
    which is the half that carries the weight.
    """

    def _two_turns(self) -> list[AssistantMessage]:
        result, _ = _replay([_start(), Tick(1000), _end(), Tick(1600), _start(), Tick(2000), _end()])
        return _assistants(result)

    def test_the_second_window_abuts_the_first(self):
        messages = self._two_turns()
        assert len(messages) == 2
        assert messages[1].started_at == messages[0].completed_at

    def test_the_inter_turn_gap_is_inside_a_window_rather_than_unaccounted(self):
        messages = self._two_turns()
        # 1000 -> 2000, which includes the 600ms between `turn_end` and the
        # next `turn_start`. Untiled this reported 400ms and lost the 600.
        assert messages[1].generation_duration_ms == pytest.approx(1000.0)


class TestToolSpansSurviveTheTurnBoundary:
    """A tool that closes BETWEEN two turns still belongs to the next window.

    `timing.subtract_tool_time` sees every span at once and clips each to the
    windows it overlaps, so the property holds by construction rather than by a
    reset rule. Kept because the property itself is what matters and a future
    decoder change could still break it — by moving a mark, or by failing to
    close the tool the collector reduces.
    """

    def _run(self) -> Replay:
        return _replay(
            [
                _start(),
                Tick(100),
                _open("c1"),
                Tick(1000),
                _end(),
                Tick(1500),
                _close("c1"),  # closes in the GAP
                Tick(1600),
                _start(),
                Tick(2000),
                _end(),
            ]
        )[0]

    def test_the_gap_slice_of_a_straddling_call_is_not_published_as_generation(self):
        messages = _assistants(self._run())
        # Window 2 tiles 1000 -> 2000. c1 ran for 1000 -> 1500 of it, so 500ms
        # is model time. With the reset left at `turn_start` this reads 1000.0.
        assert messages[1].generation_duration_ms == pytest.approx(500.0)

    def test_the_call_is_subtracted_from_exactly_one_window(self):
        messages = _assistants(self._run())
        # Window 1 bounded c1 at its own close (100 -> 1000); window 2 takes
        # only the remainder.
        assert messages[0].generation_duration_ms == pytest.approx(100.0)
        assert messages[1].generation_duration_ms == pytest.approx(500.0)

    def test_the_four_bucket_identity_closes_exactly_across_the_boundary(self):
        """generation + UNION(tool) accounts for the whole span, to the ms.

        This is the assertion the golden corpus CANNOT make: `_scrub.py` masks
        `generation_duration_ms` and both bounds to a placeholder, so a
        snapshot records that a window was measured and never what it measured.
        """
        from coder_eval.timing import busy_ms

        result = self._run()
        messages = _assistants(result)
        lo, hi = messages[0].started_at, messages[1].completed_at
        generation_ms = sum(m.generation_duration_ms or 0.0 for m in messages)
        command = next(c for c in result.record.commands if c.tool_id == "c1")
        assert command.execution_started_at is not None and command.execution_completed_at is not None
        tool_ms = busy_ms([(command.execution_started_at, command.execution_completed_at)], lo, hi)

        assert generation_ms + tool_ms == pytest.approx((hi - lo).total_seconds() * 1000.0)
        assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)

    def test_a_duplicate_turn_end_does_not_republish_the_previous_window(self):
        """A spent `turn_started_at` must not seed the next window.

        `close_window`'s `min(mark, item_start)` pulls the window open to cover
        the item's own start. A start stamp left in place after its turn was
        published is a stale value BEFORE the mark, so the guard would reopen the
        next window at the previous turn's start and publish that whole span
        again (3000 ms of generation for a 2000 ms turn). Pi's CLI retries
        internally, so a duplicate `turn_end` is a transport hiccup rather than a
        hypothetical.
        """
        result, _ = _replay([_start(), Tick(1000), _end(), Tick(2000), _end()])  # no intervening `turn_start`

        messages = _assistants(result)
        assert len(messages) == 2
        assert messages[1].started_at == messages[0].completed_at
        assert sum(m.generation_duration_ms or 0.0 for m in messages) == pytest.approx(2000.0)

    def test_a_duplicate_turn_end_does_not_republish_the_previous_content(self):
        """The CONTENT half of the same reset.

        Without it the replayed line re-emits the first turn's text as its own
        assistant message and re-lists the same `tool_use_ids` — one tool call
        appearing to belong to two generations.
        """
        text = {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "First."}}
        result, _ = _replay([_start(), text, _open("c1"), Tick(1000), _end(), Tick(2000), _end()])

        messages = _assistants(result)
        assert len(messages) == 2
        assert [b.text for b in messages[0].content_blocks if b.block_type == "text"] == ["First."]
        assert messages[0].tool_use_ids == ["c1"]
        assert messages[1].content_blocks == []
        assert messages[1].tool_use_ids == []

    def test_an_unresolved_orphan_is_not_given_a_completion_or_a_duration(self):
        """Force-closing is not observing a completion.

        Stamping the sweep's instant as `execution_completed_at` manufactures a
        bound that `timing.subtract_tool_time` then takes back out of a
        generation window the tool never occupied. `execution_started_at` IS
        kept: the CLI really did emit that start, and one bound alone forms no
        span (CE058).
        """
        result, _ = _replay([_start(), Tick(500), _open("c1"), Tick(4000)])

        closed = [e.tool for e in result.events if isinstance(e, ToolEndEvent)]
        assert len(closed) == 1
        assert closed[0].result_status == "unknown"
        assert closed[0].error_message is None
        assert closed[0].execution_started_at == _ms(500)
        assert closed[0].execution_completed_at is None
        assert closed[0].duration_ms is None

    def test_a_resolved_tool_still_gets_both_bounds_and_a_duration(self):
        """The guard narrows the UNRESOLVED case only."""
        result, _ = _replay([_start(), Tick(500), _open("c1"), Tick(1200), _close("c1")])

        closed = [e.tool for e in result.events if isinstance(e, ToolEndEvent)]
        assert len(closed) == 1
        assert closed[0].execution_completed_at == _ms(1200)
        assert closed[0].duration_ms == pytest.approx(700.0)


class TestClockIsFreshPerTurn:
    """A retried turn must not inherit the crashed turn's clock.

    `TurnClock` anchors once and derives every later stamp from that anchor, so
    one surviving a retry would stamp the new turn against the old turn's wall
    origin — and over a long run accumulate drift against real wall time. The
    lifetime is structural (the clock is built with the turn's emitter, and the
    emitter is built per `communicate()`), which is exactly the kind of property
    that stays true only while someone is checking.
    """

    async def test_a_turn_after_a_crash_is_anchored_to_a_fresh_clock(self, patch_exec, tmp_path):
        agent = _agent()
        patch_exec(_FakeProcess([], returncode=1, stderr=b"boom: bad model"))
        outcome = await _run(agent, tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED

        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = (await agent.communicate("try again", iteration=2)).record

        # The recovered turn measured a real window of its own, rather than one
        # anchored before the crash — which a stale clock would have produced
        # as an inflated first generation.
        windows = [m for m in record.messages if m.role == "assistant" and m.generation_duration_ms is not None]
        assert windows
        for message in windows:
            assert message.completed_at >= message.started_at
            assert message.generation_duration_ms < 60_000, "a window spanning the crashed turn means a stale clock"

    async def test_the_agent_retains_no_clock_between_turns(self, patch_exec, tmp_path):
        """Nothing to reset, because nothing survives — the structural half.

        The clock is reachable only through the turn's emitter and decoder, which
        are locals of `communicate()`. If either were ever hoisted onto the
        agent (a plausible refactor — several other fields are), the next turn
        would silently inherit the previous turn's anchor and no assertion
        about a single turn's numbers would notice.
        """
        agent = _agent()
        patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(agent, tmp_path)

        leaked = [
            name for name, value in vars(agent).items() if isinstance(value, _PiDecoder | TurnEmitter | TurnClock)
        ]
        assert not leaked, f"a turn's clock outlived its turn via {leaked}"


class TestTheTurnBracketComesFromTheTurnClock:
    """CE064's behavioural half: the SOURCE of the two bracket stamps.

    The rule can only see that `timestamp=` is present. Reverting it to
    `StreamEvent.timestamp`'s `default_factory=datetime.now` would leave the
    stamp within microseconds of the clock-derived one, which is precisely why
    the stand-in is anchored a year out — the revert then fails by a year.
    """

    async def test_both_brackets_are_stamped_from_the_injected_clock(
        self, patch_exec, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        import coder_eval.agent as agent_module

        monkeypatch.setattr(agent_module, "TurnClock", AnchoredClock)
        patch_exec(_FakeProcess(HAPPY_STREAM))
        recorder = _EventRecorder()
        await _run(_agent(), tmp_path, stream_callback=recorder)

        assert_bracket_on_the_clock(recorder.events)

    async def test_the_head_and_tail_are_measured_within_one_basis(
        self, patch_exec, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        """Both ends of `decompose_turn`'s subtraction come from one clock.

        A mixed pair is off by the anchor offset, not by a millisecond, so the
        bound here is what the assertion rests on rather than the sign.
        """
        import coder_eval.agent as agent_module

        monkeypatch.setattr(agent_module, "TurnClock", AnchoredClock)
        patch_exec(_FakeProcess(HAPPY_STREAM))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record

        assert_overhead_is_measured(record)
