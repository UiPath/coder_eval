"""Tests for the OpenCode agent harness.

The CLI is never invoked: ``asyncio.create_subprocess_exec`` is patched with a
fake process that replays a newline-delimited JSON event stream, so the whole
reduction path (nd-JSON -> standardized events -> ``TurnRecord``) is exercised
offline and without credentials.

The fixtures below mirror event lines CAPTURED FROM A LIVE ``opencode run
--format json`` — the CLI's own compact vocabulary (``step_start`` /
``step_finish`` / ``text`` / ``tool_use``, payload under ``part``). Do NOT
"correct" them toward the ``session.next.*`` names in the server's OpenAPI
schema: those describe `opencode serve`'s SSE surface, and an earlier version of
this harness parsed them and silently captured zero telemetry on a real run.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

import pytest

from coder_eval.agents.opencode_agent import OpenCodeAgent, _OpenCodeTurnState, _unwrap
from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.models import AssistantMessage, OpenCodeAgentConfig, PermissionMode
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    ToolEndEvent,
    ToolEndStatus,
)


SESSION = "ses_test123"


def _evt(event_type: str, part: dict[str, Any]) -> str:
    """One CLI event line: payload under ``part``, sessionID on the envelope."""
    return json.dumps(
        {"type": event_type, "timestamp": 1786663016802, "sessionID": SESSION, "part": {"sessionID": SESSION, **part}}
    )


def _tokens(inp: int, out: int, *, write: int = 0, read: int = 0, reasoning: int = 0) -> dict[str, Any]:
    """Token payload in the NESTED convention (total = input+output+reasoning, cache
    counted inside `input`); see TestTokenShapeIsObservable for the flat one."""
    return {
        "total": inp + out + reasoning,
        "input": inp,
        "output": out,
        "reasoning": reasoning,
        "cache": {"write": write, "read": read},
    }


HAPPY_STREAM = [
    _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
    _evt(
        "tool_use",
        {
            "id": "prt_2",
            "messageID": "msg_1",
            "type": "tool",
            "tool": "read",
            "callID": "call_1",
            "state": {
                "status": "completed",
                "input": {"filePath": "main.py"},
                "output": "print('hi')",
                "time": {"start": 1786663018214, "end": 1786663018231},
            },
        },
    ),
    _evt(
        "step_finish",
        {
            "id": "prt_3",
            "messageID": "msg_1",
            "reason": "tool-calls",
            "cost": 0.001,
            "tokens": _tokens(100, 20, write=5, read=10),
        },
    ),
    _evt("step_start", {"id": "prt_4", "messageID": "msg_2", "type": "step-start"}),
    _evt("text", {"id": "prt_5", "messageID": "msg_2", "type": "text", "text": "Created the file."}),
    _evt(
        "step_finish",
        {
            "id": "prt_6",
            "messageID": "msg_2",
            "reason": "stop",
            "cost": 0.002,
            "tokens": _tokens(50, 30, read=40, reasoning=7),
        },
    ),
]


class _FakeProcess:
    def __init__(self, lines: list[str], returncode: int = 0, stderr: bytes = b"") -> None:
        self._lines = [f"{line}\n".encode() for line in lines]
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._stderr = stderr
        self.pid = 4242
        self.terminated = False
        self.stdout = self

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self.returncode = self._final_returncode
        return b""

    async def read(self) -> bytes:
        return self._stderr

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._final_returncode

    def kill(self) -> None:
        self.returncode = self._final_returncode


class _RunningProcess(_FakeProcess):
    """A process that stays alive until it is explicitly terminated or killed.

    Needed for teardown assertions: the plain fake reports an exit code as soon
    as ``wait()`` is awaited, so ``kill()`` would (correctly) skip ``terminate()``
    on an already-dead process and the test would prove nothing.
    """

    def __init__(self, lines: list[str], **kwargs: Any) -> None:
        super().__init__(lines, **kwargs)
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self._exited.set()

    def kill(self) -> None:
        self._exited.set()


@pytest.fixture
def patch_exec(monkeypatch: pytest.MonkeyPatch):
    """Patch subprocess spawn; return a dict capturing the argv used.

    Also stubs ``os.killpg`` (recording each call under ``captured["killpg"]``) so
    the agent's process-group sweep can never signal a real group whose id happens
    to collide with the fake pid.
    """
    captured: dict[str, Any] = {"killpg": []}

    def _install(proc: _FakeProcess) -> dict[str, Any]:
        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            proc.stderr = proc  # type: ignore[assignment]
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/opencode")
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: captured["killpg"].append((pgid, sig)))
        return captured

    return _install


async def _run(agent: OpenCodeAgent, tmp_path: Any, prompt: str = "do the thing", **kwargs: Any):
    await agent.start(str(tmp_path))
    return await agent.communicate(prompt, **kwargs)


def _agent(**overrides: Any) -> OpenCodeAgent:
    config = OpenCodeAgentConfig(type="opencode", **{"model": "deepseek/deepseek-v4-pro", **overrides})
    return OpenCodeAgent(config, task_id="t1")


class TestEnvelopeNormalization:
    def test_part_envelope(self):
        """Normal events carry their payload under `part`."""
        t, part = _unwrap({"type": "step_finish", "sessionID": "s", "part": {"reason": "stop"}})
        assert t == "step_finish"
        assert part["reason"] == "stop"

    def test_flat_envelope(self):
        """The CLI's own error path emits a flat object with no `part`."""
        t, props = _unwrap({"type": "error", "sessionID": "s", "error": {"name": "UnknownError"}})
        assert t == "error"
        assert props["error"]["name"] == "UnknownError"


