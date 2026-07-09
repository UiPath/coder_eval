"""Tests for streaming callback protocol and helpers."""

import logging
from datetime import datetime

from coder_eval.models import CommandTelemetry
from coder_eval.streaming.callbacks import StreamCallback, TaskScopedCallback, safe_emit
from coder_eval.streaming.events import AgentStartEvent, StreamEvent, ToolStartEvent


def _make_tool(tool_name: str, tool_id: str, parameters: dict | None = None) -> CommandTelemetry:
    """Build a minimal CommandTelemetry for ToolStart/ToolEnd events."""
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id,
        timestamp=datetime.now(),
        parameters=parameters or {},
    )


class _CollectingCallback:
    """Test helper that collects events into a list."""

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)


class _ExplodingCallback:
    """Test helper that raises on every event."""

    def on_event(self, event: StreamEvent) -> None:
        raise RuntimeError("boom")


def test_collecting_callback_satisfies_protocol():
    """CollectingCallback implements StreamCallback."""
    cb: StreamCallback = _CollectingCallback()
    event = AgentStartEvent(task_id="t", iteration=1, prompt="")
    cb.on_event(event)


def test_safe_emit_delivers_event():
    """safe_emit calls on_event when callback is provided."""
    cb = _CollectingCallback()
    event = AgentStartEvent(task_id="t", iteration=1, prompt="")
    safe_emit(cb, event)
    assert len(cb.events) == 1
    assert cb.events[0] is event


def test_safe_emit_skips_none_callback():
    """safe_emit does nothing when callback is None."""
    event = AgentStartEvent(task_id="t", iteration=1, prompt="")
    safe_emit(None, event)  # Should not raise


def test_safe_emit_catches_callback_exception(caplog):
    """safe_emit catches and logs exceptions from the callback."""
    cb = _ExplodingCallback()
    event = AgentStartEvent(task_id="t", iteration=1, prompt="")
    logger = logging.getLogger("coder_eval.streaming.callbacks")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.WARNING)
    try:
        safe_emit(cb, event)  # Should not raise
    finally:
        logger.removeHandler(caplog.handler)
    assert "Stream callback failed" in caplog.text


def test_task_scoped_callback_overrides_task_id():
    """TaskScopedCallback stamps the correct task_id on all forwarded events."""
    inner = _CollectingCallback()
    scoped = TaskScopedCallback(inner, task_id="real-task-42")
    event = ToolStartEvent(task_id="wrong-id", turn_id="turn-1", tool=_make_tool("Bash", "t1"))
    scoped.on_event(event)
    assert len(inner.events) == 1
    assert inner.events[0].task_id == "real-task-42"


def test_task_scoped_callback_preserves_event_data():
    """TaskScopedCallback only changes task_id, not other fields."""
    inner = _CollectingCallback()
    scoped = TaskScopedCallback(inner, task_id="my-task")
    event = ToolStartEvent(
        task_id="agent-type",
        turn_id="turn-5",
        tool=_make_tool("Read", "t5", parameters={"path": "/foo"}),
    )
    scoped.on_event(event)
    forwarded = inner.events[0]
    assert isinstance(forwarded, ToolStartEvent)
    assert forwarded.turn_id == "turn-5"
    assert forwarded.tool.tool_name == "Read"
    assert forwarded.tool.tool_id == "t5"
    assert forwarded.tool.parameters == {"path": "/foo"}
