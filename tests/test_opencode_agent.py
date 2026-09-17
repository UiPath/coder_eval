"""Tests for the OpenCode agent harness.

The CLI is never invoked: ``asyncio.create_subprocess_exec`` is patched with a
fake process that replays a newline-delimited JSON event stream, so the whole
reduction path (nd-JSON -> standardized events -> ``TurnRecord``) is exercised
offline and without credentials. Timing cases drive ``_OpenCodeDecoder``
directly through ``coder_eval.testing.replay`` under ``cli_epoch_ms``: the
windows come from the scripted envelope stamps, never from the clock.

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coder_eval.agents import opencode_agent as agent_module
from coder_eval.agents.opencode_agent import (
    OpenCodeAgent,
    _OpenCodeDecoder,
    _unwrap,
)
from coder_eval.errors.agent import format_timeout_reason
from coder_eval.models import (
    AssistantMessage,
    FileExistsCriterion,
    OpenCodeAgentConfig,
    PermissionMode,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
    TimingBasis,
    TurnRecord,
    parse_agent_config,
)
from coder_eval.orchestration.plugin_staging import stage_plugins
from coder_eval.orchestration.turn_monitor import TurnMonitor
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.emitter import TurnOutcome
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StopReason,
    StreamEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.testing import Replay, ScriptedClock, Tick, assert_identity_closes, assert_stream_balanced, replay
from tests._fixtures.golden_streams.opencode_fixtures import (
    _T0_MS,
    CAPTURED_STREAM,
    HAPPY_STREAM,
    SESSION,
    _evt,
    _FakeProcess,
    _RunningProcess,
    _tokens,
)


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
        # raising=False: os.killpg does not exist on Windows, where the sweep is a
        # no-op — the stub must still install so the fixture works on every platform.
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: captured["killpg"].append((pgid, sig)), raising=False)
        return captured

    return _install


async def _run(
    agent: OpenCodeAgent, tmp_path: Any, prompt: str = "do the thing", *, plugin_root: Path | None = None, **kwargs: Any
) -> TurnOutcome:
    await agent.start(str(tmp_path), plugin_root=plugin_root)
    kwargs.setdefault("iteration", 1)
    return await agent.communicate(prompt, **kwargs)


async def _record(agent: OpenCodeAgent, tmp_path: Any, prompt: str = "do the thing", **kwargs: Any) -> TurnRecord:
    """The record of a turn that must not end CRASHED or TIMEOUT."""
    outcome = await _run(agent, tmp_path, prompt, **kwargs)
    assert outcome.error is None, f"{outcome.status}: {outcome.error}"
    return outcome.record


def _agent(**overrides: Any) -> OpenCodeAgent:
    config = OpenCodeAgentConfig(type="opencode", **{"model": "deepseek/deepseek-v4-pro", **overrides})
    return OpenCodeAgent(config, task_id="t1")


_BASE = datetime.fromtimestamp(_T0_MS / 1000)


def _at(ms: float) -> datetime:
    """The naive-local instant of the CLI stamp ``_T0_MS + ms``, as the decoder converts it."""
    return datetime.fromtimestamp((_T0_MS + ms) / 1000)


def _event(event_type: str, part: dict[str, Any] | None = None, *, at_ms: int | None = 0) -> dict[str, Any]:
    """One decoded CLI event; ``at_ms=None`` drops the envelope ``timestamp``."""
    event = json.loads(_evt(event_type, part or {}, at_ms=at_ms or 0))
    if at_ms is None:
        del event["timestamp"]
    return event


def _tool(call_id: str, status: str, *, at_ms: int, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    """A ``bash`` ``tool_use`` event whose ``state.time`` carries the given bounds, in ms after ``_T0_MS``."""
    times = {key: _T0_MS + ms for key, ms in (("start", start), ("end", end)) if ms is not None}
    state: dict[str, Any] = {"status": status, "input": {"command": "ls"}}
    if times:
        state["time"] = times
    return _event("tool_use", {"callID": call_id, "tool": "bash", "state": state}, at_ms=at_ms)


def _finish(at_ms: int | None) -> dict[str, Any]:
    return _event("step_finish", {"reason": "stop", "tokens": {"input": 10, "output": 5}}, at_ms=at_ms)


def _replay(
    stream: list[Any], *, status: AgentEndStatus = AgentEndStatus.COMPLETED, reason: str | None = None
) -> tuple[Replay, _OpenCodeDecoder]:
    """Drive an `_OpenCodeDecoder` under `cli_epoch_ms` on a clock at `_BASE`; return the decoder too."""
    decoders: list[_OpenCodeDecoder] = []

    def end(decoder: _OpenCodeDecoder) -> TurnOutcome:
        decoders.append(decoder)
        return decoder.end(status, reason=reason)

    result = replay(stream, _OpenCodeDecoder, clock=ScriptedClock(_BASE), basis=TimingBasis.CLI_EPOCH_MS, end=end)
    return result, decoders[0]


def _assistants(result: Replay) -> list[AssistantMessage]:
    return [m for m in result.record.messages if isinstance(m, AssistantMessage)]


class TestEnvelopeNormalization:
    def test_part_envelope(self):
        """Normal events carry their payload under `part`."""
        t, part, stamp = _unwrap({"type": "step_finish", "sessionID": "s", "part": {"reason": "stop"}})
        assert t == "step_finish"
        assert part["reason"] == "stop"
        assert stamp is None

    def test_flat_envelope(self):
        """The CLI's own error path emits a flat object with no `part`."""
        t, props, _ = _unwrap({"type": "error", "sessionID": "s", "error": {"name": "UnknownError"}})
        assert t == "error"
        assert props["error"]["name"] == "UnknownError"

    def test_the_envelope_stamp_is_returned(self):
        """The envelope `timestamp` (epoch ms) is the CLI's own stamp for the event."""
        _, _, stamp = _unwrap({"type": "text", "timestamp": _T0_MS + 250, "part": {"text": "hi"}})
        assert stamp == _at(250)


