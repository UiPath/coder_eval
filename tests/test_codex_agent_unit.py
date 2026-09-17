"""SDK-independent unit tests for CodexAgent.

These tests exercise pure-logic seams of ``codex_agent.py`` — ``_CodexDecoder``
driven over a real ``TurnEmitter`` and the telemetry builders — that need NO
Codex SDK. ``codex_agent`` imports ``openai_codex`` only lazily (inside
``start`` / ``_build_thread_options`` / the turn-completed handler), so the
module imports cleanly without the extra and these tests run in the base
Quality Gate.

The SDK-dependent tests (anything constructing real SDK notification/Turn
types or driving ``communicate``) stay in ``test_codex_agent.py`` behind that
module's ``importorskip("openai_codex")``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from coder_eval.agents.codex_agent import CodexAgent, _CodexDecoder
from coder_eval.models import AgentKind, TimingBasis, parse_agent_config
from coder_eval.streaming.emitter import TurnEmitter
from coder_eval.streaming.events import AgentEndStatus
from coder_eval.testing import ScriptedClock


def _item_notification(method: str, root: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=SimpleNamespace(item=SimpleNamespace(root=root)))


class TestCodexDecoder:
    """Unit tests for the per-turn decoder that ``_run_turn_with_streaming`` feeds."""

    @staticmethod
    def _decoder() -> _CodexDecoder:
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        emitter = TurnEmitter(
            task_id="codex",
            iteration=1,
            prompt="go",
            model="gpt-5-codex",
            basis=TimingBasis.CLI_EPOCH_MS,
            clock=ScriptedClock(datetime(2026, 1, 1)),
            sinks=[],
        )
        emitter.begin()
        return _CodexDecoder(agent, emitter, turn_id="codex-1")

    def test_a_crash_keeps_the_partial_transcript(self):
        # A mid-turn crash must keep what the turn already produced: the flushed
        # message and the completed command both reach the crashed record.
        decoder = self._decoder()
        cmd_root = SimpleNamespace(
            type="commandExecution", id="c1", command="echo hi", exit_code=0, aggregated_output="hi\n", duration_ms=5
        )
        decoder(_item_notification("item/started", cmd_root))
        decoder(_item_notification("item/completed", cmd_root))
        decoder.flush(SimpleNamespace(input_tokens=10, cached_input_tokens=0, output_tokens=5))

        outcome = decoder.end(AgentEndStatus.CRASHED, reason="stream blew up")

        assert outcome.record.crashed is True
        assert [m.message_id for m in outcome.record.messages if m.role == "assistant"] == ["codex-1-msg-0"]
        assert [m.message_id for m in decoder.messages] == ["codex-1-msg-0"]
        assert [c.tool_id for c in outcome.record.commands] == ["c1"]

    def test_command_dispatch_reaches_the_record(self):
        decoder = self._decoder()

        cmd_root = SimpleNamespace(
            type="commandExecution", id="c1", command="echo hi", exit_code=0, aggregated_output="hi\n", duration_ms=5
        )
        decoder(_item_notification("item/started", cmd_root))
        decoder(_item_notification("item/completed", cmd_root))

        assert decoder.opened_tools == {"c1"}
        # A tool_use block was recorded into the open buffer (cut at the next
        # tokenUsage flush, not here), joinable to the command by tool_id.
        assert any(b.block_type == "tool_use" and b.tool_use_id == "c1" for b in decoder.open_blocks)
        commands = decoder.end(AgentEndStatus.COMPLETED).record.commands
        assert len(commands) == 1
        assert commands[0].tool_name == "Bash"
        assert commands[0].result_status == "success"

    def test_command_output_recorded_whole_not_truncated(self):
        # Regression for the Codex `output[:100]` bug (CE043): result_summary must
        # carry the FULL command output so result_tokens reflects real tool-output
        # size instead of being pinned at a ~31-token, 100-char cap.
        decoder = self._decoder()

        big_output = "X" * 4000  # far beyond the old 100-char clip
        cmd_root = SimpleNamespace(
            type="commandExecution",
            id="c2",
            command="cat big.txt",
            exit_code=0,
            aggregated_output=big_output,
            duration_ms=5,
        )
        decoder(_item_notification("item/started", cmd_root))
        decoder(_item_notification("item/completed", cmd_root))

        cmd = decoder.end(AgentEndStatus.COMPLETED).record.commands[0]
        assert big_output in (cmd.result_summary or ""), "full output must be recorded, not truncated"
        # result_tokens (ceil(len/4)) must scale with the real output, not ~31.
        assert cmd.result_tokens >= len(big_output) // 4
        assert cmd.result_tokens > 100


# ---------------------------------------------------------------------------
# Execution bounds
#
# The SDK delivers `started_at_ms` / `completed_at_ms` on the item notification
# and the agent discarded both, publishing the item's own `duration_ms`
# instead — 0.0 for 70 of 211 commands in one nightly, absent for 25 more, and
# no execution bounds at all, so no Codex tool call could be placed on a
# timeline. All three telemetry builders now derive their timing identically.
#
# Pure logic: the builders take plain SimpleNamespace roots, so these live
# here rather than behind test_codex_agent.py's importorskip — otherwise a
# clean `make test` (which syncs no codex extra) skips them entirely.
# ---------------------------------------------------------------------------

_EPOCH_MS = 1_800_000_000_000


def _command_item(item_id: str = "cmd_1", *, duration_ms: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        type="commandExecution",
        id=item_id,
        command="echo hi",
        exit_code=0,
        aggregated_output="hi",
        duration_ms=duration_ms,
    )


def _generic_item(item_id: str = "mcp_1", *, duration_ms: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        type="mcpToolCall",
        id=item_id,
        server="srv",
        tool="lookup",
        arguments={"q": "x"},
        status="completed",
        error=None,
        success=True,
        duration_ms=duration_ms,
    )


def _file_change_item(item_id: str = "fc_1", *, duration_ms: object = None) -> SimpleNamespace:
    """A fileChange root. The SDK reports no duration for one, so
    ``duration_ms`` is accepted (for signature parity with the other two
    factories) and deliberately ignored."""
    del duration_ms
    return SimpleNamespace(
        type="fileChange",
        id=item_id,
        changes=[SimpleNamespace(path="out.txt")],
        status="completed",
    )


# (root factory, root_type) for each of the three builders, so a fix in one
# cannot pass for the others.
_BUILDERS = [
    pytest.param(_command_item, "commandExecution", id="commandExecution"),
    pytest.param(_file_change_item, "fileChange", id="fileChange"),
    pytest.param(_generic_item, "mcpToolCall", id="generic"),
]

# The two whose SDK item carries a duration at all. NAMED, not sliced
# positionally: reordering _BUILDERS must not silently point the fallback
# tests at fileChange, where the value is discarded and they would pass
# while asserting nothing.
_BUILDERS_WITH_SDK_DURATION = [_BUILDERS[0], _BUILDERS[2]]


class TestResultStatus:
    """A completed item is a resolved call: its status is the tool end status, never ``unknown``."""

    @pytest.mark.parametrize(
        ("root", "expected"),
        [
            pytest.param(
                SimpleNamespace(type="commandExecution", id="c1", command="rm x", exit_code=None, status="declined"),
                "error",
                id="declined-command",
            ),
            pytest.param(SimpleNamespace(type="webSearch", id="w1", query="q"), "success", id="no-status-field"),
        ],
    )
    def test_a_completed_item_records_a_resolved_status(self, root, expected):
        assert TestExecutionBoundsFromSdkStamps._build(root, root.type).result_status == expected


class TestExecutionBoundsFromSdkStamps:
    """For all three item kinds, the recorded command's bounds and duration come from the SDK stamps."""

    @staticmethod
    def _build(root, root_type, *, started_ms=None, completed_ms=None):
        """The command the turn record keeps after the decoder saw the item start and complete."""
        del root_type
        decoder = TestCodexDecoder._decoder()
        for method, stamps in (
            ("item/started", {"started_at_ms": started_ms}),
            ("item/completed", {"completed_at_ms": completed_ms}),
        ):
            decoder(SimpleNamespace(method=method, payload=SimpleNamespace(item=SimpleNamespace(root=root), **stamps)))
        (command,) = decoder.emitter.finalize(AgentEndStatus.COMPLETED).record.commands
        return command

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_both_stamps_give_bounds_and_an_exact_duration(self, factory, root_type):
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS, completed_ms=_EPOCH_MS + 250)
        assert tel.execution_started_at == datetime.fromtimestamp(_EPOCH_MS / 1000)
        assert tel.execution_completed_at == datetime.fromtimestamp((_EPOCH_MS + 250) / 1000)
        assert tel.duration_ms == 250.0

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_timestamp_is_the_tools_own_start(self, factory, root_type):
        # It used to be datetime.now() at COMPLETION, which places the call
        # after its own execution.
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS, completed_ms=_EPOCH_MS + 250)
        assert tel.timestamp == datetime.fromtimestamp(_EPOCH_MS / 1000)

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_only_one_stamp_never_fabricates_an_interval(self, factory, root_type):
        # _ms_to_dt(None) is datetime.now(), so pairing a real stamp with a
        # missing one would invent an interval running to the present moment.
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS, completed_ms=None)
        assert tel.execution_started_at == datetime.fromtimestamp(_EPOCH_MS / 1000)
        assert tel.execution_completed_at is None
        assert tel.duration_ms is None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_a_backwards_pair_clamps_but_keeps_both_bounds(self, factory, root_type):
        # Clock skew. The duration cannot be negative, but the anomaly stays
        # visible in the record.
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS + 500, completed_ms=_EPOCH_MS)
        assert tel.duration_ms == 0.0
        assert tel.execution_started_at is not None
        assert tel.execution_completed_at is not None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_derived_duration_wins_over_a_conflicting_sdk_value(self, factory, root_type):
        tel = self._build(factory(duration_ms=0), root_type, started_ms=_EPOCH_MS, completed_ms=_EPOCH_MS + 250)
        assert tel.duration_ms == 250.0

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_no_stamps_falls_back_to_a_reported_sdk_duration(self, factory, root_type):
        tel = self._build(factory(duration_ms=12), root_type)
        assert tel.execution_started_at is None
        assert tel.duration_ms == 12.0

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_an_sdk_zero_is_unreported_not_instant(self, factory, root_type):
        # The case that motivated the change: 70 of 211 commands in one
        # nightly reported 0 for calls the message gaps show took seconds.
        assert self._build(factory(duration_ms=0), root_type).duration_ms is None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_a_negative_sdk_duration_is_unreported_too(self, factory, root_type):
        assert self._build(factory(duration_ms=-5), root_type).duration_ms is None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_an_absent_sdk_duration_stays_unknown(self, factory, root_type):
        assert self._build(factory(), root_type).duration_ms is None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_generation_completed_at_is_left_unset(self, factory, root_type):
        # Codex's stream does not say when the model finished emitting the
        # tool_use block; deriving it from the flush time would be a guess.
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS, completed_ms=_EPOCH_MS + 1)
        assert tel.generation_completed_at is None
