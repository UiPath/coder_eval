"""Streaming callback protocol and helpers."""

import logging
from typing import Protocol

from coder_eval.streaming.events import StreamEvent


logger = logging.getLogger(__name__)


class StreamCallback(Protocol):
    """Protocol for receiving streaming events."""

    def on_event(self, event: StreamEvent) -> None:
        """Handle a streaming event."""
        pass


class TaskScopedCallback:
    """Wrapper that overrides task_id on all events before forwarding to the inner callback.

    Used by the orchestrator to ensure agent-emitted events carry the correct
    task ID, since agents don't know about evaluation-level task identity.
    """

    def __init__(self, inner: StreamCallback, task_id: str) -> None:
        self._inner = inner
        self._task_id = task_id

    def on_event(self, event: StreamEvent) -> None:
        event.task_id = self._task_id
        self._inner.on_event(event)


class CompositeStreamCallback:
    """Composite callback that forwards events to multiple handlers.

    Useful for dispatching events to both logging and display renderers.
    """

    def __init__(self, callbacks: list[StreamCallback]) -> None:
        self._callbacks = callbacks

    def on_event(self, event: StreamEvent) -> None:
        for callback in self._callbacks:
            safe_emit(callback, event)


def safe_emit(callback: StreamCallback | None, event: StreamEvent) -> None:
    """Emit an event to the callback, catching and logging any exceptions."""
    if callback is None:
        return
    try:
        callback.on_event(event)
    except Exception:
        logger.warning("Stream callback failed (ignored)", exc_info=True)
