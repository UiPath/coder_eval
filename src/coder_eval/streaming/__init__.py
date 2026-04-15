"""Real-time streaming of agent events to the terminal."""

from coder_eval.streaming.callbacks import StreamCallback, TaskScopedCallback, safe_emit
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
from coder_eval.streaming.renderers import RichStreamRenderer


__all__ = [
    "CriteriaCheckEvent",
    "CriterionSummary",
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