class TestHappyPath:
    async def test_builds_turn_record(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)

        assert record.crashed is False
        assert record.agent_output == "Created the file."
        assert record.assistant_turn_count == 2
        assert record.model_used == "deepseek/deepseek-v4-pro"

    async def test_token_buckets_accumulate_across_steps(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        # The fixture encodes the nested convention (`input` includes the cache
        # buckets), so the fresh slice subtracts them:
        # step1 100-10-5=85, step2 50-40=10  -> 95
        assert usage.uncached_input_tokens == 95
        # reasoning bills at the output rate: step1 20+0=20, step2 30+7=37 -> 57
        assert usage.output_tokens == 57
        assert usage.cache_creation_input_tokens == 5
        assert usage.cache_read_input_tokens == 50  # 10 + 40
        assert usage.total_cost_usd == pytest.approx(0.003)

    async def test_reconciliation_invariant(self, patch_exec, tmp_path):
        """Summing the four buckets across messages must equal token_usage exactly."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert sum(m.input_tokens for m in record.messages) == usage.uncached_input_tokens
        assert sum(m.output_tokens for m in record.messages) == usage.output_tokens
        assert sum(m.cache_creation_tokens for m in record.messages) == usage.cache_creation_input_tokens
        assert sum(m.cache_read_tokens for m in record.messages) == usage.cache_read_input_tokens

    async def test_tool_call_captured(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)

        assert len(record.commands) == 1
        cmd = record.commands[0]
        # Normalized to the canonical vocabulary criteria are written against.
        assert cmd.tool_name == "Read"
        assert cmd.tool_id == "call_1"
        assert cmd.result_status == "success"
        assert cmd.parameters == {"filePath": "main.py"}
        assert cmd.result_summary == "print('hi')"
        # Duration comes from state.time, not our parse instant (17ms in fixture).
        assert cmd.duration_ms == pytest.approx(17, abs=1)

    async def test_messages_attributed_to_steps(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)

        assistants = [m for m in record.messages if isinstance(m, AssistantMessage)]
        assert len(assistants) == 2
        assert assistants[0].tool_use_ids == ["call_1"]
        assert assistants[0].stop_reason == "tool-calls"
        assert assistants[1].tool_use_ids == []


class TestTokenShapeIsObservable:
    """Two conventions for `tokens.input` exist in the wild — flat (`input` IS the
    fresh slice; `total` adds the cache buckets on top) and nested (cached tokens
    counted inside `input`, the OpenAI convention). The stream's own `total`
    arbitrates per step; a `total` matching neither must be loud, because a silent
    mis-mapping under- or over-books a bucket on every cached run.
    """

    @staticmethod
    def _step(tokens: dict[str, Any]) -> str:
        return _evt("step_finish", {"id": "p", "messageID": "m", "reason": "stop", "tokens": tokens})

    async def test_flat_convention_keeps_input_verbatim(self, patch_exec, tmp_path, caplog):
        """The exact numbers of a live capture (2026-08-13): 7966 = 6796+128+18+1024,
        so `input` excludes the cache buckets and must NOT have them subtracted."""
        step = self._step(
            {"total": 7966, "input": 6796, "output": 128, "reasoning": 18, "cache": {"read": 1024, "write": 0}}
        )
        patch_exec(_FakeProcess([step]))
        with caplog.at_level("WARNING"):
            record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 6796
        assert usage.cache_read_input_tokens == 1024
        assert usage.output_tokens == 146  # 128 + 18 reasoning
        assert "unexpected token accounting" not in caplog.text

    async def test_nested_convention_subtracts_the_cache_buckets(self, patch_exec, tmp_path, caplog):
        """total = input+output+reasoning ⇒ cached tokens nest inside `input`; the
        fresh slice must come back out or the cached portion is billed twice."""
        patch_exec(_FakeProcess(HAPPY_STREAM))  # _tokens() builds nested totals
        with caplog.at_level("WARNING"):
            record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 95  # (100-10-5) + (50-40)
        assert "unexpected token accounting" not in caplog.text

    async def test_total_matching_neither_convention_warns(self, patch_exec, tmp_path, caplog):
        """nested=350, flat=8030, reported 8000 — the schema moved; keep `input`."""
        patch_exec(_FakeProcess([self._step({"total": 8000, "input": 300, "output": 50, "cache": {"read": 7680}})]))
        with caplog.at_level("WARNING"):
            record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 300
        assert usage.cache_read_input_tokens == 7680
        assert "matches neither" in caplog.text

    async def test_total_disagreeing_with_zero_cache_buckets_warns(self, patch_exec, tmp_path, caplog):
        """With no cache traffic the conventions coincide; a mismatch is still drift."""
        patch_exec(_FakeProcess([self._step({"total": 999, "input": 100, "output": 20, "reasoning": 5})]))
        with caplog.at_level("WARNING"):
            await _run(_agent(), tmp_path)
        assert "tokens.total" in caplog.text

    async def test_missing_total_with_cache_traffic_defaults_flat_but_warns(self, patch_exec, tmp_path, caplog):
        """No arbiter + cache traffic ⇒ the flat reading is an UNVERIFIABLE assumption
        (the original mapping bug was exactly such an assumption), so it must not be
        silent — but `input` is still taken verbatim, the live-verified convention."""
        patch_exec(_FakeProcess([self._step({"input": 500, "output": 20, "cache": {"read": 200}})]))
        with caplog.at_level("WARNING"):
            record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 500
        assert usage.cache_read_input_tokens == 200
        assert "tokens.total is missing" in caplog.text

    async def test_missing_total_without_cache_traffic_is_silent(self, patch_exec, tmp_path, caplog):
        """No arbiter but no cache either ⇒ the conventions agree; nothing to verify."""
        patch_exec(_FakeProcess([self._step({"input": 500, "output": 20})]))
        with caplog.at_level("WARNING"):
            record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 500
        assert "unexpected token accounting" not in caplog.text

    async def test_nested_total_contradicted_by_small_input_warns(self, patch_exec, tmp_path, caplog):
        """`total` says nested but input < cache: self-contradictory; keep `input`."""
        patch_exec(_FakeProcess([self._step({"total": 350, "input": 300, "output": 50, "cache": {"read": 7680}})]))
        with caplog.at_level("WARNING"):
            record = await _run(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 300
        assert "nest inside input" in caplog.text


class TestCostFallsBackToTheRateCard:
    async def test_stream_cost_wins_when_reported(self, patch_exec, tmp_path):
        """The provider's own accounting beats a static headline rate."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path)
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(0.003)  # 0.001 + 0.002

    async def test_missing_cost_is_priced_from_the_rate_card(self, patch_exec, tmp_path):
        """Without this the turn books tokens with no money and the run total understates."""
        stream = [
            _evt("step_start", {"id": "prt_1", "messageID": "msg_1"}),
            # No `cost` key — the provider/auth mode did not report one.
            _evt("step_finish", {"id": "prt_2", "messageID": "msg_1", "reason": "stop", "tokens": _tokens(1000, 500)}),
        ]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(), tmp_path)

        assert record.token_usage is not None
        expected = calculate_cost("deepseek/deepseek-v4-pro", uncached_input_tokens=1000, output_tokens=500)
        assert expected is not None and expected > 0
        assert record.token_usage.total_cost_usd == pytest.approx(expected)

    async def test_unpriced_model_reports_no_cost(self, patch_exec, tmp_path):
        """`None` (not 0.0) so "unpriceable" stays distinct from "ran for free"."""
        patch_exec(_FakeProcess([_evt("step_finish", {"id": "p", "reason": "stop", "tokens": _tokens(10, 5)})]))
        record = await _run(_agent(model="nowhere/not-a-real-model"), tmp_path)
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd is None

    async def test_zero_reported_cost_on_a_priced_model_uses_the_rate_card(self, patch_exec, tmp_path, caplog):
        """OpenCode reports `cost: 0` when its own registry lacks a price for the
        model, or under subscription-style auth — neither means the tokens were
        free. Latching on the reported 0 would book real tokens with no money."""
        stream = [
            _evt("step_start", {"id": "prt_1", "messageID": "msg_1"}),
            _evt(
                "step_finish",
                {"id": "prt_2", "messageID": "msg_1", "reason": "stop", "cost": 0, "tokens": _tokens(1000, 500)},
            ),
        ]
        patch_exec(_FakeProcess(stream))
        with caplog.at_level("WARNING"):
            record = await _run(_agent(), tmp_path)

        expected = calculate_cost("deepseek/deepseek-v4-pro", uncached_input_tokens=1000, output_tokens=500)
        assert expected is not None and expected > 0
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(expected)
        assert "not understated" in caplog.text

    async def test_zero_reported_cost_on_an_unpriced_model_stays_zero(self, patch_exec, tmp_path):
        """With no rate to fall back to, the stream's 0 is the best information we have."""
        stream = [
            _evt("step_start", {"id": "prt_1", "messageID": "msg_1"}),
            _evt(
                "step_finish",
                {"id": "prt_2", "messageID": "msg_1", "reason": "stop", "cost": 0, "tokens": _tokens(10, 5)},
            ),
        ]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(model="nowhere/not-a-real-model"), tmp_path)
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == 0.0