class TestHappyPath:
    async def test_builds_turn_record(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        outcome = await _run(_agent(), tmp_path)
        record = outcome.record

        assert outcome.status is AgentEndStatus.COMPLETED
        assert record.crashed is False
        assert record.agent_output == "Created the file."
        assert record.assistant_turn_count == 2
        assert record.model_used == "deepseek/deepseek-v4-pro"

    async def test_the_result_summary_carries_the_final_reply(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _record(_agent(), tmp_path)

        assert record.result_summary is not None
        assert record.result_summary.is_error is False
        assert record.result_summary.stop_reason == "stop"
        assert record.result_summary.result == "Created the file."

    async def test_events_carry_no_session_thread_id(self, patch_exec, tmp_path):
        """The session id is replayed via `--session`; it is not a sub-agent thread."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        recorder = _EventRecorder()
        await _run(_agent(), tmp_path, stream_callback=recorder)

        assert recorder.events
        assert all(e.thread_id is None for e in recorder.events)
        assert_stream_balanced(recorder.events)

    async def test_token_buckets_accumulate_across_steps(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _record(_agent(), tmp_path)

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
        record = await _record(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert sum(m.input_tokens for m in record.messages) == usage.uncached_input_tokens
        assert sum(m.output_tokens for m in record.messages) == usage.output_tokens
        assert sum(m.cache_creation_tokens for m in record.messages) == usage.cache_creation_input_tokens
        assert sum(m.cache_read_tokens for m in record.messages) == usage.cache_read_input_tokens

    async def test_tool_call_captured(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _record(_agent(), tmp_path)

        assert len(record.commands) == 1
        cmd = record.commands[0]
        # Normalized to the canonical vocabulary criteria are written against.
        assert cmd.tool_name == "Read"
        assert cmd.tool_id == "call_1"
        assert cmd.sequence_number == 0
        assert cmd.result_status == "success"
        # ...including the ARGUMENT keys: the fixture's native `filePath` is
        # recorded under Claude's `file_path` (see TestCrossHarnessNormalization).
        assert cmd.parameters == {"file_path": "main.py"}
        assert cmd.result_summary == "print('hi')"
        # Duration comes from state.time, not our parse instant (17ms in fixture).
        assert cmd.duration_ms == pytest.approx(17, abs=1)

    async def test_messages_attributed_to_steps(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _record(_agent(), tmp_path)

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
            record = await _record(_agent(), tmp_path)

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
            record = await _record(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 95  # (100-10-5) + (50-40)
        assert "unexpected token accounting" not in caplog.text

    async def test_total_matching_neither_convention_warns(self, patch_exec, tmp_path, caplog):
        """nested=350, flat=8030, reported 8000 — the schema moved; keep `input`."""
        patch_exec(_FakeProcess([self._step({"total": 8000, "input": 300, "output": 50, "cache": {"read": 7680}})]))
        with caplog.at_level("WARNING"):
            record = await _record(_agent(), tmp_path)

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
            record = await _record(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 500
        assert usage.cache_read_input_tokens == 200
        assert "tokens.total is missing" in caplog.text

    async def test_missing_total_without_cache_traffic_is_silent(self, patch_exec, tmp_path, caplog):
        """No arbiter but no cache either ⇒ the conventions agree; nothing to verify."""
        patch_exec(_FakeProcess([self._step({"input": 500, "output": 20})]))
        with caplog.at_level("WARNING"):
            record = await _record(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 500
        assert "unexpected token accounting" not in caplog.text

    async def test_nested_total_contradicted_by_small_input_warns(self, patch_exec, tmp_path, caplog):
        """`total` says nested but input < cache: self-contradictory; keep `input`."""
        patch_exec(_FakeProcess([self._step({"total": 350, "input": 300, "output": 50, "cache": {"read": 7680}})]))
        with caplog.at_level("WARNING"):
            record = await _record(_agent(), tmp_path)

        usage = record.token_usage
        assert usage is not None
        assert usage.uncached_input_tokens == 300
        assert "nest inside input" in caplog.text


class TestCostFallsBackToTheRateCard:
    async def test_stream_cost_wins_when_reported(self, patch_exec, tmp_path):
        """The provider's own accounting beats a static headline rate."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _record(_agent(), tmp_path)
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
        record = await _record(_agent(), tmp_path)

        assert record.token_usage is not None
        expected = calculate_cost("deepseek/deepseek-v4-pro", uncached_input_tokens=1000, output_tokens=500)
        assert expected is not None and expected > 0
        assert record.token_usage.total_cost_usd == pytest.approx(expected)

    async def test_unpriced_model_reports_no_cost(self, patch_exec, tmp_path):
        """`None` (not 0.0) so "unpriceable" stays distinct from "ran for free"."""
        patch_exec(_FakeProcess([_evt("step_finish", {"id": "p", "reason": "stop", "tokens": _tokens(10, 5)})]))
        record = await _record(_agent(model="nowhere/not-a-real-model"), tmp_path)
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
        with caplog.at_level("DEBUG", logger="coder_eval.pricing"):
            record = await _record(_agent(), tmp_path)

        expected = calculate_cost("deepseek/deepseek-v4-pro", uncached_input_tokens=1000, output_tokens=500)
        assert expected is not None and expected > 0
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(expected)
        assert "using the rate card" in caplog.text

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
        record = await _record(_agent(model="nowhere/not-a-real-model"), tmp_path)
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
        record = await _record(_agent(), tmp_path)
        assert [c.tool_name for c in record.commands] == ["Bash", "Write"]

    async def test_gpt_family_apply_patch_maps_to_write(self, patch_exec, tmp_path):
        """OpenCode's tool set is provider-specific, so the vocabulary varies by
        MODEL within this one harness.

        A live 174-task run showed DeepSeek using write/edit 199 times and
        apply_patch 0, while GPT-5.6 used apply_patch 120 times and write/edit 0.
        Unmapped, every `tool_name: Write` / `tool_name: Edit` criterion scores 0
        on a GPT-family model that edited the file correctly — the suite in that
        run carries 33 Write and 30 Edit criteria. Mirrors codex_agent's
        `_TOOL_ITEM_NAMES["apply_patch"] = "Write"`.
        """
        patch_exec(_FakeProcess([self._tool_with_input("apply_patch", {"patchText": "*** Begin Patch\n"})]))
        record = await _record(_agent(), tmp_path)
        assert [c.tool_name for c in record.commands] == ["Write"]

    async def test_unknown_tool_passes_through(self, patch_exec, tmp_path):
        """An unmapped tool still surfaces under its own name rather than vanishing."""
        patch_exec(_FakeProcess([self._tool_event("some_new_tool")]))
        record = await _record(_agent(), tmp_path)
        assert [c.tool_name for c in record.commands] == ["some_new_tool"]

    async def test_native_skill_tool_maps_to_canonical_skill(self, patch_exec, tmp_path):
        """`skill_triggered` keys on the canonical `Skill`; OpenCode emits lowercase
        `skill`, so without the mapping a real engagement scores as a miss."""
        patch_exec(_FakeProcess([self._tool_event("skill")]))
        record = await _record(_agent(), tmp_path)
        assert [c.tool_name for c in record.commands] == ["Skill"]

    @staticmethod
    def _tool_with_input(tool: str, params: dict[str, Any]) -> str:
        return _evt(
            "tool_use",
            {
                "id": "prt_2",
                "messageID": "msg_1",
                "type": "tool",
                "tool": tool,
                "callID": f"call_{tool}",
                "state": {"status": "completed", "input": params, "output": "ok"},
            },
        )

    @pytest.mark.parametrize(
        ("tool", "native", "expected"),
        [
            # The CLI installed at the time of writing registers `{path, ...}` for
            # all three file tools; a 2026-08-13 capture emitted `filePath`. Both
            # spellings must land on Claude's `file_path`.
            ("read", {"path": "main.py"}, {"file_path": "main.py"}),
            ("read", {"filePath": "main.py"}, {"file_path": "main.py"}),
            ("write", {"path": "a.py", "content": "x"}, {"file_path": "a.py", "content": "x"}),
            (
                "edit",
                {"path": "a.py", "oldString": "a", "newString": "b", "replaceAll": True},
                {"file_path": "a.py", "old_string": "a", "new_string": "b", "replace_all": True},
            ),
            ("skill", {"name": "uipath-flow"}, {"skill": "uipath-flow"}),
        ],
    )
    async def test_argument_keys_map_to_canonical(self, patch_exec, tmp_path, tool, native, expected):
        """Normalizing the tool NAME is only half the job.

        `command_executed` serializes `parameters` to JSON for every tool but Bash
        (criteria/command_executed.py), so a criterion like
        `{tool_name: Read, command_pattern: 'file_path.*app\\.py'}` matches on Claude
        and scores 0 on OpenCode for identical agent behaviour.
        """
        patch_exec(_FakeProcess([self._tool_with_input(tool, native)]))
        record = await _record(_agent(), tmp_path)
        assert record.commands[0].parameters == expected

    @pytest.mark.parametrize(
        ("tool", "native"),
        [
            ("bash", {"command": "pytest -q"}),  # already canonical
            ("grep", {"pattern": "x", "path": "src"}),  # `path` is Claude's key here too
            ("list", {"path": "src"}),
            ("some_new_tool", {"whatever": 1}),  # unmapped tool: untouched
        ],
    )
    async def test_already_canonical_keys_are_left_alone(self, patch_exec, tmp_path, tool, native):
        """The rename is per-tool: `path` means `file_path` on Read/Write/Edit and
        stays `path` on the search tools, which is exactly Claude's split."""
        patch_exec(_FakeProcess([self._tool_with_input(tool, native)]))
        record = await _record(_agent(), tmp_path)
        assert record.commands[0].parameters == native


class TestTwoEventToolLifecycle:
    """The CLI may emit `pending`/`running` before `completed` for one callID.

    The first event routinely carries no `input` — the call is not assembled yet —
    so freezing the first event's view leaves `parameters` permanently `{}`.
    `command_executed` reads `parameters["command"]` for `tool_name: Bash`, so that
    criterion would score 0 on every row while the run looked entirely normal.
    """

    @staticmethod
    def _event(status: str, state_extra: dict[str, Any]) -> str:
        return _evt(
            "tool_use",
            {
                "id": "prt_2",
                "messageID": "msg_1",
                "type": "tool",
                "tool": "bash",
                "callID": "call_1",
                "state": {"status": status, **state_extra},
            },
        )

    async def test_the_completion_supplies_the_parameters(self, patch_exec, tmp_path):
        patch_exec(
            _FakeProcess(
                [
                    _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
                    self._event("running", {}),
                    self._event(
                        "completed",
                        {
                            "input": {"command": "pytest -q"},
                            "output": "ok",
                            "time": {"start": 1786663018214, "end": 1786663018231},
                        },
                    ),
                ]
            )
        )
        record = await _record(_agent(), tmp_path)

        assert len(record.commands) == 1  # one tool, not two
        cmd = record.commands[0]
        assert cmd.tool_name == "Bash"
        assert cmd.parameters == {"command": "pytest -q"}
        assert cmd.result_status == "success"
        assert cmd.execution_completed_at == datetime.fromtimestamp(1786663018231 / 1000.0)
        assert cmd.execution_started_at is not None, "a start that arrives only with the result still times the call"
        assert cmd.duration_ms is not None

    async def test_one_tool_start_end_pair_is_emitted(self, patch_exec, tmp_path):
        patch_exec(
            _FakeProcess(
                [
                    _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
                    self._event("pending", {}),
                    self._event("completed", {"input": {"command": "ls"}, "output": "ok"}),
                ]
            )
        )
        recorder = _EventRecorder()
        await _run(_agent(), tmp_path, stream_callback=recorder)

        assert len([e for e in recorder.events if isinstance(e, ToolStartEvent)]) == 1
        assert len([e for e in recorder.events if isinstance(e, ToolEndEvent)]) == 1

    async def test_the_completion_time_end_becomes_execution_completed_at(self, patch_exec, tmp_path):
        """The path the deleted second `state["time"]` read served.

        `on_tool_use` parsed `state.time` twice, identically, once at the top
        and again just before closing the tool. The second read is gone; this
        asserts the close still gets its `end` stamp from the same dict — and
        gets the RIGHT one, since the two events carry different times and only
        the completion's may be published.
        """
        patch_exec(
            _FakeProcess(
                [
                    _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
                    self._event("running", {"time": {"start": 1786663018214}}),
                    self._event(
                        "completed",
                        {
                            "input": {"command": "ls"},
                            "output": "ok",
                            "time": {"start": 1786663018214, "end": 1786663018231},
                        },
                    ),
                ]
            )
        )
        record = await _record(_agent(), tmp_path)

        cmd = record.commands[0]
        assert cmd.execution_completed_at == datetime.fromtimestamp(1786663018231 / 1000.0)
        assert cmd.execution_started_at is not None, "a start that arrives only with the result still times the call"
        assert cmd.duration_ms is not None
        assert cmd.execution_started_at == datetime.fromtimestamp(1786663018214 / 1000.0)
        assert cmd.duration_ms == pytest.approx(17.0)

    async def test_a_later_event_without_input_never_clears_what_we_have(self, patch_exec, tmp_path):
        """Absent evidence is not evidence of absence — the first event's args stay."""
        patch_exec(
            _FakeProcess(
                [
                    _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
                    self._event("running", {"input": {"command": "ls"}}),
                    self._event("completed", {"output": "ok"}),
                ]
            )
        )
        record = await _record(_agent(), tmp_path)
        assert record.commands[0].parameters == {"command": "ls"}


class TestFactoryContract:
    """`create_agent` calls `agent_class(config, route=route, **kwargs)` through a
    `cast(Any, ...)`, so pyright checks nothing at the call site. Every parameter
    must therefore be DECLARED here, or nothing checks it at runtime either.
    """

    def test_route_is_accepted_positionally_and_by_keyword(self):
        config = OpenCodeAgentConfig(type="opencode", model="deepseek/deepseek-v4-pro")
        assert OpenCodeAgent(config, route=None).route is None
        assert OpenCodeAgent(config, None).route is None

    def test_an_undeclared_kwarg_raises_instead_of_vanishing(self):
        """A `**_` sink would silently drop a kwarg the factory forwards by
        mistake, yielding a run with no error; a declared signature raises.
        """
        config = OpenCodeAgentConfig(type="opencode", model="deepseek/deepseek-v4-pro")
        with pytest.raises(TypeError):
            OpenCodeAgent(config, not_a_kwarg={"x-ce-run-id": "r1"})  # type: ignore[call-arg]

    def test_a_mistyped_task_id_raises_instead_of_defaulting(self):
        config = OpenCodeAgentConfig(type="opencode", model="deepseek/deepseek-v4-pro")
        with pytest.raises(TypeError):
            OpenCodeAgent(config, task_i="t1")  # type: ignore[call-arg]


class TestSandboxEnvironment:
    """`start(env_path_prepend=..., plugin_tools_dir=...)` is the abstract
    `Agent.start()` contract, and the orchestrator ALWAYS supplies both.

    The PATH prepend is the mock-shadowing contract: a task grading a mocked CLI
    (`cli_called`, invocation-log criteria) only works if the sandbox's mock
    directories resolve BEFORE the real binaries. An inverted join order leaves
    the real binary in front, so the mock writes no invocation log and every row
    scores 0 — with the whole suite still green. It must fail here instead.
    """

    async def test_prepends_mock_dirs_ahead_of_the_inherited_path(self, patch_exec, tmp_path, monkeypatch):
        """The dirs land at the FRONT of PATH, in order, with the parent appended."""
        monkeypatch.setenv("PATH", "/parent/bin")
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await agent.start(str(tmp_path), env_path_prepend=["/sandbox/mocks", "/sandbox/bins"])
        await agent.communicate("do the thing", iteration=1)

        expected = os.pathsep.join(["/sandbox/mocks", "/sandbox/bins", "/parent/bin"])
        assert captured["kwargs"]["env"]["PATH"] == expected

    async def test_plugin_tools_dir_is_exported(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.delenv("PLUGIN_TOOLS_DIR", raising=False)
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await agent.start(str(tmp_path), plugin_tools_dir="/sandbox/tools")
        await agent.communicate("do the thing", iteration=1)

        assert captured["kwargs"]["env"]["PLUGIN_TOOLS_DIR"] == "/sandbox/tools"

    async def test_inherited_plugin_tools_dir_wins(self, patch_exec, tmp_path, monkeypatch):
        """The export is advisory: a host that already set it is never overridden."""
        monkeypatch.setenv("PLUGIN_TOOLS_DIR", "/host/tools")
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await agent.start(str(tmp_path), plugin_tools_dir="/sandbox/tools")
        await agent.communicate("do the thing", iteration=1)

        assert captured["kwargs"]["env"]["PLUGIN_TOOLS_DIR"] == "/host/tools"

    async def test_neither_key_is_touched_without_the_kwargs(self, patch_exec, tmp_path, monkeypatch):
        """A start() with no sandbox contributions passes the environment through."""
        monkeypatch.setenv("PATH", "/parent/bin")
        monkeypatch.delenv("PLUGIN_TOOLS_DIR", raising=False)
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)

        env = captured["kwargs"]["env"]
        assert env["PATH"] == "/parent/bin"
        assert "PLUGIN_TOOLS_DIR" not in env

    async def test_the_host_environment_is_inherited_whole(self, patch_exec, tmp_path, monkeypatch):
        """The CLI needs the host's provider credentials (OPENROUTER_API_KEY, ...);
        this builds the full env rather than a merge dict, so nothing is dropped."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)

        assert captured["kwargs"]["env"]["OPENROUTER_API_KEY"] == "sk-test"


def _staged_root(tmp_path: Path) -> Path:
    """A plugin root staged by ``stage_plugins`` over one authored skill."""
    authored = tmp_path / "authored" / "skills" / "uipath-admin"
    authored.mkdir(parents=True)
    (authored / "SKILL.md").write_text("---\nname: uipath-admin\ndescription: d\n---\n", encoding="utf-8")
    return stage_plugins([{"type": "local", "path": str(tmp_path / "authored")}], tmp_path / "plugin_root").root


def _injected_skill_paths(captured) -> list[str]:
    raw = captured["kwargs"]["env"].get("OPENCODE_CONFIG_CONTENT")
    return [] if raw is None else json.loads(raw)["skills"]["paths"]


class TestSkillInjection:
    """The staged plugin root reaches OpenCode as a ``skills.paths`` entry in the injected config."""

    async def test_staged_root_skills_dir_is_injected(self, patch_exec, tmp_path):
        root = _staged_root(tmp_path)
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await _run(agent, tmp_path / "sandbox", plugin_root=root)
        assert _injected_skill_paths(captured) == [str(root / "skills")]
        assert "opencode_skill_paths" not in agent.get_environment_info()

    async def test_env_untouched_without_a_plugin_root(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert "OPENCODE_CONFIG_CONTENT" not in captured["kwargs"]["env"]

    async def test_inherited_config_content_is_merged_not_clobbered(self, patch_exec, tmp_path, monkeypatch):
        root = _staged_root(tmp_path)
        monkeypatch.setenv(
            "OPENCODE_CONFIG_CONTENT",
            json.dumps({"username": "host", "skills": {"paths": ["/host/skills"]}}),
        )
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path / "sandbox", plugin_root=root)

        config = json.loads(captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])
        assert config["username"] == "host"
        assert config["skills"]["paths"] == ["/host/skills", str(root / "skills")]

    @pytest.mark.parametrize("inherited", ["{ not json", '["a", "list"]', '"a string"'])
    async def test_unusable_inherited_config_is_replaced_with_a_warning(
        self, patch_exec, tmp_path, monkeypatch, caplog, inherited
    ):
        """An inherited value we cannot merge into must not cost us the skills;
        replacing it is announced so the host knows its config was dropped."""
        root = _staged_root(tmp_path)
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", inherited)
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        with caplog.at_level("WARNING"):
            await _run(_agent(), tmp_path / "sandbox", plugin_root=root)

        assert _injected_skill_paths(captured) == [str(root / "skills")]
        assert "replacing it with the injected config" in caplog.text


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

    async def test_plan_mode_keeps_auto_and_denies_writes(self, patch_exec, tmp_path):
        """`plan` is explicit denies, so the run stays unattended instead of hanging."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(permission_mode=PermissionMode.PLAN), tmp_path)
        assert "--auto" in captured["argv"]
        config = json.loads(captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])
        assert config["permission"] == {"edit": "deny", "bash": "deny"}


_NON_TOOL_ALLOWS = {"external_directory": "allow", "doom_loop": "allow"}


class TestPermissionConfig:
    @pytest.mark.parametrize(
        ("cfg", "expected"),
        [
            ({}, None),
            ({"allowed_tools": []}, None),
            ({"allowed_tools": ["Bash"]}, {"*": "deny", **_NON_TOOL_ALLOWS, "bash": "allow"}),
            ({"disallowed_tools": ["Bash"]}, {"bash": "deny"}),
            ({"permission_mode": "plan"}, {"edit": "deny", "bash": "deny"}),
            ({"allowed_tools": ["TodoWrite", "NotebookEdit"]}, {"*": "deny", **_NON_TOOL_ALLOWS, "todowrite": "allow"}),
            ({"allowed_tools": ["NotebookEdit"]}, {"*": "deny", **_NON_TOOL_ALLOWS}),
            (
                {"allowed_tools": ["Bash", "Write"], "disallowed_tools": ["Bash"]},
                {"*": "deny", **_NON_TOOL_ALLOWS, "bash": "deny", "edit": "allow"},
            ),
            (
                {"allowed_tools": ["Bash", "Read"], "permission_mode": "plan"},
                {"*": "deny", **_NON_TOOL_ALLOWS, "bash": "deny", "read": "allow", "edit": "deny"},
            ),
        ],
    )
    def test_shapes(self, cfg: dict[str, Any], expected: dict[str, str] | None):
        assert _agent(**cfg)._permission_config() == expected

    def test_permission_map_covers_the_canonical_vocabulary(self):
        from coder_eval.models import CANONICAL_TOOL_NAMES

        assert OpenCodeAgent.tool_names is not None
        assert set(OpenCodeAgent.tool_names.names) == CANONICAL_TOOL_NAMES
        assert set(agent_module._CLAUDE_TO_OPENCODE_PERMISSION) == CANONICAL_TOOL_NAMES
        assert agent_module._CLAUDE_TO_OPENCODE_PERMISSION["NotebookEdit"] == ()
        assert agent_module._CLAUDE_TO_OPENCODE_PERMISSION["Task"] == ("task",)

    def test_wildcard_deny_comes_first(self):
        assert next(iter(_agent(allowed_tools=["Read"])._permission_config() or {})) == "*"

    def test_write_shaped_tools_share_the_edit_key(self):
        assert agent_module._CLAUDE_TO_OPENCODE_PERMISSION["Write"] == ("edit",)
        assert agent_module._CLAUDE_TO_OPENCODE_PERMISSION["Edit"] == ("edit",)


class TestSystemPromptInstructions:
    async def test_prompt_reaches_the_cli_as_an_instructions_file(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent(system_prompt="be terse")
        await _run(agent, tmp_path / "sandbox")
        try:
            (prompt_file,) = json.loads(captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])["instructions"]
            assert Path(prompt_file).read_text(encoding="utf-8") == "be terse"
            assert not prompt_file.startswith(str(tmp_path / "sandbox"))
        finally:
            await agent.stop()

    async def test_stop_removes_the_prompt_dir(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent(system_prompt="be terse")
        await agent.start(str(tmp_path))
        prompt_dir = agent._prompt_dir
        assert prompt_dir is not None and os.path.isdir(prompt_dir)
        await agent.stop()
        assert not os.path.exists(prompt_dir)
        assert agent._prompt_dir is None

    async def test_inherited_wildcard_allow_cannot_outrank_our_allowlist(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"permission": {"*": "allow", "webfetch": "allow"}}))
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(allowed_tools=["Read"]), tmp_path)
        rules = list(json.loads(captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])["permission"].items())
        assert rules[0] == ("webfetch", "allow")
        assert rules[1] == ("*", "deny")

    @pytest.mark.parametrize("host_rule", ["allow", "deny"])
    async def test_an_allowlist_keeps_a_host_rule_for_a_non_tool_permission(
        self, patch_exec, tmp_path, monkeypatch, host_rule
    ):
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"permission": {"external_directory": host_rule}}))
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(allowed_tools=["Read"]), tmp_path)
        rules = list(json.loads(captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])["permission"].items())
        assert rules[0] == ("*", "deny")
        assert rules[-1] == ("external_directory", host_rule)
        assert ("doom_loop", "allow") in rules

    async def test_no_prompt_writes_no_file(self, patch_exec, tmp_path):
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await _run(agent, tmp_path)
        assert agent._prompt_dir is None
        assert "OPENCODE_CONFIG_CONTENT" not in captured["kwargs"]["env"]

    async def test_inherited_config_merges_all_three_keys(self, patch_exec, tmp_path, monkeypatch):
        root = _staged_root(tmp_path)
        monkeypatch.setenv(
            "OPENCODE_CONFIG_CONTENT",
            json.dumps(
                {
                    "skills": {"paths": ["/host/skills"]},
                    "instructions": ["/host/AGENTS.md"],
                    "permission": {"read": "deny", "bash": "allow", "webfetch": "deny"},
                }
            ),
        )
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent(system_prompt="be terse", disallowed_tools=["Bash"])
        await _run(agent, tmp_path / "sandbox", plugin_root=root)
        await agent.stop()

        config = json.loads(captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])
        assert config["skills"]["paths"] == ["/host/skills", str(root / "skills")]
        assert config["instructions"][0] == "/host/AGENTS.md"
        assert config["instructions"][-1].endswith("system_prompt.md")
        assert list(config["permission"].items()) == [("read", "deny"), ("webfetch", "deny"), ("bash", "deny")]

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

    async def test_the_cli_never_inherits_stdin(self, patch_exec, tmp_path):
        """OpenCode reads a non-TTY stdin to EOF before it emits; an inherited open stdin stalls the turn."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL


class TestSessionContinuity:
    async def test_first_turn_omits_session(self, patch_exec, tmp_path):
        """The `if self._session_id:` guard must withhold `--session` on the first
        turn; injecting it (even empty) would fork a fresh session every turn."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert "--session" not in captured["argv"]

    async def test_second_turn_resumes_session(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await _run(agent, tmp_path)
        assert agent._session_id == SESSION

        captured2 = patch_exec(_FakeProcess(HAPPY_STREAM))
        await agent.communicate("follow up", iteration=2)
        argv = captured2["argv"]
        assert argv[argv.index("--session") + 1] == SESSION


class TestEnvironmentInfo:
    def test_carries_the_system_prompt_semantics_marker(self):
        """The base contract: every agent's env-info records the regime, so a run
        is never mis-bucketed as pre-marker. OpenCode appends the system prompt
        as an `instructions` file."""
        info = _agent().get_environment_info()
        assert info["system_prompt_semantics"] == "append"
        assert info["harness_contract"] == OpenCodeAgent.contract.model_dump(mode="json")
        assert info["opencode_model"] == "deepseek/deepseek-v4-pro"
        assert info["opencode_pure"] is True

    def test_variant_recorded_when_set(self):
        assert _agent(variant="high").get_environment_info()["opencode_variant"] == "high"

    def test_variant_absent_when_unset(self):
        assert "opencode_variant" not in _agent().get_environment_info()

    async def test_session_id_recorded_after_a_turn(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        assert "opencode_session_id" not in agent.get_environment_info()
        await _run(agent, tmp_path)
        assert agent.get_environment_info()["opencode_session_id"] == SESSION


class TestErrorEventShapes:
    """`error` is the CLI's own flat envelope, and its payload shape varies."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"error": {"data": {"message": "provider refused"}, "name": "ProviderError"}}, "provider refused"),
            ({"error": {"name": "UnknownError"}}, "UnknownError"),  # no data.message -> the name
            ({"error": {"data": None, "name": "UnknownError"}}, "UnknownError"),
            ({"error": {"data": "not-a-dict", "name": "UnknownError"}}, "UnknownError"),
            ({"error": {}}, "unknown error"),
            ({"error": "plain string"}, "plain string"),
            ({}, "unknown error"),
        ],
    )
    def test_message_extraction(self, payload, expected):
        _, decoder = _replay([{"type": "error", "sessionID": SESSION, **payload}])
        assert decoder.error == expected


class TestTokenCastsNeverRaise:
    """`_handle_line` advertises "Never raises on bad input"; these five casts were
    the module's only unguarded field reads, and every neighbouring field already
    warns-and-continues on drift rather than failing.

    Raising here is expensive: `communicate`'s `except Exception` turns it into an
    AgentCrashError, categorized AGENT_CRASH with max_retries=2, so ONE mistyped
    bucket burns three full attempts and lands the task as ERROR.
    """

    @staticmethod
    def _stream(tokens: dict[str, Any]) -> list[str]:
        return [
            _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
            _evt("step_finish", {"id": "prt_2", "messageID": "msg_1", "reason": "stop", "tokens": tokens}),
        ]

    @pytest.mark.parametrize(
        "tokens",
        [
            {"input": "abc", "output": 20, "total": 120},  # ValueError on int()
            {"input": {"nested": 1}, "output": 20},  # TypeError on int()
            {"input": [5], "output": 20},  # TypeError on int()
            {"input": 100, "output": 20, "cache": {"read": "lots", "write": None}},
        ],
    )
    async def test_a_non_numeric_bucket_warns_instead_of_crashing(self, patch_exec, tmp_path, tokens, caplog):
        patch_exec(_FakeProcess(self._stream(tokens)))
        with caplog.at_level("WARNING"):
            record = await _record(_agent(), tmp_path)

        assert record.crashed is False
        assert "unexpected token accounting" in caplog.text
        # The buckets that WERE readable still land.
        assert record.token_usage is not None

    async def test_numeric_strings_are_still_accepted(self, patch_exec, tmp_path, caplog):
        """A stringly-typed but numeric count is a serialization detail, not drift."""
        patch_exec(_FakeProcess(self._stream({"input": "100", "output": "20", "total": 120})))
        with caplog.at_level("WARNING"):
            record = await _record(_agent(), tmp_path)

        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 100
        assert record.token_usage.output_tokens == 20
        assert "unexpected token accounting" not in caplog.text

    async def test_a_float_count_truncates(self, patch_exec, tmp_path, caplog):
        """JSON has one number type, so a provider may serialize a count as 100.0."""
        patch_exec(_FakeProcess(self._stream({"input": 100.0, "output": 20.7, "total": 120})))
        with caplog.at_level("WARNING"):
            record = await _record(_agent(), tmp_path)

        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 100
        assert record.token_usage.output_tokens == 20
        assert "unexpected token accounting" not in caplog.text

    async def test_a_bool_is_not_a_token_count(self, patch_exec, tmp_path, caplog):
        """`int(True) == 1` would book a phantom token."""
        patch_exec(_FakeProcess(self._stream({"input": True, "output": 20})))
        with caplog.at_level("WARNING"):
            record = await _record(_agent(), tmp_path)

        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 0
        assert "unexpected token accounting" in caplog.text


_ERROR_LINE = json.dumps(
    {
        "type": "error",
        "sessionID": SESSION,
        "error": {"name": "UnknownError", "data": {"message": "provider exploded"}},
    }
)


class TestFailurePaths:
    async def test_error_event_then_clean_exit_crashes_with_the_clis_message(self, patch_exec, tmp_path):
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
            _ERROR_LINE,
        ]
        patch_exec(_FakeProcess(stream, returncode=0))
        agent = _agent()

        outcome = await _run(agent, tmp_path)

        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error == "OpenCode error: provider exploded"
        partial = outcome.record
        assert partial.crashed is True
        assert partial.result_summary is None
        # The in-flight tool was force-closed rather than dropped.
        assert [c.result_status for c in partial.commands] == ["unknown"]

    async def test_a_stream_error_crashes_even_when_a_stop_was_requested(self, patch_exec, tmp_path):
        """OpenCode's `error` event is final: an error read on the line a stop fires on is still a crash."""
        patch_exec(_RunningProcess([_ERROR_LINE]))
        outcome = await _run(_agent(), tmp_path, should_stop=lambda: StopReason.TOOL_CALL_CAP)

        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and outcome.error.startswith("OpenCode error:")

    async def test_nonzero_exit_without_error_event_crashes(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess([], returncode=1, stderr=b"boom: bad model"))
        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error == "OpenCode exited non-zero: boom: bad model"

    async def test_malformed_line_is_skipped(self, patch_exec, tmp_path):
        """Non-JSON noise on stdout must not kill the turn."""
        stream = ["warn: CPU lacks AVX support", *HAPPY_STREAM]
        patch_exec(_FakeProcess(stream))
        record = await _record(_agent(), tmp_path)
        assert record.crashed is False
        assert record.assistant_turn_count == 2

    async def test_missing_cli_is_actionable(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(RuntimeError, match="npm install -g opencode-ai"):
            await _agent().start(str(tmp_path))


class TestZeroTelemetryIsLoud:
    """A clean exit that captured no token telemetry must crash, not score.

    An earlier version of this harness parsed the `session.next.*` server
    vocabulary instead of the CLI's and reported SUCCESS 1.0 with zero turns,
    zero tokens and zero cost — indistinguishable from a real pass in every
    aggregate. Drift must be an ERROR, not a quiet empty success.

    The guard keys on the TELEMETRY, not the event vocabulary: recognizing the
    event names is not the property worth protecting, and checking them alone
    left the identical outcome reachable one layer down (see
    `test_finished_step_without_tokens_crashes`).
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

        outcome = await _run(agent, tmp_path)

        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "no recognized events" in outcome.error
        # The crash names what it DID see, for diagnosis.
        assert "session.next.step.ended" in outcome.error

        partial = outcome.record
        assert partial is not None
        assert partial.crashed is True

    async def test_empty_stdout_with_clean_exit_crashes(self, patch_exec, tmp_path):
        """Zero events at all is the same zero-telemetry hole as wrong vocabulary."""
        patch_exec(_FakeProcess([], returncode=0))
        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None
        assert outcome.error.startswith("OpenCode exited cleanly but the turn captured no recognized events.")

    async def test_intentional_cuts_are_exempt(self, patch_exec, tmp_path):
        """A cooperative stop can land before the first recognized event; that is
        an intentional cut, not vocabulary drift."""
        stream = [json.dumps({"id": "evt_1", "type": "session.next.idle", "properties": {"sessionID": SESSION}})]
        proc = _RunningProcess(stream)
        patch_exec(proc)
        record = await _record(_agent(), tmp_path, should_stop=lambda: StopReason.EARLY_CRITERION)
        assert record.crashed is False

    @staticmethod
    def _stream_without_tokens(**finish_extra: Any) -> list[str]:
        """HAPPY_STREAM's shape with the `tokens` key absent from every step."""
        return [
            _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
            _evt("text", {"id": "prt_2", "messageID": "msg_1", "type": "text", "text": "Done."}),
            _evt("step_finish", {"id": "prt_3", "messageID": "msg_1", "reason": "stop", **finish_extra}),
        ]

    async def test_finished_step_without_tokens_crashes(self, patch_exec, tmp_path):
        """The event vocabulary is fine and three events are recognized — but the
        turn still captured nothing.

        `EventCollector` maps an all-zero, costless `TokenUsage` to
        `token_usage=None`, so this is a COMPLETED turn a file-based criterion can
        score SUCCESS on, absent from every token aggregate, whose
        `run_limits.max_total_tokens` / `max_usd` gates could never trip no matter
        what the run really billed.
        """
        patch_exec(_FakeProcess(self._stream_without_tokens()))
        agent = _agent()

        outcome = await _run(agent, tmp_path)

        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None
        assert "zero token telemetry" in outcome.error
        assert "1 finished step(s)" in outcome.error
        assert outcome.record.crashed is True  # telemetry captured so far is still on the record
        assert len(outcome.record.messages) >= 1

    async def test_cost_without_tokens_still_crashes(self, patch_exec, tmp_path):
        """Reported cost does not excuse missing tokens: the USD gate might trip,
        but every token gate and aggregate is still silently blind."""
        patch_exec(_FakeProcess(self._stream_without_tokens(cost=0.004)))
        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "cost reported: yes" in outcome.error

    async def test_require_token_telemetry_false_warns_and_scores(self, patch_exec, tmp_path, caplog):
        """The escape hatch, for a provider that genuinely reports no usage: crashing
        every turn there would make the harness unusable, not merely imprecise."""
        patch_exec(_FakeProcess(self._stream_without_tokens()))
        with caplog.at_level("WARNING"):
            record = await _record(_agent(require_token_telemetry=False), tmp_path)

        assert record.crashed is False
        assert "require_token_telemetry is off" in caplog.text

    async def test_the_hatch_never_relaxes_the_vocabulary_check(self, patch_exec, tmp_path):
        """Vocabulary drift has silently zeroed a whole run before, and no provider
        quirk explains it — so this arm stays fatal even with the hatch open."""
        patch_exec(_FakeProcess([json.dumps({"type": "session.next.idle", "properties": {"sessionID": SESSION}})]))
        outcome = await _run(_agent(require_token_telemetry=False), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "no recognized events" in outcome.error

    async def test_a_cut_before_any_step_finished_is_exempt(self, patch_exec, tmp_path):
        """The arm keys on a step the CLI reported FINISHED. A stop landing between
        a step's start and its `step_finish` is an intentional cut, not drift."""
        proc = _RunningProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"})])
        patch_exec(proc)
        record = await _record(_agent(), tmp_path, should_stop=lambda: StopReason.EARLY_CRITERION)
        assert record.crashed is False

    async def test_real_tokens_are_never_condemned(self, patch_exec, tmp_path):
        """The guard must not fire on the ordinary path it lives beside."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _record(_agent(), tmp_path)
        assert record.crashed is False
        assert record.token_usage is not None


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
    """An unanticipated exception must still end the turn as a crashed outcome.

    Escaping raw would break it two ways: no terminal ``AgentEndEvent`` (an
    unbalanced event tree for every renderer), and captured telemetry dropped
    instead of kept on the crashed record.
    """

    async def test_stream_error_becomes_a_crash_with_the_crashed_partial(self, patch_exec, tmp_path):
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

        outcome = await _run(agent, tmp_path)

        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and outcome.error.startswith("OpenCode turn failed: ")
        partial = outcome.record
        assert partial.crashed is True
        # Telemetry captured before the failure survives, orphan tool force-closed.
        assert [c.result_status for c in partial.commands] == ["unknown"]

    async def test_spawn_failure_becomes_a_crash(self, monkeypatch, tmp_path):
        """A failure before the first byte (OSError from the spawn) is still a crash."""

        async def boom(*_argv: str, **_kwargs: Any):
            raise OSError("no fork for you")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/opencode")

        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error == "OpenCode turn failed: no fork for you"

    async def test_terminal_event_is_emitted_exactly_once(self, patch_exec, tmp_path):
        """The protocol allows exactly one AgentEnd per communicate(), crash included."""
        patch_exec(_ExplodingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})]))
        recorder = _EventRecorder()

        outcome = await _run(_agent(), tmp_path, stream_callback=recorder)
        assert outcome.status is AgentEndStatus.CRASHED

        seen = recorder.events
        assert len([e for e in seen if isinstance(e, AgentStartEvent)]) == 1
        ends = [e for e in seen if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].crashed is True
        assert ends[0].status is AgentEndStatus.CRASHED


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
        record = await asyncio.wait_for(_record(_agent(), tmp_path, timeout=300), timeout=30)

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
        record = await asyncio.wait_for(_record(_agent(), tmp_path, timeout=300), timeout=10)
        assert record.assistant_turn_count == 2
        assert record.crashed is False


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
        assert OpenCodeAgent.contract.cooperative_stop is True

    async def test_early_criterion_ends_turn_stopped_early(self, patch_exec, tmp_path):
        """A live subprocess must be torn down, and the turn must not be a crash."""
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        recorder = _EventRecorder()
        outcome = await _run(
            _agent(), tmp_path, should_stop=lambda: StopReason.EARLY_CRITERION, stream_callback=recorder
        )
        record = outcome.record

        assert outcome.status is AgentEndStatus.STOPPED_EARLY
        assert record.crashed is False
        assert proc.terminated is True
        # Stopped at the first event boundary rather than draining the stream.
        assert record.assistant_turn_count < 2
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert [e.status for e in ends] == [AgentEndStatus.STOPPED_EARLY]

    async def test_tool_call_cap_ends_turn_tool_calls_exhausted(self, patch_exec, tmp_path):
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        recorder = _EventRecorder()
        record = await _record(
            _agent(), tmp_path, should_stop=lambda: StopReason.TOOL_CALL_CAP, stream_callback=recorder
        )

        assert proc.terminated is True
        assert record.crashed is False
        assert record.tool_calls_exhausted is True
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert [e.status for e in ends] == [AgentEndStatus.TOOL_CALLS_EXHAUSTED]

    async def test_token_budget_ends_turn_token_budget_exceeded(self, patch_exec, tmp_path):
        proc = _RunningProcess(HAPPY_STREAM)
        patch_exec(proc)
        recorder = _EventRecorder()
        record = await _record(
            _agent(), tmp_path, should_stop=lambda: StopReason.TOKEN_BUDGET, stream_callback=recorder
        )

        assert proc.terminated is True
        assert record.crashed is False
        assert record.tool_calls_exhausted is False
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert [e.status for e in ends] == [AgentEndStatus.TOKEN_BUDGET_EXCEEDED]

    async def test_no_stop_is_uncapped(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        record = await _record(_agent(), tmp_path, should_stop=lambda: None)
        assert record.tool_calls_exhausted is False
        assert record.assistant_turn_count == 2

    async def test_the_deciding_step_is_kept_whole(self, patch_exec, tmp_path):
        """A stop after step 1's `step_finish` keeps step 1 complete and never opens step 2.

        Asserting only the flag would let a cut that discards the step that earned
        the stop pass — the run would report exhaustion with none of the
        telemetry that reached it.
        """
        patch_exec(_RunningProcess(HAPPY_STREAM))
        recorder = _EventRecorder()
        record = await _record(
            _agent(), tmp_path, should_stop=_stop_after(3, StopReason.TOOL_CALL_CAP), stream_callback=recorder
        )

        assert record.tool_calls_exhausted is True
        assert record.assistant_turn_count == 1
        assert len([e for e in recorder.events if isinstance(e, TurnStartEvent)]) == 1
        assert len(record.commands) == 1  # step 1's tool call
        usage = record.token_usage
        assert usage is not None
        # Step 1's buckets exactly (nested convention: 100-10-5=85 fresh input).
        assert usage.uncached_input_tokens == 85
        assert usage.output_tokens == 20
        assert usage.cache_creation_input_tokens == 5
        assert usage.cache_read_input_tokens == 10

    async def test_an_intentional_stop_is_exempt_from_a_non_zero_exit(self, patch_exec, tmp_path):
        """Killing the CLI makes it exit non-zero; that must not crash an intentional stop."""
        patch_exec(_RunningProcess(HAPPY_STREAM, returncode=-15, stderr=b"terminated"))
        record = await _record(_agent(), tmp_path, should_stop=_stop_after(3, StopReason.TOOL_CALL_CAP))
        assert record.crashed is False
        assert record.tool_calls_exhausted is True


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
        self.killed = True
        self._exited.set()


class _ExplodingRunningProcess(_HangingProcess):
    """Raises from ``readline`` mid-stream AND stays alive, like the real CLI.

    ``_ExplodingProcess`` inherits the plain fake's ``wait()``, which reports an
    exit code the instant it is awaited — so it can never model the case that
    matters for teardown: the read loop dying while the CLI is still streaming.
    """

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        raise ValueError("Separator is not found, and chunk exceed the limit")


class _EofNoExitProcess(_HangingProcess):
    """Replays its lines, signals EOF — but never exits until killed.

    Models a CLI that closed its stream during shutdown and then wedged: the one
    window where the read loop is already done, so only the bounded post-EOF
    exit wait in the transport's settle stands between the turn and an unbounded hang.
    """

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""  # EOF — but the process is still alive


class TestTimeoutContract:
    async def test_deadline_returns_a_timeout_outcome_with_the_partial(self, patch_exec, tmp_path):
        """A wedged CLI must yield a TIMEOUT outcome with a crashed partial record,
        with exactly one terminal AgentEndEvent (status TIMEOUT) emitted."""
        proc = _HangingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)
        agent = _agent()
        recorder = _EventRecorder()

        outcome = await _run(agent, tmp_path, timeout=0.2, stream_callback=recorder)

        assert outcome.status is AgentEndStatus.TIMEOUT
        assert outcome.error == format_timeout_reason(0.2)
        partial = outcome.record
        assert partial.crashed is True
        assert partial.result_summary is None
        assert proc.terminated is True  # the CLI was torn down, not abandoned
        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].status is AgentEndStatus.TIMEOUT

    async def test_eof_without_exit_hits_the_deadline(self, patch_exec, tmp_path):
        """Stream closed, process wedged: the post-EOF reap must be bounded by the
        turn deadline instead of waiting for an exit that never comes."""
        proc = _EofNoExitProcess(HAPPY_STREAM)
        patch_exec(proc)
        agent = _agent()

        outcome = await asyncio.wait_for(_run(agent, tmp_path, timeout=0.3), timeout=10)

        assert outcome.status is AgentEndStatus.TIMEOUT
        # Everything parsed before the wedge survives on the partial record.
        partial = outcome.record
        assert partial.crashed is True
        assert partial.token_usage is not None
        assert partial.token_usage.output_tokens > 0

    async def test_eof_without_exit_and_no_deadline_crashes(self, patch_exec, monkeypatch, tmp_path):
        """With no turn deadline configured, the reap still gets a fixed grace —
        a stream-closed-but-wedged CLI is a crash, not an indefinite hang."""
        monkeypatch.setattr("coder_eval.agents._transport.subprocess_jsonl._EXIT_GRACE_SECONDS", 0.1)
        proc = _EofNoExitProcess(HAPPY_STREAM)
        patch_exec(proc)

        outcome = await asyncio.wait_for(_run(_agent(), tmp_path), timeout=10)

        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None
        assert outcome.error.startswith("OpenCode closed its event stream but did not exit within")
        assert proc.terminated is True


class TestExternalCancel:
    async def test_cancel_ends_the_turn_and_reraises(self, patch_exec, tmp_path):
        """The watchdog's CancelledError must not swallow captured telemetry: the
        turn is ended, the terminal event says CRASHED, and the cancellation
        still propagates."""
        proc = _HangingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)
        agent = _agent()
        await agent.start(str(tmp_path))
        recorder = _EventRecorder()

        task = asyncio.ensure_future(agent.communicate("do the thing", iteration=1, stream_callback=recorder))
        await asyncio.sleep(0.05)  # let it spawn and read the first event
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task  # the await re-raises the cancellation; no value ever exists

        ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        assert ends[0].status is AgentEndStatus.CRASHED
        assert ends[0].crashed is True
        assert ends[0].crash_reason == "turn cancelled"
        assert proc.killed is True  # not abandoned mid-stream — see TestTurnAlwaysReapsTheCli


class TestTurnEventsAreBalanced:
    """`Agent.communicate`'s contract is one TurnStart/TurnEnd pair per inner turn.

    `on_step_start` opens one per CLI step and `on_step_finish` closes it, but a
    turn that dies (or is cut) between the two must still close the last
    TurnStartEvent — a task.log with `>>> Turn start` and no matching
    `--- Turn end` is the defect. The emitter closes it at the end of the turn.
    """

    @staticmethod
    def _pairs(recorder: _EventRecorder) -> tuple[int, int]:
        starts = len([e for e in recorder.events if isinstance(e, TurnStartEvent)])
        ends = len([e for e in recorder.events if isinstance(e, TurnEndEvent)])
        return starts, ends

    async def test_a_clean_turn_is_balanced(self, patch_exec, tmp_path):
        patch_exec(_FakeProcess(HAPPY_STREAM))
        recorder = _EventRecorder()
        await _run(_agent(), tmp_path, stream_callback=recorder)
        assert self._pairs(recorder) == (2, 2)  # HAPPY_STREAM is two steps

    async def test_a_timeout_closes_the_open_step(self, patch_exec, tmp_path):
        proc = _HangingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)
        recorder = _EventRecorder()

        outcome = await _run(_agent(), tmp_path, timeout=0.2, stream_callback=recorder)
        assert outcome.status is AgentEndStatus.TIMEOUT

        assert self._pairs(recorder) == (1, 1)
        end = next(e for e in recorder.events if isinstance(e, TurnEndEvent))
        assert end.status is TurnEndStatus.TIMEOUT
        assert end.turn_id == "msg_1"

    async def test_a_cancel_closes_the_open_step(self, patch_exec, tmp_path):
        proc = _HangingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)
        agent = _agent()
        await agent.start(str(tmp_path))
        recorder = _EventRecorder()

        task = asyncio.ensure_future(agent.communicate("do the thing", iteration=1, stream_callback=recorder))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task

        assert self._pairs(recorder) == (1, 1)
        end = next(e for e in recorder.events if isinstance(e, TurnEndEvent))
        assert end.status is TurnEndStatus.CRASHED

    async def test_a_crash_closes_the_open_step(self, patch_exec, tmp_path):
        patch_exec(_ExplodingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})]))
        recorder = _EventRecorder()

        outcome = await _run(_agent(), tmp_path, stream_callback=recorder)
        assert outcome.status is AgentEndStatus.CRASHED

        assert self._pairs(recorder) == (1, 1)

    async def test_a_clean_cut_closes_the_open_step(self, patch_exec, tmp_path):
        """A should_stop cut between a step's start and its finish closes the step too."""
        proc = _RunningProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)
        recorder = _EventRecorder()

        await _run(_agent(), tmp_path, should_stop=lambda: StopReason.EARLY_CRITERION, stream_callback=recorder)

        assert self._pairs(recorder) == (1, 1)
        end = next(e for e in recorder.events if isinstance(e, TurnEndEvent))
        assert end.status is TurnEndStatus.STOPPED_EARLY

    async def test_a_completed_step_is_never_closed_twice(self, patch_exec, tmp_path):
        """Completed steps close themselves in `on_step_finish`, so the end of the
        turn must close ONLY a straggler."""
        patch_exec(_FakeProcess(HAPPY_STREAM))
        recorder = _EventRecorder()
        record = await _record(_agent(), tmp_path, stream_callback=recorder)

        assert record.crashed is False
        starts, ends = self._pairs(recorder)
        assert starts == ends == 2
        assert all(e.status is TurnEndStatus.COMPLETED for e in recorder.events if isinstance(e, TurnEndEvent))

    def test_a_dangling_step_is_closed_crashed_at_the_next_step_start(self):
        result, _ = _replay(
            [
                _event("step_start", {"messageID": "m1"}, at_ms=0),
                _event("step_start", {"messageID": "m2"}, at_ms=100),
                _finish(200),
            ]
        )

        turn_ends = [e for e in result.events if isinstance(e, TurnEndEvent)]
        assert [(e.turn_id, e.status) for e in turn_ends] == [
            ("m1", TurnEndStatus.CRASHED),
            ("m2", TurnEndStatus.COMPLETED),
        ]
        assert_stream_balanced(result.events)


class TestTurnAlwaysReapsTheCli:
    """No exit from `communicate()` may leave the CLI running.

    A crash is categorized AGENT_CRASH (max_retries=2), and the orchestrator
    only appends the crashed record — it never kills the agent. An abandoned CLI
    therefore means attempt 2 spawns a SECOND
    `opencode --dir <sandbox> --session <same id>` while attempt 1 is still
    editing the very files the criteria are about to score, and whichever writer
    wins decides the task's result.

    The graceful `await self.kill()` already covers the intentional cuts and the
    timeout; these pin the two paths that reach `finally` with a live child.
    """

    async def test_read_loop_crash_kills_the_cli(self, patch_exec, tmp_path):
        """A read-loop crash ends the turn as an outcome, and `finally` still reaps the live CLI."""
        proc = _ExplodingRunningProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)

        outcome = await _run(_agent(), tmp_path)

        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and outcome.error.startswith("OpenCode turn failed: ")
        assert proc.killed is True

    async def test_external_cancel_kills_the_cli(self, patch_exec, tmp_path):
        """The teardown must survive a CancelledError in flight, so it takes no
        await — an interrupted one would leave the child alive after all."""
        proc = _HangingProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})])
        patch_exec(proc)
        agent = _agent()
        await agent.start(str(tmp_path))

        task = asyncio.ensure_future(agent.communicate("do the thing", iteration=1))
        await asyncio.sleep(0.05)  # let it spawn and read the first event
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task

        assert proc.killed is True

    async def test_a_clean_turn_sweeps_only_the_group(self, patch_exec, tmp_path):
        """The CLI exited, so it is not killed; the server child it left is swept with the turn."""
        proc = _FakeProcess(HAPPY_STREAM)
        captured = patch_exec(proc)
        await _run(_agent(), tmp_path)

        assert proc.killed is False
        assert captured["killpg"] == [(4242, signal.SIGKILL)]

    async def test_a_spawn_failure_has_no_process_to_reap(self, monkeypatch, tmp_path):
        """`proc` is unbound on this path; the guard must not raise NameError over it."""

        async def boom(*_argv: str, **_kwargs: Any):
            raise OSError("no fork for you")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/opencode")

        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED
        assert outcome.error is not None and "no fork for you" in outcome.error


