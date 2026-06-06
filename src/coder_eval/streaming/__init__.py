"""Real-time streaming of agent events to the terminal."""

from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback, TaskScopedCallback, safe_emit
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    CriteriaCheckEvent,
    CriterionSummary,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.streaming.renderers import LoggingStreamRenderer, RichStreamRenderer


__all__ = [
    "AgentEndEvent",
    "AgentEndStatus",
    "AgentStartEvent",
    "CompositeStreamCallback",
    "CriteriaCheckEvent",
    "CriterionSummary",
    "EventCollector",
    "LoggingStreamRenderer",
    "RichStreamRenderer",
    "StreamCallback",
    "StreamEvent",
    "TaskScopedCallback",
    "TextChunkEvent",
    "ToolEndEvent",
    "ToolEndStatus",
    "ToolStartEvent",
    "TurnEndEvent",
    "TurnEndStatus",
    "TurnStartEvent",
    "safe_emit",
]
