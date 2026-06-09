"""Tests for the cross-process stream-event wire format.

Covers:
- round-trip of every event class
- prefix detection vs. parse failure (the host needs both to preserve logs)
- collision: a plain log line that *happens* to begin with the prefix must
  not be silently swallowed (the prefix uses control chars precisely so
  this is hard to hit, but we test the invariant explicitly).
"""

from datetime import datetime

from coder_eval.models import CommandTelemetry, TokenUsage
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    CriteriaCheckEvent,
    CriterionSummary,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.streaming.wire import LINE_PREFIX, deserialize_event, has_prefix, serialize_event


def _telemetry(tool_name: str = "Bash", tool_id: str = "x", **kwargs) -> CommandTelemetry:
    return CommandTelemetry(tool_name=tool_name, tool_id=tool_id, timestamp=datetime(2025, 1, 1), **kwargs)


class TestRoundTrip:
    def test_agent_start_event(self):
        ev = AgentStartEvent(task_id="t1", iteration=2, prompt="hi", model="opus", thread_id="A")
        roundtripped = deserialize_event(serialize_event(ev))
        assert isinstance(roundtripped, AgentStartEvent)
        assert roundtripped.task_id == "t1"
        assert roundtripped.iteration == 2
        assert roundtripped.prompt == "hi"
        assert roundtripped.model == "opus"

    def test_turn_start_event(self):
        ev = TurnStartEvent(task_id="t", turn_id="t1", thread_id="A", model="opus")
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, TurnStartEvent)
        assert rt.turn_id == "t1"
        assert rt.model == "opus"

    def test_tool_start_event(self):
        ev = ToolStartEvent(
            task_id="t",
            turn_id="t1",
            tool=_telemetry(parameters={"cmd": "ls"}, sequence_number=3),
        )
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, ToolStartEvent)
        # Nested CommandTelemetry must rehydrate, not stay a raw dict.
        assert isinstance(rt.tool, CommandTelemetry)
        assert rt.tool.parameters == {"cmd": "ls"}
        assert rt.tool.sequence_number == 3

    def test_tool_end_event(self):
        ev = ToolEndEvent(
            task_id="t",
            turn_id="t1",
            tool=_telemetry(result_summary="boom"),
            status=ToolEndStatus.ERROR,
        )
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, ToolEndEvent)
        assert isinstance(rt.tool, CommandTelemetry)
        assert rt.status is ToolEndStatus.ERROR
        assert rt.tool.result_summary == "boom"

    def test_text_chunk_event(self):
        ev = TextChunkEvent(task_id="t", turn_id="t1", text="hello")
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, TextChunkEvent)
        assert rt.text == "hello"

    def test_turn_end_event(self):
        ev = TurnEndEvent(
            task_id="t",
            turn_id="t1",
            status=TurnEndStatus.COMPLETED,
            tokens=TokenUsage(uncached_input_tokens=10, output_tokens=5),
        )
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, TurnEndEvent)
        assert rt.status is TurnEndStatus.COMPLETED
        assert isinstance(rt.tokens, TokenUsage)
        assert rt.tokens.input_tokens == 10

    def test_agent_end_event(self):
        ev = AgentEndEvent(
            task_id="t",
            status=AgentEndStatus.COMPLETED,
            usage=TokenUsage(uncached_input_tokens=20, output_tokens=8),
            iteration=4,
            duration_seconds=1.5,
            agent_output="done",
        )
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, AgentEndEvent)
        assert rt.status is AgentEndStatus.COMPLETED
        assert rt.duration_seconds == 1.5
        assert rt.iteration == 4
        assert isinstance(rt.usage, TokenUsage)
        assert rt.usage.input_tokens == 20

    def test_criteria_check_event_with_nested_summary(self):
        ev = CriteriaCheckEvent(
            task_id="t",
            passed=2,
            total=3,
            weighted_score=0.75,
            criteria=[
                CriterionSummary(criterion_type="file_exists", description="x", score=1.0, passed=True),
                CriterionSummary(
                    criterion_type="pytest", description="y", score=0.0, passed=False, failure_reason="oops"
                ),
            ],
        )
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, CriteriaCheckEvent)
        assert len(rt.criteria) == 2
        # Nested models must rehydrate as CriterionSummary, not raw dict.
        assert isinstance(rt.criteria[0], CriterionSummary)
        assert rt.criteria[1].failure_reason == "oops"

    def test_timestamp_roundtrip(self):
        ts = datetime(2025, 1, 2, 3, 4, 5)
        ev = AgentStartEvent(task_id="t", timestamp=ts, iteration=0)
        rt = deserialize_event(serialize_event(ev))
        assert rt is not None
        assert rt.timestamp == ts


class TestPrefixDetectionAndCollision:
    def test_plain_log_line_returns_none(self):
        assert deserialize_event("just a regular pytest output line") is None
        assert deserialize_event("FAILED tests/test_x.py::test_y") is None
        assert deserialize_event("") is None

    def test_has_prefix_negative_on_plain_lines(self):
        assert has_prefix("STREAM_EVENT: not a real event") is False
        assert has_prefix("some output") is False

    def test_prefix_uses_control_chars(self):
        # The prefix is intentionally framed by ASCII Record Separator
        # (U+001E) so collision with real agent / tool output is
        # essentially impossible.
        assert "\x1e" in LINE_PREFIX

    def test_malformed_event_returns_none_but_caller_can_preserve(self):
        # Prefix is present, payload is garbage. deserialize_event returns
        # None; has_prefix returns True so the host knows to preserve the
        # raw line in docker.log instead of silently dropping it.
        garbage = LINE_PREFIX + "not valid json {{{"
        assert has_prefix(garbage) is True
        assert deserialize_event(garbage) is None

    def test_unknown_event_class_returns_none(self):
        import json

        line = LINE_PREFIX + json.dumps({"cls": "TotallyMadeUpEvent", "data": {"task_id": "t"}})
        assert has_prefix(line) is True
        assert deserialize_event(line) is None