@pytest.mark.skipif(os.name != "posix", reason="process-group teardown (killpg/SIGKILL) is POSIX-only by design")
class TestProcessGroupTeardown:
    async def test_spawn_uses_its_own_session(self, patch_exec, tmp_path):
        """Each invocation must be its own process group, so killpg can reap the
        server child without touching anything this invocation didn't spawn."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path)
        assert captured["kwargs"]["start_new_session"] is (os.name == "posix")

    async def test_a_clean_turn_sweeps_the_spawned_group_once(self, patch_exec, tmp_path):
        """`opencode run` leaves a server child holding the pipes; the turn must
        SIGKILL the whole group, and stop() must not signal a pgid that may be reused."""
        captured = patch_exec(_FakeProcess(HAPPY_STREAM))
        agent = _agent()
        await _run(agent, tmp_path)
        assert captured["killpg"] == [(4242, signal.SIGKILL)]

        await agent.stop()
        assert captured["killpg"] == [(4242, signal.SIGKILL)]

    async def test_a_crashed_turn_sweeps_the_group_too(self, patch_exec, tmp_path):
        """Killing the CLI pid alone would orphan the server child it left holding
        the pipes — across a retried batch, that is the leak that compounds."""
        captured = patch_exec(_ExplodingRunningProcess([_evt("step_start", {"id": "prt_1", "messageID": "msg_1"})]))

        outcome = await _run(_agent(), tmp_path)
        assert outcome.status is AgentEndStatus.CRASHED

        assert (4242, signal.SIGKILL) in captured["killpg"]

    async def test_cooperative_stop_sweeps_the_group_too(self, patch_exec, tmp_path):
        captured = patch_exec(_RunningProcess(HAPPY_STREAM))
        await _run(_agent(), tmp_path, should_stop=lambda: StopReason.EARLY_CRITERION)
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
        record = await _record(_agent(), tmp_path, stream_callback=recorder)

        [cmd] = record.commands
        assert cmd.result_status == "error"
        assert cmd.error_message == "boom: command exploded"
        [end] = [e for e in recorder.events if isinstance(e, ToolEndEvent)]
        assert end.status is ToolEndStatus.ERROR

    async def test_permission_denial_gets_its_own_status(self, patch_exec, tmp_path):
        recorder = _EventRecorder()
        patch_exec(_FakeProcess([self._failing_tool("Permission denied by policy")]))
        record = await _record(_agent(), tmp_path, stream_callback=recorder)

        [cmd] = record.commands
        assert cmd.result_status == "error"  # the persisted tri-state folds both
        [end] = [e for e in recorder.events if isinstance(e, ToolEndEvent)]
        assert end.status is ToolEndStatus.PERMISSION_DENIED

    def test_an_unresolved_call_is_swept_never_dropped(self):
        """A call the CLI never resolved still surfaces, as an `unknown` tool with no invented error."""
        result, decoder = _replay([_event("step_start", {"messageID": "m1"}), _tool("ghost", "pending", at_ms=1)])

        [event] = [e for e in result.events if isinstance(e, ToolEndEvent)]
        assert event.status is ToolEndStatus.UNRESOLVED
        assert event.tool.tool_id == "ghost"
        assert event.tool.tool_name == "Bash"
        assert event.tool.result_status == "unknown"
        assert event.tool.error_message is None
        assert decoder.open_tools == {"ghost": {"command": "ls"}}
        assert [c.tool_id for c in result.record.commands] == ["ghost"]


class TestTimingIsTheClisOwn:
    """Under `cli_epoch_ms` every recorded bound is a CLI stamp, never a read of the host clock.

    Each case moves the scripted clock far away from the stamps, so a bound taken
    from the clock instead of the stream lands seconds off and fails.
    """

    def test_window_bounds_are_the_envelope_stamps(self):
        result, _ = _replay(
            [
                Tick(7_000),
                _event("step_start", {"messageID": "m1"}, at_ms=100),
                Tick(50_000),
                _finish(900),
            ]
        )

        [message] = _assistants(result)
        assert message.started_at == _at(100)
        assert message.completed_at == _at(900)
        assert message.generation_duration_ms == pytest.approx(800.0)

    def test_tool_span_is_state_time(self):
        result, _ = _replay(
            [
                _event("step_start", {"messageID": "m1"}, at_ms=0),
                Tick(9_000),
                _tool("c1", "completed", at_ms=600, start=200, end=450),
                _finish(1000),
            ]
        )

        [command] = result.record.commands
        assert command.execution_started_at == _at(200)
        assert command.execution_completed_at == _at(450)
        assert command.duration_ms == pytest.approx(250.0)

    def test_a_resolved_tool_without_an_end_stamp_gets_no_completion_or_duration(self):
        result, _ = _replay(
            [
                _event("step_start", {"messageID": "m1"}, at_ms=0),
                Tick(600),
                _tool("c1", "completed", at_ms=600, start=200),
                _finish(1000),
            ]
        )

        [command] = result.record.commands
        assert command.result_status == "success"
        assert command.execution_started_at == _at(200)
        assert command.execution_completed_at is None
        assert command.duration_ms is None

    def test_an_orphan_has_no_completion_stamp(self):
        """Force-closing is not observing a completion; the CLI's start stamp is kept."""
        result, _ = _replay(
            [
                _event("step_start", {"messageID": "m1"}, at_ms=0),
                _tool("c1", "running", at_ms=200, start=200),
                _finish(1000),
                Tick(4_000),
            ]
        )

        [command] = result.record.commands
        assert command.result_status == "unknown"
        assert command.error_message is None
        assert command.execution_started_at == _at(200)
        assert command.execution_completed_at is None
        assert command.duration_ms is None

    def test_a_missing_envelope_stamp_bounds_the_window_on_the_host_clock_and_warns_once(self, caplog):
        with caplog.at_level("WARNING", logger="coder_eval.agents.opencode_agent"):
            result, decoder = _replay(
                [
                    _event("step_start", {"messageID": "m1"}, at_ms=0),
                    Tick(700),
                    _finish(None),
                    Tick(1_200),
                    _event("step_start", {"messageID": "m2"}, at_ms=None),
                    Tick(1_500),
                    _finish(None),
                ]
            )

        first, second = _assistants(result)
        assert first.started_at == _at(0)
        assert first.completed_at == _BASE + timedelta(milliseconds=700)
        assert second.started_at == first.completed_at
        assert second.completed_at == _BASE + timedelta(milliseconds=1_500)
        assert decoder.warned_missing_stamp is True
        warnings = [r for r in caplog.records if "no envelope timestamp" in r.getMessage()]
        assert len(warnings) == 1

    def test_the_captured_stream_tiles_on_its_own_stamps(self):
        """A real `opencode run` stream: every window and tool bound is a CLI stamp, and the identity closes."""
        lines = [json.loads(line) for line in CAPTURED_STREAM]
        first_ms = lines[0]["timestamp"]
        last_ms = max(event.get("timestamp", first_ms) for event in lines)
        origin = datetime.fromtimestamp(first_ms / 1000)
        stream: list[Any] = [*lines, Tick(last_ms - first_ms)]

        decoders: list[_OpenCodeDecoder] = []

        def end(decoder: _OpenCodeDecoder) -> TurnOutcome:
            decoders.append(decoder)
            return decoder.end(AgentEndStatus.COMPLETED)

        result = replay(stream, _OpenCodeDecoder, clock=ScriptedClock(origin), basis=TimingBasis.CLI_EPOCH_MS, end=end)

        assert result.outcome.status is AgentEndStatus.COMPLETED
        assert decoders[0].warned_missing_stamp is False
        assert [c.tool_name for c in result.record.commands] == ["Write", "Bash"]
        assert all(c.duration_ms is not None for c in result.record.commands)
        assert_stream_balanced(result.events)
        assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)


