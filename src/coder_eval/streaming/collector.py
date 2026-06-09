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

from coder_eval.models import (
    AssistantMessage,
    CommandTelemetry,
    ReconciliationMessage,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
)
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
    from the events seen so far. Sub-agent activity is captured as
    ``parent_tool_use_id``-tagged messages in the transcript; per-sub-agent
    attribution is derived by grouping those messages, not from a separate field.
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

    @staticmethod
    def _reconciled_messages(messages: list[TranscriptMessage], usage: TokenUsage) -> list[TranscriptMessage]:
        """Append a ``ReconciliationMessage`` so the transcript's token buckets
        sum to ``usage`` (the authoritative turn total).

        The per-``AssistantMessage`` stream consistently under-reports the bill —
        a fixed prompt slice (~512 input tokens on Claude) is billed on no
        SDK-emitted message, and sub-agent input/cache only partially bubbles up.
        We book that residual once, explicitly, as a synthetic entry rather than
        smearing fabricated tokens across real generations. After this, any
        consumer that sums the four token buckets across the transcript reproduces
        ``usage`` exactly — no separate aggregate needed. Only assistant
        generations carry agent-billed tokens, so the residual is measured against
        them (simulator ``UserMessage`` tokens are a separate bill). Emitted only
        when some bucket actually diverges.
        """
        in_sum = out_sum = cw_sum = cr_sum = 0
        for m in messages:
            if isinstance(m, AssistantMessage):
                in_sum += m.input_tokens
                out_sum += m.output_tokens
                cw_sum += m.cache_creation_tokens
                cr_sum += m.cache_read_tokens
        d_in = usage.uncached_input_tokens - in_sum
        d_out = usage.output_tokens - out_sum
        d_cw = usage.cache_creation_input_tokens - cw_sum
        d_cr = usage.cache_read_input_tokens - cr_sum
        if d_in == 0 and d_out == 0 and d_cw == 0 and d_cr == 0:
            return messages
        # The residual is almost always positive (tokens billed but not streamed).
        # A negative residual means the captured generations OVER-report the turn
        # total for some bucket; word the note for that case so a "-512" entry
        # doesn't read as "billed but not surfaced".
        positive = d_in >= 0 and d_out >= 0 and d_cw >= 0 and d_cr >= 0
        note = (
            "Tokens the agent billed but never surfaced as a generation "
            "(fixed prompt overhead + sub-agent input/cache the stream doesn't bubble up). "
            "Booked here so the transcript reconciles to the turn total."
            if positive
            else (
                "Per-bucket residual reconciling the captured generations to the turn total "
                "(negative where the stream over-reports a bucket). "
                "Booked here so the transcript sums to the authoritative usage."
            )
        )
        return [
            *messages,
            ReconciliationMessage(
                input_tokens=d_in,
                output_tokens=d_out,
                cache_creation_tokens=d_cw,
                cache_read_tokens=d_cr,
                note=note,
            ),
        ]

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
        tokens = end.usage
        token_usage: TokenUsage | None = (
            tokens if (not tokens.is_empty() or tokens.total_cost_usd is not None) else None
        )

        # The authoritative turn total (token_usage) is the source of truth, but
        # the per-message stream under-reports it. Book the residual as a single
        # synthetic ReconciliationMessage so the transcript's token buckets sum
        # to the total — making the stream self-reconciling for any downstream
        # consumer (e.g. the evalboard) without a competing aggregate.
        messages: list[TranscriptMessage] = list(end.messages)
        if token_usage is not None:
            messages = self._reconciled_messages(messages, token_usage)

        return TurnRecord(
            iteration=end.iteration or self._iteration,
            user_input=end.user_input or self._user_input,
            agent_output=end.agent_output,
            commands=commands,
            duration_seconds=end.duration_seconds,
            token_usage=token_usage,
            model_used=end.model_used or self._model,
            assistant_turn_count=end.assistant_turn_count,
            messages=messages,
            num_turns=end.num_turns,
            max_turns_exhausted=end.max_turns_exhausted,
            result_summary=end.result_summary,
            crashed=end.crashed,
            crash_reason=end.crash_reason,
        )
