"""EventCollector: reduce a standardized event stream into a TurnRecord.

The single, agent-agnostic place where the persisted ``TurnRecord`` (and
therefore ``task.json``) is assembled — so a new agent emits the standard events
and gets capture for free.

An agent attaches its own collector alongside the caller's ``stream_callback``
and returns ``build_turn_record()`` from ``communicate()``.

``commands`` are derived from the ``ToolEndEvent`` stream (crash-orphaned calls
included, force-closed as ``unresolved``), ordered by ``sequence_number``. The
per-message telemetry / token payload rides on the terminal ``AgentEndEvent`` and
is read back verbatim, never re-derived.
"""

from __future__ import annotations

from datetime import datetime

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
    AgentEndStatus,
    AgentStartEvent,
    StreamEvent,
    ToolEndEvent,
    TurnStartEvent,
)
from coder_eval.timing import decompose_turn, main_thread_tool_spans, subtract_tool_time, union_ms


class EventCollector:
    """A ``StreamCallback`` that accumulates events and builds a ``TurnRecord``.

    Tolerant by construction: ``build_turn_record()`` can be called at any point
    (including mid-stream after a crash) and returns the best record derivable
    from the events seen so far. Sub-agent activity is captured as
    ``parent_tool_use_id``-tagged messages in the transcript. A nested event
    (``parent_thread_id`` set) contributes only its ``ToolEndEvent`` to ``commands``;
    it sets no model and counts no turn.
    """

    def __init__(self) -> None:
        self._iteration: int = 0
        self._user_input: str = ""
        self._model: str | None = None
        self._turn_starts: int = 0
        # Stamped by AgentStartEvent; the head is measured from it.
        self._agent_start_at: datetime | None = None
        # tool_id -> finalized telemetry (last ToolEnd wins, mirroring last-result-wins).
        self._commands: dict[str, CommandTelemetry] = {}
        self._agent_end: AgentEndEvent | None = None

    @property
    def ended(self) -> bool:
        """True once the current attempt's ``AgentEndEvent`` has been seen."""
        return self._agent_end is not None

    def on_event(self, event: StreamEvent) -> None:
        if event.parent_thread_id is not None:
            if isinstance(event, ToolEndEvent):
                self._commands[event.tool.tool_id] = event.tool
            return

        if isinstance(event, AgentStartEvent):
            self._iteration = event.iteration
            self._user_input = event.prompt
            self._agent_start_at = event.timestamp
            # TurnMonitor keeps ONE collector across retries: left stale,
            # this would pair the new start with the last attempt's end and
            # publish the clamped inversion as a measured 0.0.
            self._agent_end = None
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

    def _ordered_commands(self, messages: list[TranscriptMessage]) -> list[CommandTelemetry]:
        """Commands by ``sequence_number``, each a copy carrying its derived ``assistant_turn_index``.

        The index is the position, among the ``AssistantMessage`` entries, of the first
        message whose ``tool_use_ids`` names the command; ``None`` when none does.
        """
        owner: dict[str, int] = {}
        for index, message in enumerate(m for m in messages if isinstance(m, AssistantMessage)):
            for tool_id in message.tool_use_ids:
                owner.setdefault(tool_id, index)
        return [
            command.model_copy(update={"assistant_turn_index": owner.get(command.tool_id)})
            for command in sorted(self._commands.values(), key=lambda c: c.sequence_number)
        ]

    def _overhead_ms(
        self, messages: list[TranscriptMessage], tool_spans: list[tuple[datetime, datetime]]
    ) -> tuple[float | None, float | None]:
        """The turn's head and tail — the wall clock the generations do not cover.

        Measured against ``AssistantMessage`` entries only, skipping any whose
        ``generation_duration_ms`` is ``None`` — that field is the codebase's
        marker for "no window was measurable here", and its producers stamp a
        placeholder ``started_at == completed_at`` that would otherwise read as a
        measurement (the same exemption CE059 makes).

        ``min``/``max``, not the first and last entries: the list is not ordered
        by time. MAIN THREAD ONLY, so all four buckets measure one thread.

        ``tool_spans`` is REQUIRED, never defaulted: its one caller computes the
        set once and hands the same object to both consumers, and a fallback here
        would build a SECOND set.

        Rationale: .claude/notes/timing.md § _overhead_ms
        """
        generations = [
            m
            for m in messages
            if isinstance(m, AssistantMessage) and m.generation_duration_ms is not None and m.parent_tool_use_id is None
        ]
        if not generations:
            return None, None
        return decompose_turn(
            min(m.started_at for m in generations),
            max(m.completed_at for m in generations),
            self._agent_start_at,
            self._agent_end.timestamp if self._agent_end is not None else None,
            tool_spans,
        )

    @staticmethod
    def _reconciled_messages(messages: list[TranscriptMessage], usage: TokenUsage) -> list[TranscriptMessage]:
        """Append a ``ReconciliationMessage`` so the transcript's token buckets
        sum to ``usage`` (the authoritative turn total).

        The per-message stream under-reports the bill, so the residual is booked
        once, explicitly, rather than smeared across real generations. After
        this, summing the four buckets across the transcript reproduces ``usage``
        exactly. Measured against assistant generations only, and emitted only
        when some bucket actually diverges.

        Rationale: .claude/notes/agents.md § Token accounting and the reconciliation message
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
        # A negative residual means the captured generations OVER-report some
        # bucket; word the note for that case so "-512" doesn't read as
        # "billed but not surfaced".
        positive = d_in >= 0 and d_out >= 0 and d_cw >= 0 and d_cr >= 0
        note = (
            "Tokens the agent billed but never surfaced as a generation "
            + "(fixed prompt overhead + sub-agent input/cache the stream doesn't bubble up). "
            + "Booked here so the transcript reconciles to the turn total."
            if positive
            else (
                "Per-bucket residual reconciling the captured generations to the turn total "
                + "(negative where the stream over-reports a bucket). "
                + "Booked here so the transcript sums to the authoritative usage."
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

        if end is None:
            # No terminal event yet (mid-stream snapshot): minimal record.
            return TurnRecord(
                iteration=self._iteration,
                user_input=self._user_input,
                agent_output="",
                commands=self._ordered_commands([]),
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

        messages: list[TranscriptMessage] = list(end.messages)
        # ONE span set, handed to BOTH consumers: the subtraction and the
        # head/tail must agree about which calls exist, or the buckets stop being
        # disjoint. (Their ORDER is not load-bearing. Do not claim it is.)
        # Rationale: .claude/notes/timing.md § Why the subtraction and the head/tail may run in either order
        tool_spans = main_thread_tool_spans(messages, self._commands.values())
        messages = subtract_tool_time(messages, tool_spans)
        if token_usage is not None:
            messages = self._reconciled_messages(messages, token_usage)

        startup_ms, teardown_ms = self._overhead_ms(messages, tool_spans)
        # The UNION (never the sum) of the SAME span set above. `None` when no
        # bounded span was recorded: a turn that ran tools and timed none is not
        # one whose tools took no time (CE058).
        tool_union = union_ms(tool_spans) if tool_spans else None

        return TurnRecord(
            iteration=end.iteration or self._iteration,
            user_input=end.user_input or self._user_input,
            agent_output=end.agent_output,
            commands=self._ordered_commands(messages),
            duration_seconds=end.duration_seconds,
            token_usage=token_usage,
            model_used=end.model_used or self._model,
            assistant_turn_count=end.assistant_turn_count,
            messages=messages,
            num_turns=end.num_turns,
            tool_calls_exhausted=end.status is AgentEndStatus.TOOL_CALLS_EXHAUSTED,
            result_summary=end.result_summary,
            crashed=end.crashed,
            crash_reason=end.crash_reason,
            harness_startup_ms=startup_ms,
            harness_teardown_ms=teardown_ms,
            tool_union_ms=tool_union,
        )