class TestGenerationWindowExcludesToolExecution:
    """A tool running inside a step is not model time — asserted where it is now DECIDED.

    The decoder does not subtract anything. It publishes the RAW window, and
    `timing.subtract_tool_time` takes the tool union back out of it
    once, for all five harnesses. So these cases replay the decoder through a
    real emitter and assert the PUBLISHED number — the one that reaches
    `task.json` — rather than an intermediate the decoder used to own.

    They are not duplicates of
    `tests/test_event_collector.py::TestSubtractToolTime`: those pin the
    arithmetic, these pin that THIS decoder hands the collector a window and a
    span set the arithmetic can be right about.
    """

    def _finish_step(self, spans: list[tuple[int, int]], open_starts: tuple[int, ...] = ()) -> AssistantMessage:
        """Replay one step stamped 0 -> 1000 ms with tool calls whose `state.time` is at the given offsets.

        `spans` are RESOLVED calls (both bounds); `open_starts` are calls that
        never returned. An unresolved call contributes NO span — it has no
        `execution_completed_at`, and inventing one is what `None` exists to
        prevent. The collector sees every span at once, so a call straddling a
        boundary is clipped to each window it actually overlapped.
        """
        stream: list[Any] = [_event("step_start", {"messageID": "m1"}, at_ms=0)]
        stream += [
            _tool(f"closed-{i}", "completed", at_ms=completed, start=started, end=completed)
            for i, (started, completed) in enumerate(spans)
        ]
        stream += [_tool(f"open-{i}", "running", at_ms=started, start=started) for i, started in enumerate(open_starts)]
        stream.append(_finish(1000))

        result, _ = _replay(stream)
        published = _assistants(result)
        assert len(published) == 1
        return published[0]

    def test_tool_time_inside_the_step_is_subtracted(self):
        message = self._finish_step([(200, 700)])
        span_ms = (message.completed_at - message.started_at).total_seconds() * 1000.0
        assert span_ms == pytest.approx(1000.0), "the decoder still publishes the whole window as its bounds"
        assert message.generation_duration_ms == pytest.approx(500.0)

    def test_a_step_with_no_tools_keeps_its_whole_window(self):
        assert self._finish_step([]).generation_duration_ms == pytest.approx(1000.0)

    def test_concurrent_tools_are_subtracted_once(self):
        # Two overlapping 500ms tools occupy 600ms, not 1000ms. Summing them
        # would leave 0 generation for a step that generated 400.
        message = self._finish_step([(100, 600), (200, 700)])
        assert message.generation_duration_ms == pytest.approx(400.0)

    def test_the_window_never_goes_negative(self):
        message = self._finish_step([(-30_000, 31_000)])
        assert message.generation_duration_ms == 0.0

    def test_a_tool_still_open_at_the_boundary_contributes_no_span(self):
        """A call with no `execution_completed_at` was never timed.

        Its time is subtracted when it RESOLVES, from whichever windows its real
        interval overlaps — never bounded at the window's end.
        """
        message = self._finish_step([], open_starts=(600,))
        assert message.generation_duration_ms == pytest.approx(1000.0)

    def test_a_resolved_tool_overlapping_an_unresolved_one_counts_only_the_resolved(self):
        message = self._finish_step([(200, 700)], open_starts=(500,))
        assert message.generation_duration_ms == pytest.approx(500.0)

    def test_a_mark_later_than_the_step_start_does_not_invert_the_window(self):
        """The backwards-stamp defence, pinned at the decoder, not in isolation.

        `close_window`'s `min()` only fires if the decoder actually passes the
        step's own start as `item_start`. Drop that argument and the window
        opens at the (later) mark instead, so the span shrinks — or inverts and
        clamps to 0.0, publishing a fabricated instant generation. The CLI's
        stamps are not monotonic by contract: here the first step's
        `step_finish` is stamped 400 ms AFTER the second step's `step_start`.
        """
        result, decoder = _replay(
            [
                _event("step_start", {"messageID": "m0"}, at_ms=-500),
                _finish(400),
                _event("step_start", {"messageID": "m1"}, at_ms=0),
                _finish(1000),
            ]
        )

        _, message = _assistants(result)
        assert decoder.gen_mark == _at(1000)
        assert message.started_at == _at(0)
        assert message.generation_duration_ms == pytest.approx(1000.0)

    def test_the_published_window_reconciles_to_its_own_bounds(self):
        """The collector subtracted exactly the spans the record carries.

        `scripts/timing/decompose_run.py` and the evalboard's Unaccounted cell
        both recompute the tool UNION from the recorded command spans and
        subtract it from the recorded window bounds. This asserts the published
        record is internally consistent under that recomputation, so a span
        silently added or dropped on the way in shows up here.
        """
        from coder_eval.timing import busy_ms

        message = self._finish_step([(200, 700)])
        span_ms = (message.completed_at - message.started_at).total_seconds() * 1000.0
        expected = span_ms - busy_ms([(_at(200), _at(700))], message.started_at, message.completed_at)
        assert message.generation_duration_ms == pytest.approx(expected)


