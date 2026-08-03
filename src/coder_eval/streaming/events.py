"""Standardized streaming events for agent execution.

Every event originates inside ``Agent.communicate()`` (the agent is the *single*
emitter — the orchestrator is a pure consumer). Events form a tree mirroring
execution and close every Start with a matching End on every exit path:

    AgentStartEvent(thread_id=A)
      TurnStartEvent(thread_id=A, turn_id=t1)
        TextChunkEvent(turn_id=t1)
        ToolStartEvent(turn_id=t1, tool)
        ToolEndEvent(turn_id=t1, tool, status)
      TurnEndEvent(thread_id=A, turn_id=t1, status, tokens)
    AgentEndEvent(thread_id=A, status, usage)

Self-describing via ``thread_id`` / ``parent_thread_id`` / ``turn_id`` so a
consumer can place each event in the tree without keeping its own state. Sub-agents
are *not* a separate event type — a sub-agent is a nested ``AgentStart``/``AgentEnd``
pair linked by ``parent_thread_id``.

Events are Pydantic ``BaseModel``s (keyword-only construction) and reuse the leaf
telemetry models (``TokenUsage``, ``CommandTelemetry``) rather than re-declaring
fields.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coder_eval.models import (
    CommandTelemetry,
    ResultSummary,
    TokenUsage,
    TranscriptMessage,
)


# --- Status codes -----------------------------------------------------------
#
# One enum per End-event level. Every End event carries the appropriate one so
# success, tool error, permission denial, crash, timeout and orphaned tool calls
# are a single mechanism (no ToolErrorEvent subclass + string scans).


class ToolEndStatus(StrEnum):
    """Terminal status of a single tool call."""

    OK = "ok"  # tool ran, no error
    ERROR = "error"  # tool ran and returned an error
    PERMISSION_DENIED = "permission_denied"  # blocked by permission/sandbox
    UNRESOLVED = "unresolved"  # ToolStart emitted, no result observed (crash/timeout/orphan)


class TurnEndStatus(StrEnum):
    """Terminal status of one inner turn (one API response / one Codex turn)."""

    COMPLETED = "completed"
    CRASHED = "crashed"
    TIMEOUT = "timeout"
    MAX_TURNS_EXHAUSTED = "max_turns_exhausted"
    STOPPED_EARLY = "stopped_early"  # cooperative early-stop-on-criterion (clean, non-crash)


class AgentEndStatus(StrEnum):
    """Terminal status of one agent invocation (a whole ``communicate()`` call)."""

    COMPLETED = "completed"
    CRASHED = "crashed"
    TIMEOUT = "timeout"
    MAX_TURNS_EXHAUSTED = "max_turns_exhausted"
    STOPPED_EARLY = "stopped_early"  # cooperative early-stop-on-criterion (clean, non-crash)


# Reuse the canonical TranscriptMessage union (defined once in telemetry.py) so
# AgentEndEvent carries per-message telemetry losslessly and stays in lock-step
# with TurnRecord.messages (the deferred token path rides here, not on granular events).
_MessageList = list[TranscriptMessage]


class StreamEvent(BaseModel):
    """Base class for all streaming events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_id: str
    timestamp: datetime = Field(default_factory=datetime.now)
    thread_id: str | None = Field(
        default=None,
        description="Agent-context id. Main agent: SDK session id (None until assigned). Sub-agent: spawning tool id.",
    )
    parent_thread_id: str | None = Field(
        default=None,
        description="None for the main agent; the parent's thread_id for a sub-agent.",
    )


class AgentStartEvent(StreamEvent):
    """Emitted on the first line of ``communicate()`` (replaces the orchestrator's TurnStartEvent)."""

    kind: Literal["agent_start"] = "agent_start"
    prompt: str = ""
    iteration: int = 0
    model: str | None = None


class TurnStartEvent(StreamEvent):
    """Emitted when one inner turn begins (Claude: per API call; Codex: once per communicate())."""

    kind: Literal["turn_start"] = "turn_start"
    turn_id: str = ""
    model: str | None = None


class TextChunkEvent(StreamEvent):
    """Emitted when the agent produces visible text output within a turn."""

    kind: Literal["text_chunk"] = "text_chunk"
    turn_id: str = ""
    text: str = ""


class ToolStartEvent(StreamEvent):
    """Emitted when the agent invokes a tool (the tool's result fields are pending)."""

    kind: Literal["tool_start"] = "tool_start"
    turn_id: str = ""
    tool: CommandTelemetry


class ToolEndEvent(StreamEvent):
    """Emitted when a tool returns its result (or is force-closed as unresolved on crash)."""

    kind: Literal["tool_end"] = "tool_end"
    turn_id: str = ""
    tool: CommandTelemetry
    status: ToolEndStatus = ToolEndStatus.OK


class TurnEndEvent(StreamEvent):
    """Emitted at each inner-turn boundary; carries best-effort per-turn tokens."""

    kind: Literal["turn_end"] = "turn_end"
    turn_id: str = ""
    status: TurnEndStatus = TurnEndStatus.COMPLETED
    tokens: TokenUsage | None = None


class AgentEndEvent(StreamEvent):
    """Emitted from ``finally`` at the end of ``communicate()`` — the turn finalization payload.

    Carries the cumulative, authoritative ``usage`` plus the fields the
    ``EventCollector`` cannot losslessly derive from granular events (the per-message
    telemetry / token path the plan defers). ``EventCollector`` reduces commands from
    the ``ToolEnd`` stream and reads everything else here.
    """

    kind: Literal["agent_end"] = "agent_end"
    status: AgentEndStatus = AgentEndStatus.COMPLETED
    usage: TokenUsage = Field(default_factory=TokenUsage)

    # Finalization payload (the deferred token/messages path rides here).
    iteration: int = 0
    user_input: str = ""
    agent_output: str = ""
    model_used: str | None = None
    assistant_turn_count: int = 0
    messages: _MessageList = Field(default_factory=list)
    num_turns: int | None = None
    max_turns_exhausted: bool = False
    result_summary: ResultSummary | None = None
    crashed: bool = False
    crash_reason: str | None = None
    duration_seconds: float = 0.0
    # Sub-agents whose inner tool calls never reached this turn's transcript. Rides
    # the finalization payload because the agent is the only party that knows a
    # recovery attempt failed -- there is no granular event for a stream that never
    # existed, so the collector has nothing to reduce.
    unrecovered_subagent_threads: int = 0


# --- Post-evaluation events (orchestrator-owned, not part of the agent lifecycle) ---


class CriterionSummary(BaseModel):
    """Summary of a single criterion check for display."""

    criterion_type: str = ""
    description: str = ""
    score: float = 0.0
    passed: bool = False
    failure_reason: str | None = None


class CriteriaCheckEvent(StreamEvent):
    """Emitted after success criteria are evaluated."""

    kind: Literal["criteria_check"] = "criteria_check"
    passed: int = 0
    total: int = 0
    weighted_score: float = 0.0
    details: list[str] = Field(default_factory=list)
    criteria: list[CriterionSummary] = Field(default_factory=list)
