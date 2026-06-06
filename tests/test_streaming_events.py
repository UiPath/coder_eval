"""Tests for streaming event models (new standardized event protocol)."""

from datetime import datetime

from coder_eval.models import AgentUsage, CommandTelemetry, TokenUsage
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    CriteriaCheckEvent,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)


def _telemetry(tool_name: str = "Bash", tool_id: str = "tool_123", **params: object) -> CommandTelemetry:
    """Build a minimal CommandTelemetry for tool events."""
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id,
        timestamp=datetime.now(),
        parameters=dict(params),
    )


def test_agent_start_event_creation():
    """AgentStartEvent stores iteration/prompt info and is a StreamEvent.

    (Migrated from the old TurnStartEvent, which used to mean the
    orchestrator/agent-start boundary — that role is now AgentStartEvent.)
    """
    event = AgentStartEvent(
        task_id="test-task",
        iteration=1,
        prompt="Write a hello world...",
        model="claude-opus-4",
    )
    assert isinstance(event, StreamEvent)
    assert event.kind == "agent_start"
    assert event.task_id == "test-task"
    assert event.iteration == 1
    assert event.prompt == "Write a hello world..."
    assert event.model == "claude-opus-4"
    assert isinstance(event.timestamp, datetime)


def test_turn_start_event_creation():
    """TurnStartEvent now marks a single inner turn (task_id/turn_id/model)."""
    event = TurnStartEvent(
        task_id="test-task",
        turn_id="turn-1",
        thread_id="thread-A",
        model="claude-opus-4",
    )
    assert isinstance(event, StreamEvent)
    assert event.kind == "turn_start"
    assert event.task_id == "test-task"
    assert event.turn_id == "turn-1"
    assert event.thread_id == "thread-A"
    assert event.model == "claude-opus-4"


def test_tool_start_event_carries_telemetry():
    """ToolStartEvent carries the invoked tool as CommandTelemetry.

    (Migrated from ToolCallEvent, whose loose tool_name/tool_id/parameters
    fields are now folded into the CommandTelemetry payload.)
    """
    tool = _telemetry(tool_name="Bash", tool_id="tool_123", command="echo hello")
    event = ToolStartEvent(task_id="test-task", turn_id="turn-1", tool=tool)
    assert event.kind == "tool_start"
    assert event.tool.tool_name == "Bash"
    assert event.tool.tool_id == "tool_123"
    assert event.tool.parameters == {"command": "echo hello"}


def test_tool_end_event_status_ok():
    """ToolEndEvent carries the tool result and a terminal ToolEndStatus.

    (Migrated from ToolResultEvent's success/result_preview fields, which the
    ToolEndStatus enum + CommandTelemetry now replace.)
    """
    tool = _telemetry(tool_name="Bash", tool_id="tool_123")
    tool.result_status = "success"
    tool.result_summary = "hello"
    event = ToolEndEvent(task_id="test-task", turn_id="turn-1", tool=tool, status=ToolEndStatus.OK)
    assert event.kind == "tool_end"
    assert event.status == ToolEndStatus.OK
    assert event.tool.result_summary == "hello"


def test_tool_end_event_status_error():
    """A failed tool call is signalled via ToolEndStatus.ERROR (was success=False)."""
    tool = _telemetry(tool_name="Bash", tool_id="tool_123")
    tool.result_status = "error"
    tool.error_message = "boom"
    event = ToolEndEvent(task_id="test-task", turn_id="turn-1", tool=tool, status=ToolEndStatus.ERROR)
    assert event.status == ToolEndStatus.ERROR
    assert event.status != ToolEndStatus.OK
    assert event.tool.error_message == "boom"


def test_tool_end_event_status_unresolved():
    """An orphaned tool call (crash/timeout) is force-closed as UNRESOLVED."""
    tool = _telemetry()
    event = ToolEndEvent(task_id="test-task", turn_id="turn-1", tool=tool, status=ToolEndStatus.UNRESOLVED)
    assert event.status == ToolEndStatus.UNRESOLVED


def test_text_chunk_event_creation():
    """TextChunkEvent stores assistant text output within a turn."""
    event = TextChunkEvent(task_id="test-task", turn_id="turn-1", text="I'll help you with that.")
    assert event.kind == "text_chunk"
    assert event.text == "I'll help you with that."
    assert event.turn_id == "turn-1"


def test_turn_end_event_creation():
    """TurnEndEvent stores the inner-turn terminal status and per-turn tokens.

    (Migrated from TurnCompleteEvent: the old duration/command_count/token_str
    summary now lives on AgentEndEvent; per-turn token usage rides here.)
    """
    tokens = TokenUsage(input_tokens=1000, output_tokens=200)
    event = TurnEndEvent(
        task_id="test-task",
        turn_id="turn-1",
        status=TurnEndStatus.COMPLETED,
        tokens=tokens,
    )
    assert event.kind == "turn_end"
    assert event.status == TurnEndStatus.COMPLETED
    assert event.tokens is not None
    assert event.tokens.input_tokens == 1000
    assert event.tokens.total_tokens == 1200


def test_agent_end_event_creation():
    """AgentEndEvent carries the finalization payload + cumulative usage.

    (The other half of the old TurnCompleteEvent split: turn-level summary
    stats like iteration/duration_seconds now live on the agent-end boundary.)
    """
    usage = AgentUsage(tokens=TokenUsage(input_tokens=1200, output_tokens=200), tool_uses=5)
    event = AgentEndEvent(
        task_id="test-task",
        status=AgentEndStatus.COMPLETED,
        usage=usage,
        iteration=1,
        duration_seconds=12.5,
        agent_output="done",
    )
    assert event.kind == "agent_end"
    assert event.status == AgentEndStatus.COMPLETED
    assert event.iteration == 1
    assert event.duration_seconds == 12.5
    assert event.usage.tool_uses == 5
    assert event.agent_output == "done"


def test_criteria_check_event_creation():
    """CriteriaCheckEvent stores pass/fail summary (unchanged in the new protocol)."""
    event = CriteriaCheckEvent(
        task_id="test-task",
        passed=3,
        total=4,
        weighted_score=0.875,
        details=["file_exists: PASS", "pytest: 2/3"],
    )
    assert event.kind == "criteria_check"
    assert event.passed == 3
    assert event.total == 4
    assert event.weighted_score == 0.875


def test_all_events_have_timestamp():
    """All events auto-generate a timestamp and require a task_id."""
    tool = _telemetry(tool_name="X", tool_id="x")
    events = [
        AgentStartEvent(task_id="t", iteration=1, prompt=""),
        TurnStartEvent(task_id="t", turn_id="turn-1"),
        ToolStartEvent(task_id="t", turn_id="turn-1", tool=tool),
        ToolEndEvent(task_id="t", turn_id="turn-1", tool=tool, status=ToolEndStatus.OK),
        TextChunkEvent(task_id="t", turn_id="turn-1", text=""),
        TurnEndEvent(task_id="t", turn_id="turn-1", status=TurnEndStatus.COMPLETED),
        AgentEndEvent(task_id="t", status=AgentEndStatus.COMPLETED),
        CriteriaCheckEvent(task_id="t", passed=0, total=0, weighted_score=0, details=[]),
    ]
    for event in events:
        assert isinstance(event.timestamp, datetime)
        assert event.task_id == "t"