class TestGenerationWindowsTileTheTurn:
    """Each step's window runs from the PREVIOUS step's finish, not its own `step_start`.

    The CLI announces a step only once it is already producing one, so the
    model time that PRODUCED the step lands in the gap before it. Measured on
    tasks/hello_date with a live claude-haiku-4.5: gaps of 857 ms and 851 ms
    carrying no tool at all (the Write inside them took 7 ms), attributed to
    nothing — 24% of the turn, on its own enough to hold OpenCode above the
    evalboard's 25% "Unaccounted" red threshold.
    """

    def _two_steps(self) -> list[AssistantMessage]:
        # Step 1 runs 0 -> 1000; then 800ms of model time, then a step the CLI only announces at 1800.
        result, _ = _replay(
            [
                _event("step_start", {"messageID": "m1"}, at_ms=0),
                _finish(1000),
                _event("step_start", {"messageID": "m2"}, at_ms=1800),
                _finish(2000),
            ]
        )
        return _assistants(result)

    def test_the_gap_before_a_step_is_its_generation_time(self):
        first, second = self._two_steps()
        # Bounded by its own step_start, this window was 200ms and the 800ms
        # that produced it was attributed to nothing.
        assert second.generation_duration_ms == pytest.approx(1000.0)
        assert second.started_at == first.completed_at

    def test_the_first_step_keeps_its_own_start(self):
        """Everything before the first `step_start` is CLI spawn, not model time.

        Tiling the first window back to the turn's start would report Node's
        boot — 3.1 s of OpenCode's measured head — as generation.
        """
        first, _ = self._two_steps()
        assert first.started_at == _at(0)
        assert first.generation_duration_ms == pytest.approx(1000.0)

    def test_the_steps_leave_no_gap_between_them(self):
        first, second = self._two_steps()
        covered = (second.completed_at - first.started_at).total_seconds() * 1000.0
        gen = sum(m.generation_duration_ms or 0.0 for m in (first, second))
        assert gen == pytest.approx(covered)


