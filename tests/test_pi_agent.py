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

from coder_eval.agents import pi_agent as agent_module
from coder_eval.agents.pi_agent import PiAgent, _PiTurnState, _result_text
from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.models import AgentKind, AssistantMessage, CommandTelemetry, PiAgentConfig
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
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


async def _run(agent: PiAgent, tmp_path: Any, prompt: str = "do the thing", **kwargs: Any):
    await agent.start(str(tmp_path))
    return await agent.communicate(prompt, **kwargs)


def _agent(**overrides: Any) -> PiAgent:
    config = PiAgentConfig(type="pi", **{"model": "openrouter/moonshotai/kimi-k3", **overrides})
    return PiAgent(config, task_id="t1")


class _EventRecorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


class TestHappyPath:
    async def test_builds_turn_record(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)

        assert record.crashed is False
        # 3 turn_start steps in the fixture (write, read, summarize).
        assert record.assistant_turn_count == 3
        assert record.model_used == "openrouter/moonshotai/kimi-k3"
        assert "hi" in record.agent_output

    async def test_token_buckets_sum_across_turns(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)

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
        record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert sum(m.input_tokens for m in record.messages) == usage.uncached_input_tokens
        assert sum(m.output_tokens for m in record.messages) == usage.output_tokens
        assert sum(m.cache_read_tokens for m in record.messages) == usage.cache_read_input_tokens

    async def test_tool_calls_captured(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)

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
        record = await _run(_agent(), tmp_path)

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
        record = await _run(_agent(), tmp_path)
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
        record = await _run(_agent(), tmp_path)
        assert record.commands[0].tool_name == "Glob"

    async def test_unknown_tool_passes_through(self, patch_exec, tmp_path):
        stream = [
            _turn_start(),
            _tool_start("x:0", "some_new_tool", {"whatever": 1}),
            _tool_end("x:0", "some_new_tool", "ok"),
            _turn_end(inp=10, out=5),
        ]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(), tmp_path)
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
        record = await _run(_agent(), tmp_path)
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

    async def test_tools_are_not_forwarded_but_system_prompt_is(self, patch_exec, tmp_path):
        """allowed_tools/disallowed_tools are NOT forwarded: the shared config default
        sets Claude-namespaced tool names (Bash/Read/...) that do not exist in Pi
        (lowercase bash/read/...), so `--tools` would strip the agent of ALL tools.
        Only `system_prompt` (a free-text string with no namespace) is forwarded."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(
            _agent(allowed_tools=["Read", "Write"], disallowed_tools=["Bash"], system_prompt="be terse"),
            tmp_path,
        )
        argv = captured["argv"]
        assert "--tools" not in argv
        assert "--exclude-tools" not in argv
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
        await agent.communicate("follow up")
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
        await agent.communicate("do the thing")

        assert captured["kwargs"]["env"]["PATH"] == os.pathsep.join(["/sandbox/mocks", "/sandbox/bins", "/parent/bin"])

    async def test_host_environment_is_inherited_whole(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["env"]["OPENROUTER_API_KEY"] == "sk-test"


class TestUnsupportedConfigIsAnnounced:
    async def test_start_warns_about_unenforced_fields(self, patch_exec, tmp_path, caplog):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        with caplog.at_level("WARNING"):
            await _agent(permission_mode="plan").start(str(tmp_path))
        assert "permission_mode" in caplog.text
        assert "NOT enforced" in caplog.text

    async def test_plugins_that_do_not_resolve_warn_loudly(self, patch_exec, tmp_path, caplog):
        """plugins IS supported now (-> --skill), but a path that resolves to no skills
        must warn — else the run silently measures the model WITHOUT the skill."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        with caplog.at_level("WARNING"):
            await _agent(plugins=[{"type": "local", "path": "/no/such/dir"}]).start(str(tmp_path))
        assert "0 skill dir(s) resolved" in caplog.text or "did not resolve" in caplog.text
        # plugins is no longer named in the "NOT enforced" warning.
        assert "plugins" not in "".join(r.message for r in caplog.records if "NOT enforced" in r.message)

    async def test_unenforced_fields_warn_but_system_prompt_does_not(self, patch_exec, tmp_path, caplog):
        """allowed_tools/disallowed_tools are unenforced (Claude-namespaced default cannot
        map to Pi's lowercase toolset) and MUST warn when set. `system_prompt` IS enforced
        (--append-system-prompt) and must never appear in the unenforced-fields warning."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        with caplog.at_level("WARNING"):
            await _agent(allowed_tools=["Read"], disallowed_tools=["Bash"], system_prompt="be terse").start(
                str(tmp_path)
            )
        warning = "".join(r.message for r in caplog.records if "NOT enforced" in r.message)
        assert "allowed_tools" in warning
        assert "disallowed_tools" in warning
        assert "system_prompt" not in warning  # the enforced field is never named


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
        record = await _run(_agent(), tmp_path, stream_callback=recorder)

        assert record.crashed is False
        assert len([e for e in recorder.events if isinstance(e, AgentEndEvent)]) == 1
        # Both cycles' usage merged (SUM): input 100+200, output 10+20.
        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 300
        assert record.token_usage.output_tokens == 30
        assert record.assistant_turn_count == 2


class TestCooperativeStop:
    def test_capability_flag_is_declared(self):
        assert PiAgent.supports_cooperative_stop is True

    async def test_should_stop_ends_turn_cleanly(self, patch_exec, tmp_path):
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        record = await _run(_agent(), tmp_path, should_stop=lambda: True)

        assert record.crashed is False
        assert proc.terminated is True
        # should_stop() is True from the first check, which lands after the first
        # streamed line (the `session` header) and before any turn completes — so
        # the cut is at turn 0, not merely "fewer than the full 3".
        assert record.assistant_turn_count == 0

    async def test_partial_record_is_returned_not_raised(self, patch_exec, tmp_path):
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        record = await _run(_agent(), tmp_path, should_stop=lambda: True)
        assert isinstance(record.assistant_turn_count, int)


class TestMaxTurns:
    async def test_max_turns_marks_exhausted(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path, max_turns=1)
        assert record.max_turns_exhausted is True

    async def test_a_cap_the_run_stays_under_is_not_exhausted(self, patch_exec, tmp_path):
        """The fixture is exactly 3 turns, so max_turns=3 is the boundary."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path, max_turns=3)
        assert record.max_turns_exhausted is False
        assert record.assistant_turn_count == 3

    async def test_the_deciding_turn_is_kept_whole(self, patch_exec, tmp_path):
        """max_turns=1 cuts at the START of turn 2, so turn 1 survives complete."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path, max_turns=1)

        assert record.max_turns_exhausted is True
        assert len(record.commands) == 1  # turn 1's write
        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 406  # turn 1's input exactly
        assert usage.output_tokens == 77  # 69 + 8 reasoning

    async def test_no_cap_is_uncapped(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)
        assert record.max_turns_exhausted is False
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
    async def test_deadline_raises_turn_timeout_with_partial_parked(self, patch_exec, tmp_path):
        proc = _HangingProcess([_turn_start()])
        patch_exec(proc)
        agent = _agent()
        recorder = _EventRecorder()

        with pytest.raises(TurnTimeoutError):
            await _run(agent, tmp_path, timeout=0.2, stream_callback=recorder)

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        assert proc.terminated is True
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].status is AgentEndStatus.TIMEOUT

        await agent.discard_pending_turn()
        assert agent._iteration == 0


class _ExplodingProcess(_FakeProcess):
    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        raise ValueError("Separator is not found, and chunk exceed the limit")


class TestFailurePaths:
    async def test_nonzero_exit_crashes(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess([], returncode=1, stderr=b"boom: bad model"))
        with pytest.raises(AgentCrashError, match="boom: bad model"):
            await _run(_agent(), tmp_path)

    async def test_empty_clean_exit_crashes_on_no_recognized_events(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess([], returncode=0))
        with pytest.raises(AgentCrashError, match="no recognized events"):
            await _run(_agent(), tmp_path)

    async def test_drift_crash_names_the_unrecognized_types(self, patch_exec, tmp_path):
        """A clean exit whose events are all unrecognized (schema drift) crashes and
        names what it saw, for diagnosis."""
        stream = [
            json.dumps({"type": "some.new.event", "foo": 1}),
            json.dumps({"type": "another.unknown", "bar": 2}),
        ]
        patch_exec(_FakeProcess(stream))
        with pytest.raises(AgentCrashError, match=r"another\.unknown, some\.new\.event") as exc:
            await _run(_agent(), tmp_path)
        assert "no recognized events" in str(exc.value)

    async def test_stream_error_becomes_a_crash_with_partial_parked(self, patch_exec, tmp_path):
        stream = [_turn_start(), _tool_start("w:0", "write", {"path": "a.txt", "content": "x"})]
        patch_exec(_ExplodingProcess(stream))
        agent = _agent()

        with pytest.raises(AgentCrashError, match="Pi turn failed"):
            await _run(agent, tmp_path)

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        # The in-flight tool was force-closed rather than dropped.
        assert [c.result_status for c in partial.commands] == ["unknown"]

    async def test_spawn_failure_becomes_a_crash(self, monkeypatch, tmp_path):
        async def boom(*_argv: str, **_kwargs: Any):
            raise OSError("no fork for you")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/pi")

        with pytest.raises(AgentCrashError, match="no fork for you"):
            await _run(_agent(), tmp_path)

    async def test_malformed_line_is_skipped(self, patch_exec, tmp_path):
        stream = ["not json at all", *HAPPY_STREAM]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(), tmp_path)
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

        with pytest.raises(AgentCrashError):
            await _run(_agent(), tmp_path, stream_callback=recorder)

        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].crashed is True
        assert ends[0].status is AgentEndStatus.CRASHED

    async def test_iteration_rolls_back_after_the_crash(self, patch_exec, tmp_path):
        patch_exec(_ExplodingProcess([]))
        agent = _agent()

        with pytest.raises(AgentCrashError):
            await _run(agent, tmp_path)
        assert agent._iteration == 1
        await agent.discard_pending_turn()
        assert agent._iteration == 0
        assert agent.pending_turn is None


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

        with pytest.raises(TurnTimeoutError):
            await _run(_agent(), tmp_path, timeout=0.2, stream_callback=recorder)

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
        assert PiAgent.supports_cost_log_tags is False
        with pytest.raises(TypeError):
            PiAgent(config, cost_log_tags={"x": "y"})  # type: ignore[call-arg]


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
        record = await _run(_agent(), tmp_path, stream_callback=recorder)

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
        state = _PiTurnState(task_id="t", iteration=1, user_input="x", model=None)
        events: list[Any] = []
        state.bind(events.append)
        state._close_tool("ghost", status=ToolEndStatus.UNRESOLVED, summary=None, error="no result observed")

        [event] = events
        assert isinstance(event, ToolEndEvent)
        assert event.tool.tool_name == "unknown"
        assert event.tool.result_status == "unknown"


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
        record = await _run(_agent(), tmp_path)
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
        with pytest.raises(AgentCrashError, match="blocked by guardrail"):
            await _run(agent, tmp_path)
        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True

    async def test_max_turns_cut_after_an_error_turn_finalizes_cleanly(self, patch_exec, tmp_path):
        """A max_turns cut landing right after an error turn_end (pi still retrying,
        so error_message is set but not yet cleared) must finalize as
        max_turns_exhausted — NOT crash on the stale error. Guards the documented
        'no crash, no retry' contract; without the intentional-cut gate the error
        arm would fire on a clean budget exhaustion."""
        # turn 1 errors; turn 2's turn_start trips max_turns=1 before any clean
        # turn_end can clear error_message.
        stream = [_turn_start(), _turn_end_error("transient 429"), _turn_start(), _turn_end(inp=1, out=1)]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(), tmp_path, max_turns=1)
        assert record.max_turns_exhausted is True
        assert record.crashed is False

    def test_error_message_resets_on_a_recovered_turn(self):
        """#3: an intermediate error a later cycle recovers from must not leak into the result."""
        state = _PiTurnState(task_id="t", iteration=1, user_input="x", model="m")
        state.on_turn_end(json.loads(_turn_end_error("transient")))
        assert state.error_message == "transient"
        state.on_turn_end(json.loads(_turn_end(inp=5, out=2)))  # a later, successful turn
        assert state.error_message is None

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
            record = await _run(_agent(), tmp_path)
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
            record = await _run(_agent(), tmp_path)
        assert record.crashed is False
        assert any("does not reconcile" in r.getMessage() for r in caplog.records)


