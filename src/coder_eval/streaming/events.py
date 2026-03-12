"""Streaming event dataclasses for real-time agent output."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class StreamEvent:
    """Base class for all streaming events."""

    task_id: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class TurnStartEvent(StreamEvent):
    """Emitted when an evaluation iteration begins."""

    iteration: int = 0
    max_iterations: int = 0
    prompt_preview: str = ""


@dataclass
class ToolCallEvent(StreamEvent):
    """Emitted when the agent invokes a tool."""

    tool_name: str = ""
    tool_id: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    sequence_number: int = 0


@dataclass
class ToolResultEvent(StreamEvent):
    """Emitted when a tool returns its result."""

    tool_id: str = ""
    tool_name: str = ""
    success: bool = True
    result_preview: str = ""


@dataclass
class TextChunkEvent(StreamEvent):
    """Emitted when the assistant produces text output."""

    text: str = ""


@dataclass
class TurnCompleteEvent(StreamEvent):
    """Emitted when an evaluation iteration finishes."""

    iteration: int = 0
    duration_s: float = 0.0
    command_count: int = 0
    token_usage_str: str = ""


@dataclass
class CriteriaCheckEvent(StreamEvent):
    """Emitted after success criteria are evaluated."""

    passed: int = 0
    total: int = 0
    weighted_score: float = 0.0
    details: list[str] = field(default_factory=list)
