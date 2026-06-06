"""EventCollector: reduce a standardized event stream into a TurnRecord.

This is the single, agent-agnostic place where the persisted ``TurnRecord``
(and therefore ``task.json``) is assembled from the event stream — so adding a
new agent means emitting the standard events, with capture coming for free
(no per-agent telemetry-assembly code).

Reduction split:

- ``commands`` are derived from the ``ToolEndEvent`` stream (every tool call,
  including crash-orphaned ones force-closed as ``unresolved``), ordered by the
  tool's ``sequence_number``. This is the genuine "events are the source of
  truth" path for tool telemetry.
- The per-message telemetry / token payload (the intricate, SDK-specific token
  machinery the plan defers) rides on the terminal ``AgentEndEvent`` and is read
  back verbatim — no re-derivation, so token correctness is untouched.

An agent attaches its own ``EventCollector`` alongside the caller's callback and
returns ``build_turn_record()`` from ``communicate()``; the orchestrator keeps
reading the return value (and ``pending_turn`` on crash), now event-derived.
"""

from __future__ import annotations

from coder_eval.models import CommandTelemetry, TokenUsage, TurnRecord
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentStartEvent,
    StreamEvent,
    ToolEndEvent,
    TurnStartEvent,
)


class EventCollector:
    """A ``StreamCallback`` that accumulates events and builds a ``TurnRecord``.

    Tolerant by construction: ``build_turn_record()`` can be called at any point
    (including mid-stream after a crash) and returns the best record derivable
    from the events seen so far. Only ``main-agent`` events (``parent_thread_id``
    is None) contribute to this record; nested sub-agent events are folded into
    ``sub_agent_usage`` instead.
    """

    def __init__(self) -> None:
        self._iteration: int = 0
        self._user_input: str = ""
        self._model: str | None = None
        self._turn_starts: int = 0
        # tool_id -> finalized telemetry (last ToolEnd wins, mirroring last-result-wins).
        self._commands: dict[str, CommandTelemetry] = {}
        self._agent_end: AgentEndEvent | None = None

    def on_event(self, event: StreamEvent) -> None:
        # Only the main agent's own events shape its TurnRecord. This guard is
        # forward-looking: nested sub-agent events (parent_thread_id set) are NOT
        # emitted by any agent yet (sub-agent nesting is deferred), so this branch
        # is currently never taken. It's here so that when nesting lands, child
        # events are skipped here and attributed via the finalization payload
        # rather than corrupting the main-agent record.
        if event.parent_thread_id is not None:
            return

        if isinstance(event, AgentStartEvent):
            self._iteration = event.iteration
            self._user_input = event.prompt
            if event.model:
                self._model = event.model
        elif isinstance(event, TurnStartEvent):
            self._turn_starts += 1
            if event.model:
                self._model = event.model
        elif isinstance(event, ToolEndEvent):
            self._commands[event.tool.tool_id] = event.tool
        elif isinstance(event, AgentEndEvent):
            self._agent_end = event

    def _ordered_commands(self) -> list[CommandTelemetry]:
        return sorted(self._commands.values(), key=lambda c: c.sequence_number)

    def build_turn_record(self) -> TurnRecord:
        """Assemble the ``TurnRecord`` from the events observed so far."""
        end = self._agent_end
        commands = self._ordered_commands()

        if end is None:
            # No terminal event yet (e.g. mid-stream snapshot). Return a minimal
            # record from the granular events we have.
            return TurnRecord(
                iteration=self._iteration,
                user_input=self._user_input,
                agent_output="",
                commands=commands,
                token_usage=None,
                model_used=self._model,
                assistant_turn_count=self._turn_starts,
            )

        # Treat an all-zero, costless usage as "no usage reported" (None) so the
        # record matches agents that surfaced nothing; otherwise carry it through.
        tokens = end.usage.tokens
        token_usage: TokenUsage | None = (
            tokens if (not tokens.is_empty() or tokens.total_cost_usd is not None) else None
        )

        return TurnRecord(
            iteration=end.iteration or self._iteration,
            user_input=end.user_input or self._user_input,
            agent_output=end.agent_output,
            commands=commands,
            duration_seconds=end.duration_seconds,
            token_usage=token_usage,
            model_used=end.model_used or self._model,
            assistant_turn_count=end.assistant_turn_count,
            messages=list(end.messages),
            num_turns=end.num_turns,
            max_turns_exhausted=end.max_turns_exhausted,
            result_summary=end.result_summary,
            crashed=end.crashed,
            crash_reason=end.crash_reason,
            sub_agent_usage=list(end.sub_agent_usage),
        )