class TestSkillInjection:
    """agent.plugins -> `pi --skill <dir>` (mirrors OpenCode/Codex plugin->skills)."""

    def _plugin_root(self, tmp_path):
        # A Claude-plugin root: <root>/skills/<name>/SKILL.md (manifest-default layout).
        root = tmp_path / "plug"
        skill = root / "skills" / "demo-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n# Demo\n")
        return root

    async def test_resolved_plugin_emits_skill_arg(self, patch_exec, tmp_path):
        root = self._plugin_root(tmp_path)
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(plugins=[{"type": "local", "path": str(root)}]), tmp_path)
        argv = captured["argv"]
        assert "--skill" in argv
        # Points at the skills-PARENT dir (Pi discovers <name>/SKILL.md recursively).
        assert argv[argv.index("--skill") + 1] == str((root / "skills").resolve())

    async def test_no_plugins_means_no_skill_arg(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert "--skill" not in captured["argv"]

    async def test_skill_paths_recorded_in_environment_info(self, patch_exec, tmp_path):
        # Installs the shutil.which("pi") patch so start() doesn't require the real
        # CLI on PATH (CI has no pi binary); we only assert on recorded skill paths.
        patch_exec(_FakeProcess(HAPPY_STREAM))
        root = self._plugin_root(tmp_path)
        agent = _agent(plugins=[{"type": "local", "path": str(root)}])
        await agent.start(str(tmp_path))
        assert agent.get_environment_info()["pi_skill_paths"] == [str((root / "skills").resolve())]


class TestTurnAlwaysReapsTheCli:
    """No exit from ``communicate()`` may leave the CLI running. ``AgentCrashError``
    is categorized AGENT_CRASH (max_retries=2) and the orchestrator's attempt-failure
    hook only drains ``pending_turn`` — it never kills the agent. An abandoned CLI
    therefore means attempt 2 spawns a SECOND ``pi`` editing the very files the
    criteria are about to score. The graceful ``kill()`` covers the intentional cuts
    and the timeout; these pin the two paths that reach ``finally`` with a live child.
    """

    async def test_read_loop_crash_kills_the_cli(self, patch_exec, tmp_path):
        """``_crash_turn`` is synchronous and raises — nothing below it reaps."""
        proc = _ExplodingRunningProcess([_turn_start()])
        patch_exec(proc)
        with pytest.raises(AgentCrashError, match="Pi turn failed"):
            await _run(_agent(), tmp_path)
        assert proc.killed is True

    async def test_external_cancel_kills_the_cli(self, patch_exec, tmp_path):
        """Teardown must survive a CancelledError in flight (no await), or the child
        outlives an interrupted turn."""
        proc = _HangingProcess([_turn_start()])
        patch_exec(proc)
        agent = _agent()
        await agent.start(str(tmp_path))
        task = asyncio.ensure_future(agent.communicate("do the thing"))
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
    async def test_cancel_parks_partial_and_reraises(self, patch_exec, tmp_path):
        """The watchdog's CancelledError must not swallow captured telemetry: the
        partial is parked, the terminal event says CRASHED, and the cancellation
        still propagates."""
        proc = _HangingProcess([_turn_start()])
        patch_exec(proc)
        agent = _agent()
        await agent.start(str(tmp_path))
        recorder = _EventRecorder()
        task = asyncio.ensure_future(agent.communicate("do the thing", stream_callback=recorder))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task  # the await re-raises the cancellation; no value ever exists
        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].status is AgentEndStatus.CRASHED
        assert ends[0].crash_reason == "turn cancelled"
        assert proc.killed is True  # not abandoned mid-stream — see TestTurnAlwaysReapsTheCli


