"""Tests for streaming callback protocol and helpers."""

import logging

from coder_eval.streaming.callbacks import StreamCallback, TaskScopedCallback, safe_emit
from coder_eval.streaming.events import StreamEvent, ToolCallEvent, TurnStartEvent


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
    event = TurnStartEvent(task_id="t", iteration=1, max_iterations=1, prompt_preview="")
    cb.on_event(event)


def test_safe_emit_delivers_event():
    """safe_emit calls on_event when callback is provided."""
    cb = _CollectingCallback()
    event = TurnStartEvent(task_id="t", iteration=1, max_iterations=1, prompt_preview="")
    safe_emit(cb, event)
    assert len(cb.events) == 1
    assert cb.events[0] is event


def test_safe_emit_skips_none_callback():
    """safe_emit does nothing when callback is None."""
    event = TurnStartEvent(task_id="t", iteration=1, max_iterations=1, prompt_preview="")
    safe_emit(None, event)  # Should not raise


def test_safe_emit_catches_callback_exception(caplog):
    """safe_emit catches and logs exceptions from the callback."""
    cb = _ExplodingCallback()
    event = TurnStartEvent(task_id="t", iteration=1, max_iterations=1, prompt_preview="")
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
    event = ToolCallEvent(task_id="wrong-id", tool_name="Bash", tool_id="t1")
    scoped.on_event(event)
    assert len(inner.events) == 1
    assert inner.events[0].task_id == "real-task-42"


def test_task_scoped_callback_preserves_event_data():
    """TaskScopedCallback only changes task_id, not other fields."""
    inner = _CollectingCallback()
    scoped = TaskScopedCallback(inner, task_id="my-task")
    event = ToolCallEvent(task_id="agent-type", tool_name="Read", tool_id="t5", parameters={"path": "/foo"})
    scoped.on_event(event)
    forwarded = inner.events[0]
    assert isinstance(forwarded, ToolCallEvent)
    assert forwarded.tool_name == "Read"
    assert forwarded.tool_id == "t5"
    assert forwarded.parameters == {"path": "/foo"}