class TestCrossHarnessNormalization:
    """A criterion written once must score identically on every harness."""

    @staticmethod
    def _tool_event(tool: str) -> str:
        return _evt(
            "tool_use",
            {
                "id": "prt_2",
                "messageID": "msg_1",
                "type": "tool",
                "tool": tool,
                "callID": f"call_{tool}",
                "state": {"status": "completed", "input": {"command": "pytest -q"}, "output": "ok"},
            },
        )

    async def test_native_names_map_to_canonical(self, patch_exec, tmp_path):
        """`command_executed` filters on `tool_name == "Bash"` and pulls
        `parameters["command"]` only for that name — OpenCode's `bash` would match
        nothing and fall back to raw-JSON matching."""
        patch_exec(_FakeProcess([self._tool_event("bash"), self._tool_event("write")]))
        record = await _run(_agent(), tmp_path)
        assert [c.tool_name for c in record.commands] == ["Bash", "Write"]

    async def test_unknown_tool_passes_through(self, patch_exec, tmp_path):
        """An unmapped tool still surfaces under its own name rather than vanishing."""
        patch_exec(_FakeProcess([self._tool_event("some_new_tool")]))
        record = await _run(_agent(), tmp_path)
        assert [c.tool_name for c in record.commands] == ["some_new_tool"]