def _turn_end_no_cost(*, inp: int, out: int) -> str:
    """A `turn_end` whose usage object omits the `cost` key (provider/auth mode that
    reports no cost) — so `_resolve_cost` must fall back to the rate card."""
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
        record = await _run(_agent(), tmp_path)
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(0.5)

    async def test_missing_cost_is_priced_from_the_rate_card(self, patch_exec, tmp_path):
        """No `cost` key at all — without the fallback the turn books tokens with no money."""
        stream = [_turn_start(), _turn_end_no_cost(inp=1000, out=500), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(), tmp_path)
        expected = calculate_cost("openrouter/moonshotai/kimi-k3", uncached_input_tokens=1000, output_tokens=500)
        assert expected is not None and expected > 0
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(expected)

    async def test_unpriced_model_reports_no_cost(self, patch_exec, tmp_path):
        """`None` (not 0.0) so "unpriceable" stays distinct from "ran for free"."""
        stream = [_turn_start(), _turn_end_no_cost(inp=10, out=5), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(model="nowhere/not-a-real-model"), tmp_path)
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd is None

    async def test_zero_reported_cost_on_a_priced_model_uses_the_rate_card(self, patch_exec, tmp_path, caplog):
        """A reported $0 on a model the rate card prices is a subscription/registry
        gap, not a free run — latching on it would book real tokens with no money."""
        stream = [_turn_start(), _turn_end(inp=1000, out=500, cost=0.0), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        with caplog.at_level("DEBUG"):
            record = await _run(_agent(), tmp_path)
        expected = calculate_cost("openrouter/moonshotai/kimi-k3", uncached_input_tokens=1000, output_tokens=500)
        assert expected is not None and expected > 0
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(expected)
        assert "not understated" in caplog.text

    async def test_zero_reported_cost_on_an_unpriced_model_stays_zero(self, patch_exec, tmp_path):
        """With no rate to fall back to, the stream's 0 is the best information we have."""
        stream = [_turn_start(), _turn_end(inp=10, out=5, cost=0.0), self._SETTLED]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(model="nowhere/not-a-real-model"), tmp_path)
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == 0.0


