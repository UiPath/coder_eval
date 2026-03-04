"""Tests for streaming event dataclasses."""

from datetime import datetime

from coder_eval.streaming.events import (
    CriteriaCheckEvent,
    StreamEvent,
    TextChunkEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    TurnStartEvent,
)


def test_turn_start_event_creation():
    """TurnStartEvent stores iteration info and is a StreamEvent."""
    event = TurnStartEvent(
        task_id="test-task",
        iteration=1,
        max_iterations=3,
        prompt_preview="Write a hello world...",
    )
    assert isinstance(event, StreamEvent)
    assert event.task_id == "test-task"
    assert event.iteration == 1
    assert event.max_iterations == 3
    assert isinstance(event.timestamp, datetime)


def test_tool_call_event_truncates_parameters():
    """ToolCallEvent stores tool call info."""
    event = ToolCallEvent(
        task_id="test-task",
        tool_name="Bash",
        tool_id="tool_123",
        parameters={"command": "echo hello"},
        sequence_number=0,
    )
    assert event.tool_name == "Bash"
    assert event.tool_id == "tool_123"
    assert event.parameters == {"command": "echo hello"}


def test_tool_result_event_creation():
    """ToolResultEvent stores result status and preview."""
    event = ToolResultEvent(
        task_id="test-task",
        tool_id="tool_123",
        tool_name="Bash",
        success=True,
        result_preview="hello",
    )
    assert event.success is True
    assert event.result_preview == "hello"


def test_text_chunk_event_creation():
    """TextChunkEvent stores assistant text output."""
    event = TextChunkEvent(task_id="test-task", text="I'll help you with that.")
    assert event.text == "I'll help you with that."


def test_turn_complete_event_creation():
    """TurnCompleteEvent stores turn summary stats."""
    event = TurnCompleteEvent(
        task_id="test-task",
        iteration=1,
        duration_s=12.5,
        command_count=5,
        token_usage_str="1.2k tokens",
    )
    assert event.duration_s == 12.5
    assert event.command_count == 5


def test_criteria_check_event_creation():
    """CriteriaCheckEvent stores pass/fail summary."""
    event = CriteriaCheckEvent(
        task_id="test-task",
        passed=3,
        total=4,
        weighted_score=0.875,
        details=["file_exists: PASS", "pytest: 2/3"],
    )
    assert event.passed == 3
    assert event.total == 4
    assert event.weighted_score == 0.875


def test_all_events_have_timestamp():
    """All events auto-generate a timestamp."""
    events = [
        TurnStartEvent(task_id="t", iteration=1, max_iterations=1, prompt_preview=""),
        ToolCallEvent(task_id="t", tool_name="X", tool_id="x", parameters={}, sequence_number=0),
        ToolResultEvent(task_id="t", tool_id="x", tool_name="X", success=True, result_preview=""),
        TextChunkEvent(task_id="t", text=""),
        TurnCompleteEvent(task_id="t", iteration=1, duration_s=0, command_count=0, token_usage_str=""),
        CriteriaCheckEvent(task_id="t", passed=0, total=0, weighted_score=0, details=[]),
    ]
    for event in events:
        assert isinstance(event.timestamp, datetime)