class TestUnsupportedConfigIsAnnounced:
    async def test_start_warns_about_unenforced_fields(self, patch_exec, tmp_path, caplog):
        """`experiments/default.yaml` sets allowed_tools on every task; the CLI has no
        equivalent knob, so silence would let a task believe it was constrained."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        with caplog.at_level("WARNING"):
            await _agent(allowed_tools=["Bash"], system_prompt="be terse").start(str(tmp_path))
        assert "allowed_tools" in caplog.text
        assert "system_prompt" in caplog.text

    async def test_no_warning_when_nothing_is_dropped(self, patch_exec, tmp_path, caplog):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        with caplog.at_level("WARNING"):
            await _agent().start(str(tmp_path))
        assert "NOT enforced" not in caplog.text


class TestArgvConstruction:
    async def test_defaults_include_auto_and_pure(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)

        argv = captured["argv"]
        assert argv[:4] == ["opencode", "run", "--format", "json"]
        assert "--auto" in argv
        assert "--pure" in argv
        assert argv[argv.index("-m") + 1] == "deepseek/deepseek-v4-pro"
        assert argv[-1] == "do the thing"

    async def test_plan_mode_withholds_auto(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(permission_mode=PermissionMode.PLAN), tmp_path)
        assert "--auto" not in captured["argv"]

    async def test_variant_and_pure_off(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(variant="high", pure=False), tmp_path)

        argv = captured["argv"]
        assert argv[argv.index("--variant") + 1] == "high"
        assert "--pure" not in argv

    async def test_explicit_line_limit_is_passed(self, patch_exec, tmp_path):
        """A large tool result must not blow StreamReader's default 64 KiB cap."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["limit"] > 64 * 1024