class TestGenerationWindowExcludesToolExecution:
    """A tool running inside a turn is not model time.

    Pi marks the window at `turn_start` and closes it at `turn_end`, and
    every tool call executes INSIDE it while also publishing its own
    measured `duration_ms`. Publishing the raw span as generation time
    counted the same milliseconds twice, which the task page's Unaccounted
    cell renders as a ~-100% residual.

    Driven at the reducer: the window is two `datetime.now()` reads and the
    tool interval comes from the event payload, so only setting both
    explicitly makes the arithmetic deterministic.
    """

    WINDOW_START = datetime(2026, 1, 1, 12, 0, 0)
    WINDOW_END = datetime(2026, 1, 1, 12, 0, 1)  # a 1000ms turn

    def _finish_turn(self, monkeypatch, spans, open_starts=()):
        class _Clock(datetime):
            @staticmethod
            def now(tz=None):
                return TestGenerationWindowExcludesToolExecution.WINDOW_END

        state = _PiTurnState(task_id="t", iteration=1, user_input="x", model="m")
        state.turn_started_at = self.WINDOW_START
        state.turn_tool_spans = list(spans)
        for i, started in enumerate(open_starts):
            state.open_tools[f"open-{i}"] = CommandTelemetry(
                tool_name="bash",
                tool_id=f"open-{i}",
                timestamp=started,
                execution_started_at=started,
            )
        monkeypatch.setattr(agent_module, "datetime", _Clock)
        state.on_turn_end(
            {"message": {"role": "assistant", "usage": {"input": 100, "output": 20}, "stopReason": "stop"}}
        )
        assistant = [m for m in state.messages if m.role == "assistant"]
        assert len(assistant) == 1
        return assistant[0]

    def test_tool_time_inside_the_turn_is_subtracted(self, monkeypatch):
        message = self._finish_turn(
            monkeypatch,
            [(self.WINDOW_START + timedelta(milliseconds=200), self.WINDOW_START + timedelta(milliseconds=700))],
        )
        span_ms = (message.completed_at - message.started_at).total_seconds() * 1000.0
        assert span_ms == pytest.approx(1000.0)
        assert message.generation_duration_ms == pytest.approx(500.0)

    def test_a_turn_with_no_tools_keeps_its_whole_window(self, monkeypatch):
        assert self._finish_turn(monkeypatch, []).generation_duration_ms == pytest.approx(1000.0)

    def test_concurrent_tools_are_subtracted_once(self, monkeypatch):
        # Two overlapping 500ms tools occupy 600ms, not 1000ms. Summing them
        # would leave 0 generation for a turn that generated 400.
        message = self._finish_turn(
            monkeypatch,
            [
                (self.WINDOW_START + timedelta(milliseconds=100), self.WINDOW_START + timedelta(milliseconds=600)),
                (self.WINDOW_START + timedelta(milliseconds=200), self.WINDOW_START + timedelta(milliseconds=700)),
            ],
        )
        assert message.generation_duration_ms == pytest.approx(400.0)

    def test_the_window_never_goes_negative(self, monkeypatch):
        message = self._finish_turn(
            monkeypatch,
            [(self.WINDOW_START - timedelta(seconds=30), self.WINDOW_END + timedelta(seconds=30))],
        )
        assert message.generation_duration_ms == 0.0

    def test_a_tool_still_open_at_the_boundary_is_subtracted(self, monkeypatch):
        # A call that opens inside this turn and closes inside the NEXT one
        # straddles the boundary. Counting only closed intervals published the
        # pre-boundary 400ms as generation while the call's own duration_ms
        # counted it again.
        message = self._finish_turn(
            monkeypatch,
            [],
            open_starts=[self.WINDOW_START + timedelta(milliseconds=600)],
        )
        assert message.generation_duration_ms == pytest.approx(600.0)

    def test_an_open_tool_overlapping_a_closed_one_is_counted_once(self, monkeypatch):
        # Union, not sum, across the closed and still-open sets alike.
        message = self._finish_turn(
            monkeypatch,
            [(self.WINDOW_START + timedelta(milliseconds=200), self.WINDOW_START + timedelta(milliseconds=700))],
            open_starts=[self.WINDOW_START + timedelta(milliseconds=500)],
        )
        assert message.generation_duration_ms == pytest.approx(200.0)