class TestToolSpansSurviveTheStepBoundary:
    """A tool that closes BETWEEN two steps still belongs to the next window.

    `timing.subtract_tool_time` sees every span at once and clips each to the
    windows it overlaps, so the property holds by construction rather than by a
    reset rule. Kept because the property is what matters: a future decoder
    change could still break it by moving a mark or failing to close the tool
    the collector reduces.

    It needs the NON-TERMINAL tool path to reach: the CLI normally emits one
    already-`completed` event per call, which closes inside the step that
    opened it. That is why the measured corpus reads 0.00% and a reproduction
    has to script the stream.
    """

    def _run(self) -> Replay:
        return _replay(
            [
                _event("step_start", {"messageID": "m1"}, at_ms=0),
                _tool("c1", "running", at_ms=100, start=100),  # non-terminal: stays open across the boundary
                _finish(1000),
                _tool("c1", "completed", at_ms=1500, start=100, end=1500),  # closes in the GAP between the steps
                _event("step_start", {"messageID": "m2"}, at_ms=1600),
                _finish(2000),
                Tick(2000),
            ]
        )[0]

    def test_the_gap_slice_of_a_straddling_call_is_not_published_as_generation(self):
        messages = _assistants(self._run())
        assert len(messages) == 2
        # Window 2 tiles 1000 -> 2000. c1 ran for 1000 -> 1500 of it, so 500ms
        # is model time. Before the reset moved, this published 1000.0 — a 100%
        # overstatement, with c1's own duration_ms counting the same 500ms.
        assert messages[1].generation_duration_ms == pytest.approx(500.0)

    def test_the_call_is_subtracted_from_exactly_one_window(self):
        # Window 1 owns c1's 100 -> 1000 slice; window 2 owns 1000 -> 1500. Neither owns both.
        messages = _assistants(self._run())
        assert messages[0].generation_duration_ms == pytest.approx(100.0)
        assert messages[1].generation_duration_ms == pytest.approx(500.0)

    def test_the_four_bucket_identity_closes_exactly_across_the_boundary(self):
        """generation + UNION(tool) accounts for the whole span, to the ms.

        The assertion the golden corpus CANNOT make: `_scrub.py` masks
        `generation_duration_ms` and both bounds to a placeholder, so a
        snapshot records that a window was measured and never what it
        measured.
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

    def test_a_duplicate_step_finish_does_not_republish_the_previous_window(self):
        """A spent `step_started_at` must not seed the next window.

        `close_window`'s `min(mark, item_start)` pulls the window open to cover
        the item's own start. A start stamp left in place after its step was
        published is a stale value BEFORE the mark, so the guard would reopen the
        next window at the previous step's start and publish that whole span
        again. Reproduced on Pi's identical twin before the fix: 3000 ms of
        generation for a 2000 ms turn.
        """
        result, _ = _replay(
            [_event("step_start", {"messageID": "m1"}, at_ms=0), _finish(1000), _finish(2000)]
        )  # no intervening `step_start`

        messages = _assistants(result)
        assert len(messages) == 2
        assert messages[1].started_at == messages[0].completed_at
        assert sum(m.generation_duration_ms or 0.0 for m in messages) == pytest.approx(2000.0)

    def test_a_step_that_never_finishes_does_not_advance_the_mark(self):
        """The half of this that is still the decoder's job.

        There is no span list to preserve — the collector reduces the ToolEnd
        stream itself. What the decoder still owns is the MARK: a step that
        published nothing must not advance it, or its time is handed to
        whichever step finishes next.
        """
        result, decoder = _replay(
            [
                _event("step_start", {"messageID": "m1"}, at_ms=0),
                _finish(1000),
                _event("step_start", {"messageID": "m2"}, at_ms=1600),
                _tool("c2", "running", at_ms=1700, start=1700),
                Tick(1900),
            ],
            status=AgentEndStatus.CRASHED,
            reason="turn cancelled",
        )

        assert decoder.gen_mark == _at(1000)
        assert len(_assistants(result)) == 1
        assert result.outcome.status is AgentEndStatus.CRASHED


class TestModelTurnCap:
    def test_the_model_turn_cap_stops_at_the_next_turn_start_with_the_last_turn_resolved(self) -> None:
        lines = Path("tests/fixtures/opencode_happy_stream.jsonl").read_text(encoding="utf-8").splitlines()
        result, _ = _replay([json.loads(line) for line in lines if line.strip()])
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="go",
            agent=parse_agent_config(type="opencode"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
            run_limits=RunLimits(max_turns=1),
        )
        monitor = TurnMonitor.for_task(task, arm=False)
        ends: list[ToolEndEvent] = []
        latch: tuple[StreamEvent, list[ToolEndEvent]] | None = None
        for event in result.events:
            monitor.on_event(event)
            if latch is None and monitor.stop_reason is not None:
                latch = (event, list(ends))
            if isinstance(event, ToolEndEvent) and event.parent_thread_id is None:
                ends.append(event)

        assert monitor.model_turns == 2
        assert monitor.stop_reason is StopReason.MODEL_TURN_CAP
        assert latch is not None
        latched_on, ends_at_latch = latch
        assert isinstance(latched_on, TurnStartEvent)
        unresolved = sum(end.status is ToolEndStatus.UNRESOLVED for end in ends_at_latch)
        assert (len(ends_at_latch) - unresolved, unresolved) == (2, 0)
