"""Real-time streaming of agent events to the terminal."""

from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback, TaskScopedCallback, safe_emit
from coder_eval.streaming.events import (
    CriteriaCheckEvent,
    CriterionSummary,
    StreamEvent,
    TextChunkEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    TurnStartEvent,
)
from coder_eval.streaming.renderers import LoggingStreamRenderer, RichStreamRenderer


__all__ = [
    "CompositeStreamCallback",
    "CriteriaCheckEvent",
    "CriterionSummary",
    "LoggingStreamRenderer",
    "RichStreamRenderer",
    "StreamCallback",
    "StreamEvent",
    "TaskScopedCallback",
    "TextChunkEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "TurnCompleteEvent",
    "TurnStartEvent",
    "safe_emit",
]
