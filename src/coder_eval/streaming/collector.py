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
    AgentStartEvent,
    StreamEvent,
    ToolEndEvent,
    TurnStartEvent,
)
from coder_eval.timing import busy_ms, decompose_turn


def subtract_tool_time(
    messages: list[TranscriptMessage],
    spans: list[tuple[datetime, datetime]],
) -> list[TranscriptMessage]:
    """Take tool execution back out of the generation windows it overlapped.

    THE one place this happens. Five reducers used to do it themselves — four
    through ``close_window`` as they flushed, claude-code once at finalization —
    while the head and tail were already computed centrally, right here. That
    asymmetry was the complexity, and every timing defect this branch fixed
    lived in the per-reducer bookkeeping around the subtraction rather than in
    the subtraction itself: when to reset a span list, when to clear a start
    stamp, when to advance a mark. A reducer now publishes the RAW window and
    keeps only the genuinely harness-shaped decision, which is where its window
    opens.

    NON-MUTATING, and the reason is aliasing rather than repeated calls. Every
    agent builds its terminal event as ``AgentEndEvent(messages=list(...))`` —
    that copies the LIST, not the message objects — so writing in place would
    reach back into the agent's own live state from the collector, which is
    exactly the layering "the collector is the sole capture seam" exists to
    prevent. ``model_copy`` keeps it one-directional. It is also unconditionally
    safe for any caller that builds a record twice: ``EarlyStopWatcher`` holds
    one collector across a turn's tool-call rounds and calls
    ``build_turn_record`` on every one.

    GROUPED BY IDENTICAL BOUNDS, not by ``message_id``. Codex splits one window
    across two sub-messages (thinking and action) that share ``started_at`` and
    ``completed_at`` and divide the window by output-token share; subtracting
    the group's overlap from each part separately would subtract it twice and
    stop the parts summing to the window. Bounds identity covers that, and it
    also covers OpenCode and Pi, which can legitimately carry
    ``message_id is None`` — so keying on the id would silently collapse every
    id-less message of a turn into one group.

    MAIN THREAD ONLY. A sub-agent generation (``parent_tool_use_id`` set) is
    skipped: its own tools are not in this span set, and the Agent call that
    spawned it already covers its whole run.

    A ``generation_duration_ms`` of ``None`` means no window was ever measured
    (codex's rollout rebuild, claude's synthesized sub-agent terminal), so there
    is nothing to subtract from and it passes through untouched — never
    coerced to ``0.0`` (CE058). Every non-``AssistantMessage`` entry — a
    simulation ``UserMessage``, the appended ``ReconciliationMessage`` — passes
    through by identity.

    A window entirely covered by tool execution reaches ``0.0``, and that is a
    measurement rather than an absence.
    """
    # (index, raw window ms) per group. The raw value is captured HERE, where
    # the message is already narrowed to AssistantMessage, so the apportioning
    # loop below needs no second narrowing.
    groups: dict[tuple[datetime, datetime], list[tuple[int, float]]] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, AssistantMessage):
            continue
        raw = message.generation_duration_ms
        if raw is None or message.parent_tool_use_id is not None:
            continue
        groups.setdefault((message.started_at, message.completed_at), []).append((index, raw))

    out = list(messages)
    for (started, completed), members in groups.items():
        raw_total = sum(raw for _, raw in members)
        # Nothing to apportion, and dividing by it is a ZeroDivisionError. A
        # group already at zero stays at zero.
        if raw_total <= 0:
            continue
        net = max(raw_total - busy_ms(spans, started, completed), 0.0)
        assigned = 0.0
        for n, (index, raw) in enumerate(members):
            # The last member takes the remainder so the parts reconstruct the
            # group's net exactly, rather than drifting by the rounding.
            share = net - assigned if n == len(members) - 1 else round(net * (raw / raw_total), 6)
            out[index] = out[index].model_copy(update={"generation_duration_ms": share})
            assigned += share
    return out


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
        # Stamped by AgentStartEvent; the head is measured from it.
        self._agent_start_at: datetime | None = None
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
            self._agent_start_at = event.timestamp
            # A new turn has begun, so the previous turn's terminal event is no
            # longer this turn's. Every agent builds a fresh collector per
            # communicate(), but EarlyStopWatcher keeps ONE across retries: left
            # stale, it would pair this attempt's start with the last attempt's
            # end and publish the clamped inversion as a measured 0.0.
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

    @property
    def visible_turn_count(self) -> int:
        """Visible timeline entries observed so far — one per resolved tool call.

        The live, in-stream counterpart of ``reports_stats.visible_turn_count``,
        which counts the very same list once the turn is a finished
        ``TurnRecord`` (minus its trailing final-reply entry, which cannot exist
        while the turn is still running).

        Agents whose SDK has no meaningful native turn counter — Codex and
        Antigravity each deliver a single SDK turn per ``communicate()`` — enforce
        ``run_limits.max_turns`` against this. Reading it from the collector rather
        than from each agent's own scratch list is what makes the cap mean the same
        thing on both: the collector is the single agent-agnostic capture path, and
        keying on ``tool_id`` means a re-emitted end event cannot double-count.
        """
        return len(self._commands)

    def _ordered_commands(self) -> list[CommandTelemetry]:
        return sorted(self._commands.values(), key=lambda c: c.sequence_number)

    def _overhead_ms(
        self, messages: list[TranscriptMessage], tool_spans: list[tuple[datetime, datetime]] | None = None
    ) -> tuple[float | None, float | None]:
        """The turn's head and tail — the wall clock the generations do not cover.

        Measured against ``AssistantMessage`` entries only: a simulation turn
        interleaves ``UserMessage`` entries, and a reconciled turn ends with a
        ``ReconciliationMessage`` that carries no timestamps at all, so indexing
        the raw list would measure the wrong thing or raise.

        Two further restrictions, both of which are the difference between a
        measurement and an invention:

        A message whose ``generation_duration_ms`` is ``None`` is SKIPPED. That
        field is the codebase's own marker for "no window was measurable here",
        and every producer of one stamps ``started_at == completed_at ==
        datetime.now()`` at *append* time as an admitted placeholder — Codex's
        rollout rebuild (``_messages_from_items``), both Codex sub-agent
        recovery builders, and Claude's ``_synthesize_subagent_terminal_message``.
        Reading those stamps as window bounds turns a placeholder into a
        measurement: a Codex turn rebuilt from its rollout stamps every message
        at turn END, which would book the entire turn as harness startup. It is
        the same exemption CE059 makes for exactly the same reason.

        ``min`` / ``max`` rather than the first and last list entries, because
        the list is not ordered by time — Codex appends recovered sub-agent
        messages after the parent's last flush. Positional access made the
        result depend on append order, which nothing enforces.

        MAIN THREAD ONLY, the third restriction and the same rule its two
        sibling call sites already apply (``codex_agent._token_usage_from_messages``
        and ``scripts/timing/decompose_run.py``). A sub-agent's generations
        carry the spawning Agent call's ``parent_tool_use_id``, and the identity
        these two values complete sums generation over the main thread ONLY —
        the parent tool call's own interval already spans the sub-agent's whole
        run. Bracketing the span with a sub-agent message therefore shrinks the
        head or the tail by time no other bucket claims, and Codex's recovered
        child messages carry the CHILD's clock, so the bracket can move either
        way. Excluding them keeps all four buckets measuring one thread.
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
            tool_spans if tool_spans is not None else self._main_thread_tool_spans(messages),
        )

    def _main_thread_tool_spans(self, messages: list[TranscriptMessage]) -> list[tuple[datetime, datetime]]:
        """Bounded execution intervals of the MAIN THREAD's tool calls.

        The span set both the head/tail decomposition and the generation
        subtraction are measured against, so they cannot disagree about which
        calls exist.

        Sub-agent tools are excluded, and this used to be the gap: ``_overhead_ms``
        filtered its GENERATIONS to the main thread and then passed EVERY
        command, so its docstring's claim to keep all four buckets measuring one
        thread was true only by luck. It held because a child nests inside the
        parent Agent call, whose own interval the union already covers — but
        Codex's recovered child tools carry the CHILD's clock, so nothing made
        it true by construction. The evalboard's twin (``toolExecutionMs``) does
        filter, so the two implementations agreed by accident.

        A sub-agent's tool ids are reachable only through the messages that own
        them: a child generation carries ``parent_tool_use_id``, and its
        ``tool_use_ids`` are the calls it made.
        """
        sub_agent_tool_ids = {
            tool_id
            for m in messages
            if isinstance(m, AssistantMessage) and m.parent_tool_use_id is not None
            for tool_id in m.tool_use_ids
        }
        return [
            (c.execution_started_at, c.execution_completed_at)
            for c in self._commands.values()
            if c.execution_started_at is not None
            and c.execution_completed_at is not None
            and c.tool_id not in sub_agent_tool_ids
        ]

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
        # Tool execution comes out of the generation windows HERE, once, for
        # every harness — the reducers publish raw windows.
        #
        # The span set is computed ONCE and handed to both consumers. That is
        # the invariant worth protecting, and it is the one that is easy to
        # break: the subtraction and the head/tail must agree about which calls
        # exist, or the buckets stop being disjoint. (The ORDER of the two is
        # not load-bearing — `_overhead_ms` reads only the bounds, the
        # main-thread flag and whether the duration is `None`, none of which
        # `subtract_tool_time` changes. Do not add a comment claiming it is.)
        tool_spans = self._main_thread_tool_spans(messages)
        messages = subtract_tool_time(messages, tool_spans)
        if token_usage is not None:
            messages = self._reconciled_messages(messages, token_usage)

        startup_ms, teardown_ms = self._overhead_ms(messages, tool_spans)

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
            harness_startup_ms=startup_ms,
            harness_teardown_ms=teardown_ms,
        )
