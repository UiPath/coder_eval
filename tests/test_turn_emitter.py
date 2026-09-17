"""``TurnEmitter``: the one writer of the event protocol, and the rules it enforces at runtime."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pytest

from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.errors.agent import CRASH_REASON_MAX_CHARS
from coder_eval.models import ContentBlock, ResultSummary, TimingBasis, TokenUsage, TurnRecord
from coder_eval.streaming.emitter import Generation, TurnEmitter, TurnOutcome
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.timing import Window, close_window


BASE = datetime(2026, 9, 16, 12, 0, 0)


def at(ms: float) -> datetime:
    return BASE + timedelta(milliseconds=ms)


class _Clock:
    def __init__(self) -> None:
        self.ms = 0.0

    def now(self) -> datetime:
        return at(self.ms)


class _Sink:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)

    def of(self, kind: type[Any]) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


def _emitter(
    basis: TimingBasis = TimingBasis.TURN_CLOCK, *, sinks: list[Any] | None = None
) -> tuple[TurnEmitter, _Clock, _Sink]:
    clock, sink = _Clock(), _Sink()
    emitter = TurnEmitter(
        task_id="t",
        iteration=3,
        prompt="go",
        model="m",
        basis=basis,
        clock=clock,
        sinks=[sink] if sinks is None else sinks,
    )
    emitter.begin()
    return emitter, clock, sink


def _text(text: str) -> ContentBlock:
    return ContentBlock(block_type="text", sequence=0, text=text)


def _tool_use(tool_id: str) -> ContentBlock:
    return ContentBlock(block_type="tool_use", sequence=0, tool_use_id=tool_id)


def _part(*blocks: ContentBlock, output: int = 0) -> Generation:
    return Generation(blocks=list(blocks), tokens=TokenUsage(output_tokens=output))


class TestBracketAndSinks:
    def test_the_bracket_is_stamped_from_the_clock(self) -> None:
        clock, sink = _Clock(), _Sink()
        clock.ms = 100
        emitter = TurnEmitter(
            task_id="t", iteration=1, prompt="go", model="m", basis=TimingBasis.TURN_CLOCK, clock=clock, sinks=[sink]
        )
        emitter.begin()
        clock.ms = 900
        emitter.finalize(AgentEndStatus.COMPLETED)
        assert sink.of(AgentStartEvent)[0].timestamp == at(100)
        assert sink.of(AgentEndEvent)[0].timestamp == at(900)

    def test_begin_twice_raises(self) -> None:
        emitter, _, _ = _emitter()
        with pytest.raises(RuntimeError, match="twice"):
            emitter.begin()

    @pytest.mark.parametrize(
        "call",
        [
            lambda e: e.begin_inner_turn("a"),
            lambda e: e.text("hi"),
            lambda e: e.open_tool("c1", "Bash", {}),
            lambda e: e.close_tool("c1", status=ToolEndStatus.OK),
            lambda e: e.add_unmeasured_generation(message_id="m", part=Generation(blocks=[], tokens=TokenUsage())),
            lambda e: e.finalize(AgentEndStatus.COMPLETED),
            lambda e: e.fail(AgentEndStatus.CRASHED, "boom"),
        ],
    )
    def test_every_call_before_begin_is_refused(self, call: Any) -> None:
        sink = _Sink()
        emitter = TurnEmitter(
            task_id="t", iteration=1, prompt="go", model="m", basis=TimingBasis.TURN_CLOCK, clock=_Clock(), sinks=[sink]
        )
        with pytest.raises(RuntimeError, match="before begin"):
            call(emitter)
        assert sink.events == []

    def test_a_raising_sink_does_not_break_the_emitter(self) -> None:
        class _Broken:
            def on_event(self, event: StreamEvent) -> None:
                raise ValueError("boom")

        good = _Sink()
        emitter, _, _ = _emitter(sinks=[_Broken(), good])
        emitter.text("hi")
        outcome = emitter.finalize(AgentEndStatus.COMPLETED)
        assert outcome.record.agent_output == "hi"
        assert len(good.of(AgentEndEvent)) == 1

    def test_no_sinks_is_valid(self) -> None:
        emitter, _, _ = _emitter(sinks=[])
        assert emitter.finalize(AgentEndStatus.COMPLETED).status is AgentEndStatus.COMPLETED


class TestToolBasis:
    @pytest.mark.parametrize("keyword", ["started_at", "completed_at"])
    @pytest.mark.parametrize("value", [None, BASE])
    def test_turn_clock_rejects_an_explicit_stamp(self, keyword: str, value: datetime | None) -> None:
        emitter, _, _ = _emitter()
        with pytest.raises(TypeError, match="TURN_CLOCK"):
            if keyword == "started_at":
                emitter.open_tool("c1", "Bash", {}, started_at=value)
            else:
                emitter.open_tool("c1", "Bash", {})
                emitter.close_tool("c1", status=ToolEndStatus.OK, completed_at=value)

    def test_cli_epoch_requires_both_stamps_on_a_main_thread_tool(self) -> None:
        emitter, _, _ = _emitter(TimingBasis.CLI_EPOCH_MS)
        with pytest.raises(TypeError, match="started_at"):
            emitter.open_tool("c1", "Bash", {})
        emitter.open_tool("c1", "Bash", {}, started_at=None)
        with pytest.raises(TypeError, match="completed_at"):
            emitter.close_tool("c1", status=ToolEndStatus.OK)

    def test_cli_epoch_uses_the_stamps_passed(self) -> None:
        emitter, _, _ = _emitter(TimingBasis.CLI_EPOCH_MS)
        emitter.open_tool("c1", "Bash", {}, started_at=at(700))
        emitter.close_tool("c1", status=ToolEndStatus.OK, completed_at=at(1200))
        command = emitter.finalize(AgentEndStatus.COMPLETED).record.commands[0]
        assert (command.execution_started_at, command.execution_completed_at) == (at(700), at(1200))
        assert command.duration_ms == pytest.approx(500.0)

    @pytest.mark.parametrize("basis", [TimingBasis.TURN_CLOCK, TimingBasis.CLI_EPOCH_MS])
    def test_a_nested_tool_is_exempt_from_both_checks(self, basis: TimingBasis) -> None:
        emitter, _, _ = _emitter(basis)
        emitter.open_tool("a", "Bash", {}, parent_tool_id="agent_1")
        emitter.close_tool("a", status=ToolEndStatus.OK)
        emitter.open_tool("b", "Bash", {}, parent_tool_id="agent_1", started_at=at(5))
        emitter.close_tool("b", status=ToolEndStatus.OK, completed_at=at(9))
        commands = {c.tool_id: c for c in emitter.finalize(AgentEndStatus.COMPLETED).record.commands}
        assert commands["b"].duration_ms == pytest.approx(4.0)
        stamped = basis is TimingBasis.TURN_CLOCK
        assert (commands["a"].execution_started_at is not None) is stamped
        assert (commands["a"].execution_completed_at is not None) is stamped

    def test_a_cli_tool_with_no_stamps_is_untimed(self) -> None:
        emitter, _, _ = _emitter(TimingBasis.CLI_EPOCH_MS)
        emitter.open_tool("c1", "Bash", {}, started_at=None)
        emitter.close_tool("c1", status=ToolEndStatus.OK, completed_at=None)
        command = emitter.finalize(AgentEndStatus.COMPLETED).record.commands[0]
        assert (command.execution_started_at, command.execution_completed_at, command.duration_ms) == (None, None, None)
        assert command.result_status == "success"

    def test_a_start_that_arrives_with_the_result_fills_a_missing_one(self) -> None:
        emitter, _, _ = _emitter(TimingBasis.CLI_EPOCH_MS)
        emitter.open_tool("late", "Bash", {}, started_at=None)
        emitter.close_tool("late", status=ToolEndStatus.OK, completed_at=at(900), started_at=at(400))
        emitter.open_tool("kept", "Bash", {}, started_at=at(100))
        emitter.close_tool("kept", status=ToolEndStatus.OK, completed_at=at(900), started_at=at(400))
        commands = {c.tool_id: c for c in emitter.finalize(AgentEndStatus.COMPLETED).record.commands}
        assert commands["late"].duration_ms == pytest.approx(500.0)
        assert commands["kept"].execution_started_at == at(100)

    def test_a_reported_duration_times_a_call_the_stamps_do_not(self) -> None:
        emitter, _, _ = _emitter(TimingBasis.CLI_EPOCH_MS)
        for tool_id in ("reported", "zero", "stamped", "orphan"):
            emitter.open_tool(tool_id, "Bash", {}, started_at=at(100) if tool_id == "stamped" else None)
        emitter.close_tool("reported", status=ToolEndStatus.OK, completed_at=None, reported_duration_ms=12.0)
        emitter.close_tool("zero", status=ToolEndStatus.OK, completed_at=None, reported_duration_ms=0.0)
        emitter.close_tool("stamped", status=ToolEndStatus.OK, completed_at=at(400), reported_duration_ms=12.0)
        emitter.close_tool("orphan", status=ToolEndStatus.UNRESOLVED, completed_at=None, reported_duration_ms=12.0)
        commands = {c.tool_id: c for c in emitter.finalize(AgentEndStatus.COMPLETED).record.commands}
        assert {tool_id: c.duration_ms for tool_id, c in commands.items()} == {
            "reported": 12.0,
            "zero": None,
            "stamped": pytest.approx(300.0),
            "orphan": None,
        }

    def test_a_reported_duration_is_refused_under_the_turn_clock(self) -> None:
        emitter, _, _ = _emitter()
        emitter.open_tool("c1", "Bash", {})
        with pytest.raises(TypeError, match="TURN_CLOCK"):
            emitter.close_tool("c1", status=ToolEndStatus.OK, reported_duration_ms=5.0)

    def test_a_start_at_the_result_is_refused_under_the_turn_clock(self) -> None:
        emitter, _, _ = _emitter()
        emitter.open_tool("c1", "Bash", {})
        with pytest.raises(TypeError, match="TURN_CLOCK"):
            emitter.close_tool("c1", status=ToolEndStatus.OK, started_at=at(1))

    def test_a_completion_before_the_start_is_a_zero_duration(self) -> None:
        emitter, _, _ = _emitter(TimingBasis.CLI_EPOCH_MS)
        emitter.open_tool("c1", "Bash", {}, started_at=at(5000))
        emitter.close_tool("c1", status=ToolEndStatus.OK, completed_at=at(4000))
        assert emitter.finalize(AgentEndStatus.COMPLETED).record.commands[0].duration_ms == 0.0

    def test_an_unknown_id_close_under_cli_epoch_still_needs_its_stamp(self) -> None:
        emitter, _, _ = _emitter(TimingBasis.CLI_EPOCH_MS)
        with pytest.raises(TypeError, match="completed_at"):
            emitter.close_tool("ghost", status=ToolEndStatus.OK)

    def test_turn_clock_stamps_tools_from_the_clock(self) -> None:
        emitter, clock, _ = _emitter()
        clock.ms = 700
        emitter.open_tool("c1", "Bash", {"command": "ls"}, generation_completed=True)
        clock.ms = 1200
        emitter.close_tool("c1", status=ToolEndStatus.OK, summary="out")
        command = emitter.finalize(AgentEndStatus.COMPLETED).record.commands[0]
        assert command.timestamp == command.execution_started_at == command.generation_completed_at == at(700)
        assert command.execution_completed_at == at(1200)
        assert command.duration_ms == pytest.approx(500.0)
        assert (command.result_status, command.result_summary) == ("success", "out")


class TestTools:
    def test_sequence_numbers_start_at_zero_in_open_order(self) -> None:
        emitter, _, _ = _emitter()
        for tool_id in ("a", "b", "c"):
            emitter.open_tool(tool_id, "Bash", {})
        emitter.close_tool("c", status=ToolEndStatus.OK)
        emitter.close_tool("a", status=ToolEndStatus.OK)
        emitter.close_tool("b", status=ToolEndStatus.OK)
        record = emitter.finalize(AgentEndStatus.COMPLETED).record
        assert [(c.tool_id, c.sequence_number) for c in record.commands] == [("a", 0), ("b", 1), ("c", 2)]

    def test_an_unresolved_tool_keeps_its_start_and_gets_no_completion(self) -> None:
        emitter, clock, _ = _emitter()
        clock.ms = 10
        emitter.open_tool("c1", "Bash", {})
        clock.ms = 20
        emitter.close_tool("c1", status=ToolEndStatus.UNRESOLVED)
        command = emitter.finalize(AgentEndStatus.COMPLETED).record.commands[0]
        assert command.execution_started_at == at(10)
        assert command.execution_completed_at is None and command.duration_ms is None
        assert command.result_status == "unknown"

    def test_the_orphan_sweep_closes_an_open_tool_unresolved(self) -> None:
        emitter, _, sink = _emitter()
        emitter.open_tool("c1", "Bash", {})
        record = emitter.fail(AgentEndStatus.CRASHED, "died").record
        ends = sink.of(ToolEndEvent)
        assert [e.status for e in ends] == [ToolEndStatus.UNRESOLVED]
        command = record.commands[0]
        assert command.result_status == "unknown"
        assert command.execution_started_at is not None
        assert (command.execution_completed_at, command.duration_ms, command.error_message) == (None, None, None)

    def test_a_close_for_an_unknown_id_synthesizes_a_call(self) -> None:
        emitter, _, _ = _emitter()
        emitter.open_tool("known", "Bash", {})
        emitter.close_tool("ghost", status=ToolEndStatus.ERROR, error="no start")
        commands = {c.tool_id: c for c in emitter.fail(AgentEndStatus.CRASHED, "x").record.commands}
        assert commands["ghost"].tool_name == "unknown"
        assert commands["ghost"].sequence_number == 1
        assert commands["ghost"].result_status == "error"

    def test_a_second_close_on_the_same_id_keeps_the_first_record(self) -> None:
        emitter, clock, sink = _emitter()
        emitter.open_tool("c1", "Bash", {})
        clock.ms = 10
        emitter.close_tool("c1", status=ToolEndStatus.OK)
        clock.ms = 20
        emitter.close_tool("c1", status=ToolEndStatus.ERROR, error="late")
        emitter.open_tool("c1", "Bash", {})
        command = emitter.finalize(AgentEndStatus.COMPLETED).record.commands
        assert [(c.tool_name, c.duration_ms, c.result_status) for c in command] == [("Bash", 10.0, "success")]
        assert (len(sink.of(ToolStartEvent)), len(sink.of(ToolEndEvent))) == (1, 1)

    def test_close_refreshes_parameters_and_result_data(self) -> None:
        emitter, _, _ = _emitter()
        emitter.open_tool("c1", "Bash", {"a": 1})
        emitter.close_tool("c1", status=ToolEndStatus.PERMISSION_DENIED, parameters={"b": 2}, result_data=[1])
        command = emitter.finalize(AgentEndStatus.COMPLETED).record.commands[0]
        assert (command.parameters, command.result_data, command.result_status) == ({"b": 2}, [1], "error")

    def test_a_tool_end_carries_its_opening_turn_id(self) -> None:
        emitter, _, sink = _emitter()
        emitter.begin_inner_turn("turn_1")
        emitter.open_tool("c1", "Bash", {})
        emitter.end_inner_turn()
        emitter.begin_inner_turn("turn_2")
        emitter.close_tool("c1", status=ToolEndStatus.OK)
        assert sink.of(ToolStartEvent)[0].turn_id == sink.of(ToolEndEvent)[0].turn_id == "turn_1"


class TestInnerTurns:
    def test_begin_while_open_raises(self) -> None:
        emitter, _, _ = _emitter()
        emitter.begin_inner_turn("a")
        with pytest.raises(RuntimeError, match="still open"):
            emitter.begin_inner_turn("b")

    def test_end_with_none_open_raises(self) -> None:
        emitter, _, _ = _emitter()
        with pytest.raises(RuntimeError, match="no inner turn"):
            emitter.end_inner_turn()

    @pytest.mark.parametrize(
        ("end", "expected"),
        [("finalize", TurnEndStatus.TOOL_CALLS_EXHAUSTED), ("fail", TurnEndStatus.TIMEOUT)],
    )
    def test_the_end_closes_an_open_inner_turn_with_the_mapped_status(self, end: str, expected: TurnEndStatus) -> None:
        emitter, _, sink = _emitter()
        emitter.begin_inner_turn("a")
        assert emitter.inner_turn_open
        if end == "finalize":
            emitter.finalize(AgentEndStatus.TOOL_CALLS_EXHAUSTED)
        else:
            emitter.fail(AgentEndStatus.TIMEOUT, "late")
        turn_end = sink.of(TurnEndEvent)[0]
        assert (turn_end.turn_id, turn_end.status, turn_end.tokens) == ("a", expected, None)
        assert isinstance(sink.events[-1], AgentEndEvent)

    def test_turns_are_counted_on_the_main_thread_only(self) -> None:
        emitter, _, sink = _emitter()
        emitter.begin_inner_turn("a")
        emitter.end_inner_turn()
        emitter.begin_inner_turn("sub", "sub-model", parent_tool_id="agent_1")
        emitter.end_inner_turn()
        end = emitter.finalize(AgentEndStatus.COMPLETED)
        assert end.record.assistant_turn_count == 1
        assert end.record.num_turns == 1
        assert [e.model for e in sink.of(TurnStartEvent)] == ["m", "sub-model"]


class TestGenerations:
    def test_two_parts_share_bounds_and_apportion_by_output(self) -> None:
        emitter, _, _ = _emitter()
        window = close_window(mark=at(0), now=at(1000))
        messages = emitter.add_generation(
            message_id="g1", window=window, parts=[_part(_text("a"), output=1), _part(_text("b"), output=2)]
        )
        assert {(m.started_at, m.completed_at, m.message_id) for m in messages} == {(at(0), at(1000), "g1")}
        assert messages[0].generation_duration_ms == round(1000 / 3, 6)
        assert math.isclose(sum(m.generation_duration_ms or 0 for m in messages), 1000.0, abs_tol=1e-6)

    def test_parts_with_no_output_split_evenly(self) -> None:
        emitter, _, _ = _emitter()
        window = Window(started_at=at(0), completed_at=at(90))
        messages = emitter.add_generation(message_id=None, window=window, parts=[_part(), _part(), _part()])
        assert [m.generation_duration_ms for m in messages] == pytest.approx([30.0, 30.0, 30.0])

    def test_empty_parts_raise(self) -> None:
        emitter, _, _ = _emitter()
        with pytest.raises(ValueError, match="at least one part"):
            emitter.add_generation(message_id=None, window=Window(at(0), at(1)), parts=[])

    def test_message_id_is_a_required_keyword(self) -> None:
        emitter, _, _ = _emitter()
        with pytest.raises(TypeError):
            emitter.add_generation(window=Window(at(0), at(1)), parts=[_part()])  # type: ignore[call-arg]

    def test_the_message_carries_its_part_and_the_live_blocks(self) -> None:
        emitter, _, _ = _emitter()
        block = _tool_use("c1")
        part = Generation(
            blocks=[_text("x"), block],
            tokens=TokenUsage(
                uncached_input_tokens=1, output_tokens=2, cache_creation_input_tokens=3, cache_read_input_tokens=4
            ),
            reasoning_tokens=1,
            stop_reason="tool_use",
        )
        (message,) = emitter.add_generation(
            message_id="g", window=Window(at(0), at(5)), parts=[part], parent_tool_id="agent_1", model="sub"
        )
        assert message.tool_use_ids == ["c1"]
        assert message.content_blocks[1] is block
        assert (
            message.input_tokens,
            message.output_tokens,
            message.cache_creation_tokens,
            message.cache_read_tokens,
        ) == (
            1,
            2,
            3,
            4,
        )
        assert (message.reasoning_tokens, message.stop_reason, message.model) == (1, "tool_use", "sub")
        assert message.parent_tool_use_id == "agent_1"
        block.is_error = True
        end = emitter.finalize(AgentEndStatus.COMPLETED)
        assert end.record.messages[0].content_blocks[1].is_error is True  # type: ignore[union-attr]

    def test_an_unmeasured_generation_has_equal_bounds_and_no_duration(self) -> None:
        emitter, clock, _ = _emitter()
        clock.ms = 42
        message = emitter.add_unmeasured_generation(message_id="u", part=_part(_text("late")))
        assert message.started_at == message.completed_at == at(42)
        assert message.generation_duration_ms is None
        assert message.model == "m"


class TestThreads:
    def test_nested_events_carry_the_parent_tool_as_thread_and_parent(self) -> None:
        emitter, _, sink = _emitter()
        emitter.begin_inner_turn("sub", parent_tool_id="agent_1")
        emitter.text("child says", parent_tool_id="agent_1")
        emitter.open_tool("c1", "Bash", {}, parent_tool_id="agent_1")
        emitter.close_tool("c1", status=ToolEndStatus.OK)
        emitter.end_inner_turn()
        nested = sink.events[1:5]
        assert {(e.thread_id, e.parent_thread_id) for e in nested} == {("agent_1", "agent_1")}
        assert sink.of(TurnEndEvent)[0].parent_thread_id == "agent_1"
        end = emitter.finalize(AgentEndStatus.COMPLETED)
        assert end.record.agent_output == ""

    def test_main_thread_events_have_no_thread_id(self) -> None:
        emitter, _, sink = _emitter()
        emitter.begin_inner_turn("a")
        emitter.text("hi")
        emitter.open_tool("c1", "Bash", {})
        emitter.close_tool("c1", status=ToolEndStatus.OK)
        emitter.finalize(AgentEndStatus.COMPLETED)
        assert {(e.thread_id, e.parent_thread_id) for e in sink.events} == {(None, None)}


class TestTokens:
    def test_the_end_publishes_the_sum_of_turn_deltas_by_default(self) -> None:
        emitter, _, sink = _emitter()
        for n in (1, 2):
            emitter.begin_inner_turn(f"t{n}")
            emitter.end_inner_turn(tokens=TokenUsage(output_tokens=n))
        record = emitter.finalize(AgentEndStatus.COMPLETED).record
        assert record.token_usage == TokenUsage(output_tokens=3)
        assert sink.of(AgentEndEvent)[0].usage.output_tokens == 3

    def test_no_turn_and_no_usage_publishes_no_token_usage(self) -> None:
        emitter, _, _ = _emitter()
        assert emitter.finalize(AgentEndStatus.COMPLETED).record.token_usage is None

    def test_deltas_over_the_published_usage_warn_once_and_do_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        emitter, _, _ = _emitter()
        emitter.begin_inner_turn("a")
        emitter.end_inner_turn(tokens=TokenUsage(output_tokens=10, cache_read_input_tokens=5))
        with caplog.at_level("WARNING", logger="coder_eval.streaming.emitter"):
            outcome = emitter.finalize(AgentEndStatus.COMPLETED, usage=TokenUsage(output_tokens=4, total_cost_usd=9.0))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "output_tokens 10 > 4" in warnings[0].getMessage()
        assert "cache_read_input_tokens 5 > 0" in warnings[0].getMessage()
        assert "[t]" in warnings[0].getMessage()
        assert outcome.status is AgentEndStatus.COMPLETED

    def test_a_cost_difference_is_not_a_bucket(self, caplog: pytest.LogCaptureFixture) -> None:
        emitter, _, _ = _emitter()
        emitter.begin_inner_turn("a")
        emitter.end_inner_turn(tokens=TokenUsage(output_tokens=1, total_cost_usd=5.0))
        with caplog.at_level("WARNING", logger="coder_eval.streaming.emitter"):
            emitter.finalize(AgentEndStatus.COMPLETED, usage=TokenUsage(output_tokens=1, total_cost_usd=1.0))
        assert not caplog.records


class TestTheEnd:
    def test_fail_timeout_is_a_crashed_record_with_the_untruncated_error(self) -> None:
        emitter, _, _ = _emitter()
        reason = "x" * (CRASH_REASON_MAX_CHARS + 50)
        outcome = emitter.fail(AgentEndStatus.TIMEOUT, reason)
        assert outcome.status is AgentEndStatus.TIMEOUT
        assert outcome.error == reason
        assert outcome.record.crashed is True
        assert outcome.record.crash_reason is not None
        assert len(outcome.record.crash_reason) == CRASH_REASON_MAX_CHARS + 1
        assert outcome.record.result_summary is None
        assert outcome.record.iteration == 3

    def test_finalize_summary_is_the_final_text_reply(self) -> None:
        emitter, _, _ = _emitter()
        emitter.add_generation(message_id=None, window=Window(at(0), at(1)), parts=[_part(_tool_use("c1"))])
        emitter.add_generation(
            message_id=None, window=Window(at(1), at(2)), parts=[_part(_text("Done "), _text("now."))]
        )
        emitter.add_unmeasured_generation(message_id=None, part=_part(_text("child")), parent_tool_id="agent_1")
        summary = emitter.finalize(AgentEndStatus.COMPLETED, stop_reason="end_turn").record.result_summary
        assert summary == ResultSummary(is_error=False, subtype="completed", stop_reason="end_turn", result="Done now.")

    def test_a_last_message_that_calls_a_tool_is_no_final_reply(self) -> None:
        emitter, _, _ = _emitter()
        emitter.add_generation(
            message_id=None, window=Window(at(0), at(1)), parts=[_part(_text("calling"), _tool_use("c1"))]
        )
        summary = emitter.finalize(AgentEndStatus.TOOL_CALLS_EXHAUSTED).record.result_summary
        assert summary is not None and summary.result is None and summary.subtype == "tool_calls_exhausted"

    def test_text_after_the_last_tool_call_in_the_same_generation_is_the_final_reply(self) -> None:
        emitter, _, _ = _emitter()
        emitter.add_generation(
            message_id=None, window=Window(at(0), at(1)), parts=[_part(_tool_use("c1"), _text("All set."))]
        )
        summary = emitter.finalize(AgentEndStatus.COMPLETED).record.result_summary
        assert summary is not None and summary.result == "All set."

    @pytest.mark.parametrize("given", [None, ResultSummary(is_error=True, subtype="sdk", result="detail")])
    def test_an_explicit_result_summary_wins(self, given: ResultSummary | None) -> None:
        emitter, _, _ = _emitter()
        emitter.add_generation(message_id=None, window=Window(at(0), at(1)), parts=[_part(_text("reply"))])
        assert emitter.finalize(AgentEndStatus.COMPLETED, result_summary=given).record.result_summary == given

    @pytest.mark.parametrize("status", [AgentEndStatus.CRASHED, AgentEndStatus.TIMEOUT])
    def test_finalize_refuses_a_failed_status(self, status: AgentEndStatus) -> None:
        emitter, _, _ = _emitter()
        with pytest.raises(ValueError, match="fail"):
            emitter.finalize(status)

    def test_fail_refuses_a_clean_status(self) -> None:
        emitter, _, _ = _emitter()
        with pytest.raises(ValueError, match="finalize"):
            emitter.fail(AgentEndStatus.COMPLETED, "x")  # type: ignore[arg-type]

    def test_the_end_is_idempotent(self) -> None:
        emitter, _, sink = _emitter()
        first = emitter.finalize(AgentEndStatus.COMPLETED)
        assert emitter.finalize(AgentEndStatus.STOPPED_EARLY) is first
        assert emitter.fail(AgentEndStatus.CRASHED, "late") is first
        assert len(sink.of(AgentEndEvent)) == 1

    def test_writes_after_the_end_are_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        emitter, _, sink = _emitter()
        emitter.begin_inner_turn("a")
        emitter.fail(AgentEndStatus.CRASHED, "gone")
        count = len(sink.events)
        with caplog.at_level("DEBUG", logger="coder_eval.streaming.emitter"):
            emitter.text("late")
            emitter.open_tool("c1", "Bash", {})
            emitter.close_tool("c1", status=ToolEndStatus.OK)
            emitter.begin_inner_turn("b")
            emitter.end_inner_turn()
            late = emitter.add_generation(message_id=None, window=Window(at(0), at(1)), parts=[_part(_text("late"))])
            emitter.add_unmeasured_generation(message_id=None, part=_part())
        assert len(sink.events) == count
        assert late[0] not in sink.of(AgentEndEvent)[0].messages
        assert sink.of(AgentEndEvent)[0].messages == []
        assert len([r for r in caplog.records if "dropped" in r.getMessage()]) == 1

    def test_default_agent_output_is_the_joined_main_thread_text(self) -> None:
        emitter, _, sink = _emitter()
        emitter.text("Hello, ")
        emitter.text("ignored", parent_tool_id="agent_1")
        emitter.text("world")
        outcome = emitter.finalize(AgentEndStatus.COMPLETED)
        assert outcome.record.agent_output == "Hello, world"
        assert [e.text for e in sink.of(TextChunkEvent)] == ["Hello, ", "ignored", "world"]

    def test_explicit_payload_overrides(self) -> None:
        emitter, _, _ = _emitter()
        emitter.text("streamed")
        record = emitter.finalize(
            AgentEndStatus.COMPLETED, agent_output="final", model_used="m2", assistant_turn_count=4, num_turns=5
        ).record
        assert (record.agent_output, record.model_used, record.assistant_turn_count, record.num_turns) == (
            "final",
            "m2",
            4,
            5,
        )

    @pytest.mark.parametrize("end", ["finalize", "fail"])
    def test_an_explicit_none_num_turns_is_recorded_and_omitted_counts_main_turns(self, end: str) -> None:
        def _end(emitter: TurnEmitter, **kwargs: Any) -> TurnRecord:
            if end == "finalize":
                return emitter.finalize(AgentEndStatus.COMPLETED, **kwargs).record
            return emitter.fail(AgentEndStatus.CRASHED, "boom", **kwargs).record

        counted, _, _ = _emitter()
        counted.begin_inner_turn("a")
        explicit, _, _ = _emitter()
        explicit.begin_inner_turn("a")
        assert _end(counted).num_turns == 1
        assert _end(explicit, num_turns=None).num_turns is None

    def test_a_failed_turn_takes_an_explicit_model_used(self) -> None:
        emitter, _, _ = _emitter()
        assert emitter.fail(AgentEndStatus.TIMEOUT, "late", model_used="observed").record.model_used == "observed"


def _outcome(status: AgentEndStatus, error: str | None = None) -> TurnOutcome:
    emitter, _, _ = _emitter()
    return emitter.fail(status, error or "") if error is not None else emitter.finalize(status)  # type: ignore[arg-type]


class TestRecordOrRaise:
    @pytest.mark.parametrize(
        "status",
        [s for s in AgentEndStatus if s not in (AgentEndStatus.CRASHED, AgentEndStatus.TIMEOUT)],
    )
    def test_a_clean_status_returns_the_record(self, status: AgentEndStatus) -> None:
        outcome = _outcome(status)
        assert outcome.record_or_raise() is outcome.record

    def test_crashed_raises_agent_crash_error_with_the_full_error(self) -> None:
        with pytest.raises(AgentCrashError, match="provider exploded"):
            _outcome(AgentEndStatus.CRASHED, "provider exploded").record_or_raise()

    def test_timeout_raises_turn_timeout_error(self) -> None:
        with pytest.raises(TurnTimeoutError) as raised:
            _outcome(AgentEndStatus.TIMEOUT, "late").record_or_raise(timeout_seconds=30, task_id="t9", iteration=2)
        assert raised.value.timeout_seconds == 30
        assert raised.value.iteration == 2


class TestEndRobustness:
    def test_duration_seconds_is_exactly_the_bracket(self) -> None:
        emitter, clock, sink = _emitter()
        clock.ms = 2500
        record = emitter.finalize(AgentEndStatus.COMPLETED).record
        start, end = sink.of(AgentStartEvent)[0].timestamp, sink.of(AgentEndEvent)[0].timestamp
        assert record.duration_seconds == (end - start).total_seconds() == 2.5

    def test_under_the_wall_clock_the_duration_is_monotonic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coder_eval.streaming import emitter as emitter_module

        ticks = iter([100.0, 101.5])
        monkeypatch.setattr(emitter_module.time, "monotonic", lambda: next(ticks))
        emitter, clock, _ = _emitter(TimingBasis.CLI_EPOCH_MS)
        clock.ms = 3_600_000  # a wall-clock step of an hour
        assert emitter.finalize(AgentEndStatus.COMPLETED).record.duration_seconds == pytest.approx(1.5)

    def test_a_record_that_cannot_be_built_ends_the_turn_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coder_eval.streaming.collector import EventCollector

        emitter, _, sink = _emitter()

        def explode(_self: Any) -> Any:
            raise ValueError("bad record")

        monkeypatch.setattr(EventCollector, "build_turn_record", explode)
        with pytest.raises(ValueError, match="bad record"):
            emitter.finalize(AgentEndStatus.COMPLETED)
        with pytest.raises(RuntimeError, match="could not be built"):
            emitter.fail(AgentEndStatus.CRASHED, "again")
        emitter.text("late")
        assert len(sink.of(AgentEndEvent)) == 1
        assert not sink.of(TextChunkEvent)