class TestSessionContinuity:
    async def test_second_turn_resumes_session(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await _run(agent, tmp_path)
        assert agent._session_id == SESSION

        captured2 = patch_exec(_FakeProcess(HAPPY_STREAM))
        await agent.communicate("follow up")
        argv = captured2["argv"]
        assert argv[argv.index("--session") + 1] == SESSION


class TestFailurePaths:
    async def test_error_event_raises_and_parks_partial(self, patch_exec, tmp_path):
        stream = [
            _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
            _evt(
                "tool_use",
                {
                    "id": "prt_2",
                    "messageID": "msg_1",
                    "type": "tool",
                    "tool": "bash",
                    "callID": "call_1",
                    "state": {"status": "running", "input": {"command": "ls"}},
                },
            ),
            json.dumps(
                {
                    "type": "error",
                    "sessionID": SESSION,
                    "error": {"name": "UnknownError", "data": {"message": "provider exploded"}},
                }
            ),
        ]
        patch_exec(_FakeProcess(stream))
        agent = _agent()

        with pytest.raises(AgentCrashError, match="provider exploded"):
            await _run(agent, tmp_path)

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        # The in-flight tool was force-closed rather than dropped.
        assert [c.result_status for c in partial.commands] == ["unknown"]

    async def test_nonzero_exit_without_error_event_crashes(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess([], returncode=1, stderr=b"boom: bad model"))
        with pytest.raises(AgentCrashError, match="boom: bad model"):
            await _run(_agent(), tmp_path)

    async def test_malformed_line_is_skipped(self, patch_exec, tmp_path):
        """Non-JSON noise on stdout must not kill the turn."""
        stream = ["warn: CPU lacks AVX support", *HAPPY_STREAM]
        patch_exec(_FakeProcess(stream))
        record = await _run(_agent(), tmp_path)
        assert record.crashed is False
        assert record.assistant_turn_count == 2

    async def test_missing_cli_is_actionable(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(RuntimeError, match="npm install -g opencode-ai"):
            await _agent().start(str(tmp_path))


class TestZeroTelemetryIsLoud:
    """A clean exit that recognized no events must crash, not score.

    An earlier version of this harness parsed the `session.next.*` server
    vocabulary instead of the CLI's and reported SUCCESS 1.0 with zero turns,
    zero tokens and zero cost — indistinguishable from a real pass in every
    aggregate. Vocabulary drift must be an ERROR, not a quiet empty success.
    """

    async def test_unrecognized_vocabulary_crashes_and_names_the_types(self, patch_exec, tmp_path):
        stream = [
            json.dumps(
                {
                    "id": "evt_1",
                    "type": "session.next.step.ended",
                    "properties": {"sessionID": SESSION, "tokens": {"input": 100, "output": 20}},
                }
            ),
            json.dumps({"id": "evt_2", "type": "session.next.idle", "properties": {"sessionID": SESSION}}),
        ]
        patch_exec(_FakeProcess(stream))
        agent = _agent()

        with pytest.raises(AgentCrashError, match="no recognized events") as exc:
            await _run(agent, tmp_path)
        # The crash names what it DID see, for diagnosis.
        assert "session.next.step.ended" in str(exc.value)

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True

    async def test_empty_stdout_with_clean_exit_crashes(self, patch_exec, tmp_path):
        """Zero events at all is the same zero-telemetry hole as wrong vocabulary."""
        patch_exec(_FakeProcess([], returncode=0))
        with pytest.raises(AgentCrashError, match="no recognized events"):
            await _run(_agent(), tmp_path)

    async def test_intentional_cuts_are_exempt(self, patch_exec, tmp_path):
        """A cooperative stop can land before the first recognized event; that is
        an intentional cut, not vocabulary drift."""
        stream = [json.dumps({"id": "evt_1", "type": "session.next.idle", "properties": {"sessionID": SESSION}})]
        proc = _RunningProcess(stream)
        patch_exec(proc)
        record = await _run(_agent(), tmp_path, should_stop=lambda: True)
        assert record.crashed is False


class _ExplodingProcess(_FakeProcess):
    """Replays events, then raises from ``readline`` mid-stream.

    Stands in for everything the turn loop does not anticipate — most concretely
    ``StreamReader.readline`` raising ``ValueError`` on a line past ``limit``.
    """

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        raise ValueError("Separator is not found, and chunk exceed the limit")


class _EventRecorder:
    """Minimal ``StreamCallback``: records every event the agent emits."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


class TestUnexpectedErrorContract:
    """An unanticipated exception must still honor the pending-turn contract.

    Escaping raw would break it three ways: no terminal ``AgentEndEvent`` (an
    unbalanced event tree for every renderer), captured telemetry dropped instead
    of parked on ``pending_turn``, and ``_iteration`` left incremented because the
    orchestrator never reaches ``discard_pending_turn``.
    """

    async def test_stream_error_becomes_a_crash_with_partial_parked(self, patch_exec, tmp_path):
        stream = [
            _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
            _evt(
                "tool_use",
                {
                    "id": "prt_2",
                    "messageID": "msg_1",
                    "type": "tool",
                    "tool": "bash",
                    "callID": "call_1",
                    "state": {"status": "running", "input": {"command": "ls"}},
                },
            ),
        ]
        patch_exec(_ExplodingProcess(stream))
        agent = _agent()

        with pytest.raises(AgentCrashError, match="OpenCode turn failed"):
            await _run(agent, tmp_path)

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        # Telemetry captured before the failure survives, orphan tool force-closed.
        assert [c.result_status for c in partial.commands] == ["unknown"]

    async def test_spawn_failure_becomes_a_crash(self, monkeypatch, tmp_path):
        """A failure before the first byte (OSError from the spawn) is still a crash."""

        async def boom(*_argv: str, **_kwargs: Any):
            raise OSError("no fork for you")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/opencode")

        with pytest.raises(AgentCrashError, match="no fork for you"):
            await _run(_agent(), tmp_path)

    async def test_terminal_event_is_emitted_exactly_once(self, patch_exec, tmp_path):
        """The protocol allows exactly one AgentEnd per communicate(), crash included."""
        patch_exec(_ExplodingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})]))
        recorder = _EventRecorder()

        with pytest.raises(AgentCrashError):
            await _run(_agent(), tmp_path, stream_callback=recorder)

        seen = recorder.events
        assert len([e for e in seen if isinstance(e, AgentStartEvent)]) == 1
        ends = [e for e in seen if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].crashed is True
        assert ends[0].status is AgentEndStatus.CRASHED

    async def test_iteration_rolls_back_after_the_crash(self, patch_exec, tmp_path):
        """`discard_pending_turn` must find the bump it needs to undo."""
        patch_exec(_ExplodingProcess([]))
        agent = _agent()

        with pytest.raises(AgentCrashError):
            await _run(agent, tmp_path)
        assert agent._iteration == 1
        await agent.discard_pending_turn()
        assert agent._iteration == 0
        assert agent.pending_turn is None


class _LeakyPipeProcess(_FakeProcess):
    """Replays events, then never signals EOF — the real CLI's behavior.

    ``opencode run`` leaves a local server child holding the inherited stdout
    pipe open, so after the CLI exits ``readline()`` blocks forever instead of
    returning b"". The agent must fall back to a bounded drain rather than hang
    until the turn deadline.
    """

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self.returncode = self._final_returncode  # process reaped...
        await asyncio.sleep(3600)  # ...but the pipe stays open
        return b""

    async def read(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class TestLeakedPipeDrain:
    async def test_completes_without_eof(self, patch_exec, tmp_path):
        """A stdout pipe that never closes must not stall the turn."""
        patch_exec(_LeakyPipeProcess(HAPPY_STREAM))
        record = await asyncio.wait_for(_run(_agent(), tmp_path, timeout=300), timeout=30)

        assert record.crashed is False
        assert record.assistant_turn_count == 2
        assert record.agent_output == "Created the file."


class _StderrBackpressureProcess(_FakeProcess):
    """Models the two-pipe deadlock: the child makes no progress until stderr is read.

    A real CLI that fills the ~64 KiB stderr pipe blocks on write, so it emits no
    further stdout and never exits. Reading stderr only after the stdout loop ends
    therefore hangs the turn to its deadline.
    """

    def __init__(self, lines: list[str], **kwargs: Any) -> None:
        super().__init__(lines, **kwargs)
        self._stderr_read = asyncio.Event()

    async def readline(self) -> bytes:
        await self._stderr_read.wait()
        return await super().readline()

    async def read(self) -> bytes:
        self._stderr_read.set()
        return self._stderr


class TestStderrIsDrainedConcurrently:
    async def test_turn_completes_under_stderr_backpressure(self, patch_exec, tmp_path):
        patch_exec(_StderrBackpressureProcess(HAPPY_STREAM, stderr=b"noisy"))
        # Bounded so a regression fails here instead of hanging the suite.
        record = await asyncio.wait_for(_run(_agent(), tmp_path, timeout=300), timeout=10)
        assert record.assistant_turn_count == 2
        assert record.crashed is False


class TestCooperativeStop:
    def test_capability_flag_is_declared(self):
        assert OpenCodeAgent.supports_cooperative_stop is True

    async def test_should_stop_ends_turn_cleanly(self, patch_exec, tmp_path):
        """A live subprocess must be torn down, and the turn must not be a crash."""
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        record = await _run(_agent(), tmp_path, should_stop=lambda: True)

        assert record.crashed is False
        assert proc.terminated is True
        # Stopped at the first event boundary rather than draining the stream.
        assert record.assistant_turn_count < 2

    async def test_max_turns_marks_exhausted(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _run(_agent(), tmp_path, max_turns=1)
        assert record.max_turns_exhausted is True


class _HangingProcess(_FakeProcess):
    """Emits nothing and never exits until it is signaled.

    Models a CLI stuck mid-turn (a wedged provider call): no stdout, no exit —
    the shape that must be cut by the turn deadline, not waited out.
    """

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
        self._exited.set()


class _EofNoExitProcess(_HangingProcess):
    """Replays its lines, signals EOF — but never exits until killed.

    Models a CLI that closed its stream during shutdown and then wedged: the one
    window where the read loop is already done, so only a bounded reap in
    ``_settle_turn`` stands between the turn and an unbounded hang.
    """

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""  # EOF — but the process is still alive


class TestTimeoutContract:
    async def test_deadline_raises_turn_timeout_with_partial_parked(self, patch_exec, tmp_path):
        """A wedged CLI must yield TurnTimeoutError + a crashed partial record,
        with exactly one terminal AgentEndEvent (status TIMEOUT) emitted."""
        proc = _HangingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)
        agent = _agent()
        recorder = _EventRecorder()

        with pytest.raises(TurnTimeoutError):
            await _run(agent, tmp_path, timeout=0.2, stream_callback=recorder)

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        assert proc.terminated is True  # the CLI was torn down, not abandoned
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].status is AgentEndStatus.TIMEOUT

        await agent.discard_pending_turn()
        assert agent._iteration == 0  # the failed turn's bump was rolled back

    async def test_eof_without_exit_hits_the_deadline(self, patch_exec, tmp_path):
        """Stream closed, process wedged: the post-EOF reap must be bounded by the
        turn deadline instead of waiting for an exit that never comes."""
        proc = _EofNoExitProcess(HAPPY_STREAM)
        patch_exec(proc)
        agent = _agent()

        with pytest.raises(TurnTimeoutError):
            await asyncio.wait_for(_run(agent, tmp_path, timeout=0.3), timeout=10)

        # Everything parsed before the wedge survives on the partial record.
        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        assert partial.token_usage is not None
        assert partial.token_usage.output_tokens > 0

    async def test_eof_without_exit_and_no_deadline_crashes(self, patch_exec, monkeypatch, tmp_path):
        """With no turn deadline configured, the reap still gets a fixed grace —
        a stream-closed-but-wedged CLI is a crash, not an indefinite hang."""
        monkeypatch.setattr("coder_eval.agents.opencode_agent._TERM_GRACE_SECONDS", 0.1)
        proc = _EofNoExitProcess(HAPPY_STREAM)
        patch_exec(proc)

        with pytest.raises(AgentCrashError, match="did not exit"):
            await asyncio.wait_for(_run(_agent(), tmp_path), timeout=10)


class TestExternalCancel:
    async def test_cancel_parks_partial_and_reraises(self, patch_exec, tmp_path):
        """The watchdog's CancelledError must not swallow captured telemetry: the
        partial record is parked, the terminal event says CRASHED, and the
        cancellation still propagates."""
        proc = _HangingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)
        agent = _agent()
        await agent.start(str(tmp_path))
        recorder = _EventRecorder()

        task = asyncio.ensure_future(agent.communicate("do the thing", stream_callback=recorder))
        await asyncio.sleep(0.05)  # let it spawn and read the first event
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        partial = agent.pending_turn
        assert partial is not None
        assert partial.crashed is True
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].status is AgentEndStatus.CRASHED
        assert ends[0].crash_reason == "turn cancelled"


class TestProcessGroupTeardown:
    async def test_spawn_uses_its_own_session(self, patch_exec, tmp_path):
        """Each invocation must be its own process group, so killpg can reap the
        server child without touching anything this invocation didn't spawn."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["start_new_session"] is (os.name == "posix")

    async def test_stop_sweeps_the_spawned_group(self, patch_exec, tmp_path):
        """`opencode run` leaves a server child holding the pipes; stop() must
        SIGKILL the whole group or every task in a batch leaks one."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await _run(agent, tmp_path)
        assert captured["killpg"] == []  # a clean turn does not kill mid-run state

        await agent.stop()
        assert (4242, signal.SIGKILL) in captured["killpg"]

    async def test_cooperative_stop_sweeps_the_group_too(self, patch_exec, tmp_path):
        captured = patch_exec(_RunningProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path, should_stop=lambda: True)
        assert (4242, signal.SIGKILL) in captured["killpg"]

    async def test_kill_sync_signals_pid_and_group(self, patch_exec, monkeypatch, tmp_path):
        """kill_sync runs on the watchdog's non-asyncio thread: plain os.kill on
        the CLI plus a group sweep, no awaits."""
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
    @staticmethod
    def _failing_tool(error: str) -> str:
        return _evt(
            "tool_use",
            {
                "id": "prt_2",
                "messageID": "msg_1",
                "type": "tool",
                "tool": "bash",
                "callID": "call_1",
                "state": {"status": "error", "input": {"command": "ls /root"}, "error": error},
            },
        )

    async def test_tool_error_is_captured_not_dropped(self, patch_exec, tmp_path):
        recorder = _EventRecorder()
        patch_exec(_FakeProcess([self._failing_tool("boom: command exploded")]))
        record = await _run(_agent(), tmp_path, stream_callback=recorder)

        [cmd] = record.commands
        assert cmd.result_status == "error"
        assert cmd.error_message == "boom: command exploded"
        [end] = [e for e in recorder.events if isinstance(e, ToolEndEvent)]
        assert end.status is ToolEndStatus.ERROR

    async def test_permission_denial_gets_its_own_status(self, patch_exec, tmp_path):
        recorder = _EventRecorder()
        patch_exec(_FakeProcess([self._failing_tool("Permission denied by policy")]))
        record = await _run(_agent(), tmp_path, stream_callback=recorder)

        [cmd] = record.commands
        assert cmd.result_status == "error"  # the persisted tri-state folds both
        [end] = [e for e in recorder.events if isinstance(e, ToolEndEvent)]
        assert end.status is ToolEndStatus.PERMISSION_DENIED

    def test_orphan_result_is_never_dropped(self):
        """A result with no matching call still surfaces as an `unknown` tool."""
        state = _OpenCodeTurnState(task_id="t", iteration=1, user_input="x", model=None)
        events: list[Any] = []
        state.bind(events.append)

        state._close_tool("ghost", status=ToolEndStatus.UNRESOLVED, summary=None, error="no result observed")

        [event] = events
        assert isinstance(event, ToolEndEvent)
        assert event.tool.tool_name == "unknown"
        assert event.tool.result_status == "unknown"
        assert event.tool.error_message == "no result observed"
