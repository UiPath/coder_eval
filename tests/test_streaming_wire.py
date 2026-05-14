"""Tests for the cross-process stream-event wire format.

Covers:
- round-trip of every event class
- prefix detection vs. parse failure (the host needs both to preserve logs)
- collision: a plain log line that *happens* to begin with the prefix must
  not be silently swallowed (the prefix uses control chars precisely so
  this is hard to hit, but we test the invariant explicitly).
"""

from datetime import datetime

from coder_eval.streaming.events import (
    CriteriaCheckEvent,
    CriterionSummary,
    TextChunkEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    TurnStartEvent,
)
from coder_eval.streaming.wire import LINE_PREFIX, deserialize_event, has_prefix, serialize_event


class TestRoundTrip:
    def test_turn_start_event(self):
        ev = TurnStartEvent(task_id="t1", iteration=2, prompt_preview="hi")
        roundtripped = deserialize_event(serialize_event(ev))
        assert isinstance(roundtripped, TurnStartEvent)
        assert roundtripped.task_id == "t1"
        assert roundtripped.iteration == 2
        assert roundtripped.prompt_preview == "hi"

    def test_tool_call_event(self):
        ev = ToolCallEvent(task_id="t", tool_name="Bash", tool_id="x", parameters={"cmd": "ls"}, sequence_number=3)
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, ToolCallEvent)
        assert rt.parameters == {"cmd": "ls"}
        assert rt.sequence_number == 3

    def test_tool_result_event(self):
        ev = ToolResultEvent(task_id="t", tool_id="x", tool_name="Bash", success=False, result_preview="boom")
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, ToolResultEvent)
        assert rt.success is False

    def test_text_chunk_event(self):
        ev = TextChunkEvent(task_id="t", text="hello")
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, TextChunkEvent)
        assert rt.text == "hello"

    def test_turn_complete_event(self):
        ev = TurnCompleteEvent(task_id="t", iteration=4, duration_s=1.5, command_count=2)
        rt = deserialize_event(serialize_event(ev))
        assert isinstance(rt, TurnCompleteEvent)
        assert rt.duration_s == 1.5

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
        # Nested dataclasses must rehydrate as CriterionSummary, not raw dict.
        assert isinstance(rt.criteria[0], CriterionSummary)
        assert rt.criteria[1].failure_reason == "oops"

    def test_timestamp_roundtrip(self):
        ts = datetime(2025, 1, 2, 3, 4, 5)
        ev = TurnStartEvent(task_id="t", timestamp=ts, iteration=0)
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
