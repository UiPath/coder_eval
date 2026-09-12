"""Unit tests for ``EventCollector`` reduction branches.

Feeds hand-built event lists to a bare ``EventCollector()`` (no agent) and
asserts ``build_turn_record()`` honors the coalescing / filtering / ordering
rules in ``coder_eval/streaming/collector.py``.
"""

from datetime import datetime, timedelta
from typing import ClassVar

import pytest

from coder_eval.models import (
    AssistantMessage,
    CommandTelemetry,
    ReconciliationMessage,
    ResultSummary,
    TokenUsage,
    TurnRecord,
)
from coder_eval.streaming.collector import EventCollector, subtract_tool_time
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    ToolEndEvent,
    TurnStartEvent,
)
from coder_eval.timing import union_ms


TASK_ID = "collector-test"


def _tool(tool_id: str, seq: int, name: str = "Bash") -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=name,
        tool_id=tool_id,
        timestamp=datetime.now(),
        sequence_number=seq,
    )


def _feed(collector: EventCollector, events) -> None:
    for ev in events:
        collector.on_event(ev)


class TestUsageCoalescing:
    """The all-zero / costless usage coalescing rule (collector.py ~96-103)."""

    def test_all_zero_costless_usage_becomes_none(self):
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1),
                AgentEndEvent(
                    task_id=TASK_ID,
                    usage=TokenUsage(),  # all-zero, no cost
                ),
            ],
        )

        record = collector.build_turn_record()
        assert record.token_usage is None

    def test_costless_but_nonempty_usage_is_kept(self):
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1),
                AgentEndEvent(
                    task_id=TASK_ID,
                    usage=TokenUsage(output_tokens=5),
                ),
            ],
        )

        record = collector.build_turn_record()
        assert record.token_usage is not None
        assert record.token_usage.output_tokens == 5

    def test_zero_tokens_with_cost_is_kept(self):
        # is_empty() ignores cost, so a costless-zero is None but a $-bearing
        # zero-token usage must be carried through (the `or cost is not None` arm).
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1),
                AgentEndEvent(
                    task_id=TASK_ID,
                    usage=TokenUsage(total_cost_usd=0.0),
                ),
            ],
        )

        record = collector.build_turn_record()
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == 0.0


class TestSubAgentEventFiltering:
    """Events with parent_thread_id set are ignored (collector.py ~61)."""

    def test_sub_agent_events_do_not_affect_record(self):
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="main prompt", iteration=2),
                # A nested sub-agent's events (parent_thread_id set) must be skipped.
                AgentStartEvent(
                    task_id=TASK_ID,
                    prompt="child prompt",
                    iteration=99,
                    thread_id="tool_x",
                    parent_thread_id="main",
                ),
                ToolEndEvent(
                    task_id=TASK_ID,
                    tool=_tool("child_tool", 0),
                    thread_id="tool_x",
                    parent_thread_id="main",
                ),
                AgentEndEvent(
                    task_id=TASK_ID,
                    iteration=2,
                    user_input="main prompt",
                    agent_output="main out",
                    usage=TokenUsage(output_tokens=10),
                    parent_thread_id=None,
                ),
            ],
        )

        record = collector.build_turn_record()
        # The child AgentStart did not overwrite iteration/user_input.
        assert record.iteration == 2
        assert record.user_input == "main prompt"
        assert record.agent_output == "main out"
        # The child ToolEnd contributed no command.
        assert record.commands == []


class TestToolReduction:
    """Tool reduction: last-result-wins dedup + sequence ordering (~73-79)."""

    def test_duplicate_tool_id_last_end_wins(self):
        collector = EventCollector()
        first = _tool("dup", 0, name="Bash")
        # Same tool_id, different identity/sequence — last ToolEnd should win.
        second = _tool("dup", 5, name="Write")
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1),
                ToolEndEvent(task_id=TASK_ID, tool=first),
                ToolEndEvent(task_id=TASK_ID, tool=second),
                AgentEndEvent(task_id=TASK_ID, usage=TokenUsage(output_tokens=1)),
            ],
        )

        record = collector.build_turn_record()
        assert len(record.commands) == 1
        assert record.commands[0].tool_name == "Write"
        assert record.commands[0].sequence_number == 5

    def test_out_of_order_tool_ids_are_ordered_by_sequence(self):
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1),
                # Emit out of sequence order; collector must sort by sequence_number.
                ToolEndEvent(task_id=TASK_ID, tool=_tool("c", 2)),
                ToolEndEvent(task_id=TASK_ID, tool=_tool("a", 0)),
                ToolEndEvent(task_id=TASK_ID, tool=_tool("b", 1)),
                AgentEndEvent(task_id=TASK_ID, usage=TokenUsage(output_tokens=1)),
            ],
        )

        record = collector.build_turn_record()
        assert [c.tool_id for c in record.commands] == ["a", "b", "c"]
        assert [c.sequence_number for c in record.commands] == [0, 1, 2]


class TestFullFieldParity:
    """Every ``TurnRecord`` field is sourced — none silently drops to its default.

    ``build_turn_record`` derives ``commands`` from the ToolEnd stream and
    ``token_usage`` from ``end.usage.tokens``; every *other* field is read back
    verbatim off the terminal ``AgentEndEvent``. The risk the review flagged: a
    field added to ``TurnRecord`` (and to ``AgentEndEvent``) but not wired into
    ``build_turn_record`` would silently land on its model default with no
    failing assertion. These tests pin the *whole* field set, not just commands
    + tokens.
    """

    # TurnRecord fields NOT carried verbatim from AgentEndEvent:
    #   commands            -> reduced from the ToolEnd stream
    #   token_usage         -> derived from end.usage.tokens
    #   timestamp           -> record's own creation stamp, not an event field
    #   provider_call_costs -> joined in post-run by the orchestrator from the
    #                          LiteLLM proxy cost log (litellm_cost.apply_actual_cost),
    #                          not emitted by the agent/EventCollector.
    _DERIVED: ClassVar[set[str]] = {
        "commands",
        "token_usage",
        "timestamp",
        "provider_call_costs",
        # Measured by the collector between the agent's own start/end event
        # stamps and the first/last generation window — not carried on
        # AgentEndEvent, because no agent computes them.
        "harness_startup_ms",
        "harness_teardown_ms",
    }

    def _full_agent_end(self) -> AgentEndEvent:
        """An AgentEndEvent with every verbatim field set to a non-default sentinel."""
        started = datetime(2026, 9, 11, 9, 0, 0)
        msg = AssistantMessage(
            started_at=started,
            completed_at=started + timedelta(milliseconds=12.0),
            generation_duration_ms=12.0,
            output_tokens=7,
        )
        return AgentEndEvent(
            task_id=TASK_ID,
            iteration=4,
            user_input="the prompt",
            agent_output="the output",
            duration_seconds=9.5,
            usage=TokenUsage(output_tokens=7),
            model_used="model-z",
            assistant_turn_count=3,
            messages=[msg],
            num_turns=3,
            max_turns_exhausted=True,
            result_summary=ResultSummary(is_error=False, subtype="success", result="all done"),
            crashed=True,
            crash_reason="boom",
        )

    def test_no_turn_record_field_is_unaccounted_for(self):
        # Guards against a new TurnRecord field that is neither derived nor
        # asserted below: it must be classified in one bucket or the other, so
        # adding a field forces a conscious decision (and a test update).
        verbatim = set(AgentEndEvent.model_fields) & set(TurnRecord.model_fields)
        accounted = verbatim | self._DERIVED
        missing = set(TurnRecord.model_fields) - accounted
        assert not missing, f"TurnRecord field(s) not sourced from AgentEndEvent or derived: {missing}"

    def test_every_verbatim_field_round_trips(self):
        collector = EventCollector()
        end = self._full_agent_end()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="ignored", iteration=0),
                ToolEndEvent(task_id=TASK_ID, tool=_tool("a", 0)),
                end,
            ],
        )
        record = collector.build_turn_record()

        # Derived fields land from their own source, not from defaults.
        assert [c.tool_id for c in record.commands] == ["a"]
        assert record.token_usage is not None and record.token_usage.output_tokens == 7

        # Every field shared with AgentEndEvent must equal the event's value, so
        # nothing falls through to a TurnRecord default.
        verbatim = (set(AgentEndEvent.model_fields) & set(TurnRecord.model_fields)) - self._DERIVED
        for name in verbatim:
            event_value = getattr(end, name)
            record_value = getattr(record, name)
            # messages are copied into a new list; compare contents.
            if isinstance(event_value, list):
                assert record_value == list(event_value), f"{name} did not round-trip"
            else:
                assert record_value == event_value, f"{name}: record={record_value!r} event={event_value!r}"


_GEN_BASE = datetime(2026, 9, 11, 9, 0, 0)
_GEN_WINDOW_MS = 1.0


def _assistant(
    *,
    window: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    parent_tool_use_id: str | None = None,
) -> AssistantMessage:
    """One generation, shaped the way a reducer emits one.

    The bounds SPAN the published duration, and `window` tiles successive
    messages rather than leaving them on one instant. Both matter to
    `subtract_tool_time`, which asserts that a group's published total equals
    the span its bounds describe and which GROUPS on those bounds: two
    messages sharing an instant would be read as one Codex-style split window
    and then violate the equality by summing to twice it.
    """
    started = _GEN_BASE + timedelta(milliseconds=_GEN_WINDOW_MS * window)
    return AssistantMessage(
        started_at=started,
        completed_at=started + timedelta(milliseconds=_GEN_WINDOW_MS),
        generation_duration_ms=_GEN_WINDOW_MS,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        parent_tool_use_id=parent_tool_use_id,
    )


def _transcript_sum(record: TurnRecord) -> TokenUsage:
    """Sum the four token buckets across the transcript (assistant + reconciliation).

    Mirrors how a downstream consumer (the evalboard) sums the message stream:
    only the per-message token buckets, no separate aggregate.
    """
    u = TokenUsage()
    for m in record.messages:
        # Assistant generations and the synthetic reconciliation entry both carry
        # the four token buckets under identical field names; user/simulator
        # messages are a separate bill and excluded.
        if isinstance(m, AssistantMessage | ReconciliationMessage):
            u = u + TokenUsage(
                uncached_input_tokens=m.input_tokens,
                output_tokens=m.output_tokens,
                cache_creation_input_tokens=m.cache_creation_tokens,
                cache_read_input_tokens=m.cache_read_tokens,
            )
    return u


class TestReconciliation:
    """The synthetic ReconciliationMessage makes the transcript's token buckets
    sum EXACTLY to the authoritative turn total (collector.py ``_reconciled_messages``).

    This is the invariant the evalboard relies on to drop its separate aggregate:
    sum(message buckets) == token_usage, for any agent.
    """

    def _build(self, messages, usage: TokenUsage) -> TurnRecord:
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1),
                AgentEndEvent(task_id=TASK_ID, usage=usage, messages=messages),
            ],
        )
        return collector.build_turn_record()

    def test_claude_shaped_gap_is_booked_so_transcript_reconciles(self):
        # Claude: model_usage total exceeds the per-message sum (a fixed ~512 input
        # slice + sub-agent input ride on no streamed message).
        messages = [
            _assistant(window=0, input_tokens=100, output_tokens=40, cache_read_tokens=2000),
            _assistant(window=1, input_tokens=50, output_tokens=20, cache_read_tokens=3000),
        ]
        usage = TokenUsage(
            uncached_input_tokens=662,  # 150 + 512 unattributed
            output_tokens=60,
            cache_creation_input_tokens=1000,  # all unattributed (sub-agent)
            cache_read_input_tokens=5000,
            total_cost_usd=0.12,
        )
        record = self._build(messages, usage)

        recon = [m for m in record.messages if isinstance(m, ReconciliationMessage)]
        assert len(recon) == 1
        assert recon[0].input_tokens == 512
        assert recon[0].cache_creation_tokens == 1000
        assert recon[0].output_tokens == 0
        assert recon[0].cache_read_tokens == 0
        # The invariant: transcript buckets sum to the authoritative total.
        s = _transcript_sum(record)
        assert s.uncached_input_tokens == usage.uncached_input_tokens
        assert s.output_tokens == usage.output_tokens
        assert s.cache_creation_input_tokens == usage.cache_creation_input_tokens
        assert s.cache_read_input_tokens == usage.cache_read_input_tokens

    def test_codex_shaped_with_subagent_messages_reconciles(self):
        # Codex: parent + recovered sub-agent (parent_tool_use_id) generations, with
        # the folded total slightly above the streamed sum.
        messages = [
            _assistant(window=0, input_tokens=200, output_tokens=80, cache_read_tokens=1000),
            _assistant(window=1, input_tokens=300, output_tokens=20, parent_tool_use_id="call_sub"),
        ]
        usage = TokenUsage(
            uncached_input_tokens=520,  # 500 + 20 residual
            output_tokens=100,
            cache_read_input_tokens=1000,
        )
        record = self._build(messages, usage)
        s = _transcript_sum(record)
        assert s.uncached_input_tokens == 520
        assert s.output_tokens == 100
        assert s.cache_read_input_tokens == 1000

    def test_no_reconciliation_when_already_exact(self):
        messages = [_assistant(input_tokens=150, output_tokens=60, cache_read_tokens=5000)]
        usage = TokenUsage(uncached_input_tokens=150, output_tokens=60, cache_read_input_tokens=5000)
        record = self._build(messages, usage)
        assert not any(isinstance(m, ReconciliationMessage) for m in record.messages)
        assert len(record.messages) == 1

    def test_no_reconciliation_when_usage_is_none(self):
        # All-zero costless usage coalesces to None → no authoritative target → no entry.
        record = self._build([_assistant(output_tokens=0)], TokenUsage())
        assert record.token_usage is None
        assert not any(isinstance(m, ReconciliationMessage) for m in record.messages)

    def test_simulator_user_tokens_do_not_count_toward_the_sum(self):
        # UserMessage simulator tokens are a separate bill; only assistant
        # generations are measured against the agent total, so a UserMessage's
        # tokens must not shrink the booked residual.
        from coder_eval.models import UserMessage

        messages = [
            UserMessage(text="hi", input_tokens=999, output_tokens=999),
            _assistant(input_tokens=100, output_tokens=40),
        ]
        usage = TokenUsage(uncached_input_tokens=150, output_tokens=40)
        record = self._build(messages, usage)
        recon = [m for m in record.messages if isinstance(m, ReconciliationMessage)]
        assert len(recon) == 1
        assert recon[0].input_tokens == 50  # 150 - 100 (assistant only), NOT minus the 999
        assert recon[0].output_tokens == 0

    def test_negative_residual_when_stream_over_reports(self):
        # If the captured generations sum to MORE than the authoritative total for
        # some bucket, the residual is negative. The invariant must still hold
        # (transcript sums to the total), and the note must read for over-report
        # rather than "billed but not surfaced".
        messages = [_assistant(input_tokens=200, output_tokens=40, cache_read_tokens=5000)]
        usage = TokenUsage(uncached_input_tokens=150, output_tokens=40, cache_read_input_tokens=5000)
        record = self._build(messages, usage)
        recon = [m for m in record.messages if isinstance(m, ReconciliationMessage)]
        assert len(recon) == 1
        assert recon[0].input_tokens == -50  # 150 - 200, booked (not clamped)
        assert "over-report" in recon[0].note
        # Invariant holds even with a negative residual.
        assert _transcript_sum(record).uncached_input_tokens == 150

    def test_reconciliation_message_round_trips_through_turnrecord_json(self):
        # The Python serialization boundary: a TurnRecord carrying a
        # ReconciliationMessage must deserialize the entry back to the right type
        # via Discriminator("role") (the TS side is covered; this pins the Python side).
        messages = [_assistant(input_tokens=100, output_tokens=40)]
        usage = TokenUsage(uncached_input_tokens=612, output_tokens=40)
        record = self._build(messages, usage)
        restored = TurnRecord.model_validate(record.model_dump())
        tail = restored.messages[-1]
        assert isinstance(tail, ReconciliationMessage)
        assert tail.role == "reconciliation"
        assert tail.input_tokens == 512
        # Round-trip via JSON string too (not just a dict).
        restored_json = TurnRecord.model_validate_json(record.model_dump_json())
        assert isinstance(restored_json.messages[-1], ReconciliationMessage)


class TestNoTerminalEvent:
    """A mid-stream snapshot (no AgentEndEvent) builds a minimal record."""

    def test_minimal_record_without_agent_end(self):
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=3, model="gpt-x"),
                TurnStartEvent(task_id=TASK_ID, turn_id="t1"),
                ToolEndEvent(task_id=TASK_ID, tool=_tool("a", 0)),
            ],
        )

        record = collector.build_turn_record()
        assert record.iteration == 3
        assert record.user_input == "go"
        assert record.token_usage is None
        assert record.model_used == "gpt-x"
        assert record.assistant_turn_count == 1
        assert [c.tool_id for c in record.commands] == ["a"]


def _span_ms(started: datetime, completed: datetime) -> float:
    """The window its own bounds describe — what every reducer publishes."""
    return (completed - started).total_seconds() * 1000.0


class TestHarnessOverheadBuckets:
    """The turn's two unexplained ends: before the first generation, after the last.

    Measured live across all five harnesses, these two plus generation plus tool
    execution account for the turn to within 0.1 ms — so what the evalboard shows
    as "Unaccounted" is fully explained rather than merely displayed. The head is
    where the harnesses differ most — every one of them now measures it up to
    its first observed model output, but what that interval CONTAINS ranges from
    ~0.23 s on Pi to ~4.7 s on Antigravity, depending on whether the harness
    spawns its process per turn and how long the provider takes to first token.
    That spread is exactly why it is booked as its own bucket instead of being
    folded into generation.
    """

    @staticmethod
    def _msg(started: datetime, completed: datetime, *, measurable: bool = True) -> AssistantMessage:
        """A generation window. ``measurable=False`` is the placeholder shape
        every fabricated-bounds producer writes — a rollout rebuild or a
        sub-agent recovery — which stamps one instant on both bounds and says
        so with ``generation_duration_ms=None``."""
        return AssistantMessage(
            started_at=started,
            completed_at=completed,
            # Derived, not a literal: `subtract_tool_time` asserts a published
            # window equals the span its own bounds describe.
            generation_duration_ms=_span_ms(started, completed) if measurable else None,
        )

    @staticmethod
    def _subagent_msg(started: datetime, completed: datetime) -> AssistantMessage:
        """A sub-agent generation: same shape, tagged with the spawning Agent
        call's tool_use_id. Its time is already inside that call's interval."""
        return AssistantMessage(
            started_at=started,
            completed_at=completed,
            generation_duration_ms=_span_ms(started, completed),
            parent_tool_use_id="toolu_agent",
        )

    @staticmethod
    def _tool(started: datetime, completed: datetime, tool_id: str = "t1") -> ToolEndEvent:
        return ToolEndEvent(
            task_id=TASK_ID,
            tool=CommandTelemetry(
                tool_id=tool_id,
                tool_name="Bash",
                timestamp=started,
                sequence_number=0,
                execution_started_at=started,
                execution_completed_at=completed,
                result_status="success",
            ),
        )

    def _record(self, messages, *, start: datetime, end: datetime, tools=()) -> TurnRecord:
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1, timestamp=start),
                *tools,
                AgentEndEvent(
                    task_id=TASK_ID,
                    usage=TokenUsage(output_tokens=1),
                    messages=messages,
                    timestamp=end,
                ),
            ],
        )
        return collector.build_turn_record()

    def test_head_and_tail_are_measured_from_the_agent_event_stamps(self):
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [self._msg(t0.replace(second=2), t0.replace(second=5))],
            start=t0,
            end=t0.replace(second=9),
        )
        assert rec.harness_startup_ms == pytest.approx(2000.0)
        assert rec.harness_teardown_ms == pytest.approx(4000.0)

    def test_a_sub_agent_generation_does_not_move_the_bracket(self):
        """MAIN THREAD ONLY, the rule the two sibling call sites already apply.

        The identity these buckets complete sums generation over the main
        thread only — a sub-agent's run is already inside its parent Agent
        call's interval. Letting a sub-agent message bracket the span shrinks
        the head or the tail by time no bucket then claims, and Codex's
        recovered child messages carry the CHILD's clock, so the bracket can
        move either way.
        """
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [
                self._msg(t0.replace(second=2), t0.replace(second=5)),
                # Stamps outside the main thread's own span, in both directions.
                self._subagent_msg(t0.replace(second=1), t0.replace(second=8)),
            ],
            start=t0,
            end=t0.replace(second=9),
        )
        assert rec.harness_startup_ms == pytest.approx(2000.0)
        assert rec.harness_teardown_ms == pytest.approx(4000.0)

    def test_a_turn_whose_only_generations_are_sub_agent_reports_no_overhead(self):
        """No main-thread window means nothing was measured — None, not 0.0."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [self._subagent_msg(t0.replace(second=2), t0.replace(second=5))],
            start=t0,
            end=t0.replace(second=9),
        )
        assert rec.harness_startup_ms is None
        assert rec.harness_teardown_ms is None

    def test_a_turn_with_no_generation_says_so_rather_than_claiming_zero(self):
        """None means never measured; 0.0 would mean measured-and-instant (CE058)."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record([], start=t0, end=t0.replace(second=9))
        assert rec.harness_startup_ms is None
        assert rec.harness_teardown_ms is None

    def test_a_measured_zero_head_is_zero_not_none(self):
        """claude-code and Antigravity really do open their first window at turn
        start, so their head is a genuine 0.0 — the distinction from None is the
        whole point of the field."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record([self._msg(t0, t0.replace(second=5))], start=t0, end=t0.replace(second=5))
        assert rec.harness_startup_ms == 0.0
        assert rec.harness_teardown_ms == 0.0

    def test_the_tail_ignores_a_trailing_reconciliation_entry(self):
        """It is always last when present and carries no timestamps at all, so
        indexing messages[-1] would raise rather than measure."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [
                self._msg(t0.replace(second=1), t0.replace(second=4)),
                ReconciliationMessage(input_tokens=5, note="residual"),
            ],
            start=t0,
            end=t0.replace(second=6),
        )
        assert rec.harness_teardown_ms == pytest.approx(2000.0)

    def test_a_clock_inversion_clamps_rather_than_going_negative(self):
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [self._msg(t0.replace(minute=59, hour=11), t0.replace(second=5))],
            start=t0,
            end=t0.replace(second=1),
        )
        assert rec.harness_startup_ms == 0.0

    def test_a_snapshot_before_the_terminal_event_measures_nothing(self):
        collector = EventCollector()
        _feed(collector, [AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1)])
        rec = collector.build_turn_record()
        assert rec.harness_startup_ms is None
        assert rec.harness_teardown_ms is None

    def test_a_placeholder_message_does_not_supply_the_bounds(self):
        """A Codex turn rebuilt from its rollout stamps every message at turn
        END and marks them generation_duration_ms=None. Reading those stamps as
        window bounds books the WHOLE TURN as harness startup."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [
                self._msg(t0.replace(second=2), t0.replace(second=5)),
                self._msg(t0.replace(second=9), t0.replace(second=9), measurable=False),
            ],
            start=t0,
            end=t0.replace(second=9),
        )
        assert rec.harness_startup_ms == pytest.approx(2000.0)
        # 9s - 5s, measured off the real window, not off the placeholder's stamp.
        assert rec.harness_teardown_ms == pytest.approx(4000.0)

    def test_a_turn_of_only_placeholders_measures_nothing(self):
        """codex_g_items_rebuild's shape: an assistant message exists, but
        nothing in it was timed, so there is no end to measure against."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [self._msg(t0.replace(second=9), t0.replace(second=9), measurable=False)],
            start=t0,
            end=t0.replace(second=9),
        )
        assert rec.harness_startup_ms is None
        assert rec.harness_teardown_ms is None

    def test_the_bounds_do_not_depend_on_append_order(self):
        """Codex appends recovered sub-agent messages after the parent's last
        flush, so the list is not ordered by time."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [
                self._msg(t0.replace(second=6), t0.replace(second=8)),
                self._msg(t0.replace(second=2), t0.replace(second=4)),
            ],
            start=t0,
            end=t0.replace(second=9),
        )
        assert rec.harness_startup_ms == pytest.approx(2000.0)
        assert rec.harness_teardown_ms == pytest.approx(1000.0)

    def test_a_tool_running_past_the_last_window_is_not_counted_twice(self):
        """Antigravity force-closes an orphan at finalization, stamping its
        completion inside the tail, and backgrounds anything over ten seconds.
        Such a span is already in the tool bucket, so leaving it in the tail
        books it twice and drives the residual sharply negative."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [self._msg(t0.replace(second=1), t0.replace(second=4))],
            start=t0,
            end=t0.replace(second=9),
            tools=[self._tool(t0.replace(second=3), t0.replace(second=7))],
        )
        # Tail spans 4s->9s = 5s, of which 4s->7s = 3s was the tool still running.
        assert rec.harness_teardown_ms == pytest.approx(2000.0)

    def test_a_tool_running_before_the_first_window_is_not_counted_twice(self):
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        rec = self._record(
            [self._msg(t0.replace(second=5), t0.replace(second=8))],
            start=t0,
            end=t0.replace(second=8),
            tools=[self._tool(t0.replace(second=1), t0.replace(second=3))],
        )
        # Head spans 0s->5s = 5s, of which 1s->3s = 2s was tool execution.
        assert rec.harness_startup_ms == pytest.approx(3000.0)

    def test_a_new_turn_clears_the_previous_turn_terminal_event(self):
        """EarlyStopWatcher keeps ONE collector across retries. Left stale, the
        next attempt's start pairs with the last attempt's end and the clamped
        inversion publishes as a measured 0.0."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        collector = EventCollector()
        _feed(
            collector,
            [
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1, timestamp=t0),
                AgentEndEvent(
                    task_id=TASK_ID,
                    usage=TokenUsage(output_tokens=1),
                    messages=[self._msg(t0.replace(second=1), t0.replace(second=2))],
                    timestamp=t0.replace(second=3),
                    crashed=True,
                ),
                # Retry, a minute later, with no terminal event of its own yet.
                AgentStartEvent(task_id=TASK_ID, prompt="go", iteration=1, timestamp=t0.replace(minute=1)),
            ],
        )
        rec = collector.build_turn_record()
        assert rec.harness_startup_ms is None
        assert rec.harness_teardown_ms is None


class TestSubtractToolTime:
    """The ONE tool subtraction, moved here from five reducers.

    Four of them did it inside `close_window` as they flushed; claude-code did
    it once at finalization. Head and tail were already computed centrally, in
    this module — that asymmetry was the complexity, and every timing defect
    this branch fixed lived in the per-reducer bookkeeping around the
    subtraction rather than in the subtraction itself.
    """

    BASE: ClassVar[datetime] = datetime(2026, 9, 11, 9, 0, 0)

    @classmethod
    def _at(cls, ms: float) -> datetime:
        return cls.BASE + timedelta(milliseconds=ms)

    @classmethod
    def _msg(cls, lo: float, hi: float, gen: float | None, **kwargs) -> AssistantMessage:
        return AssistantMessage(started_at=cls._at(lo), completed_at=cls._at(hi), generation_duration_ms=gen, **kwargs)

    def test_a_contained_tool_is_subtracted_exactly_once(self):
        out = subtract_tool_time([self._msg(0, 1000, 1000.0)], [(self._at(200), self._at(700))])
        assert out[0].generation_duration_ms == pytest.approx(500.0)

    def test_the_input_messages_are_not_mutated(self):
        """Non-mutating because of ALIASING, not because of repeated calls.

        Every agent builds its terminal event as
        `AgentEndEvent(messages=list(...))`, which copies the LIST and not the
        message objects — so an in-place write would reach back into the
        agent's own live state from the collector.
        """
        messages = [self._msg(0, 1000, 1000.0)]
        subtract_tool_time(messages, [(self._at(200), self._at(700))])
        assert messages[0].generation_duration_ms == pytest.approx(1000.0)

    def test_a_group_sharing_bounds_is_subtracted_once_and_the_parts_still_sum(self):
        """Codex splits one window across two sub-messages by output share.

        Subtracting the group's overlap from each part separately would take it
        twice and stop the parts summing to the window. Grouping is on the
        BOUNDS, not on `message_id` — OpenCode and Pi can carry `None` there.
        """
        # A 1000 ms window split 25/75, with a 250 ms tool inside it.
        out = subtract_tool_time(
            [self._msg(0, 1000, 250.0, message_id="m"), self._msg(0, 1000, 750.0, message_id="m")],
            [(self._at(300), self._at(550))],
        )
        assert [m.generation_duration_ms for m in out] == [pytest.approx(187.5), pytest.approx(562.5)]
        assert sum(m.generation_duration_ms or 0.0 for m in out) == pytest.approx(750.0)

    def test_a_group_with_no_message_id_is_still_grouped_by_its_bounds(self):
        """The case keying on `message_id` would break.

        Two id-less messages sharing a window must be one group; keying on the
        id would instead collapse every id-less message of the turn into one.
        """
        out = subtract_tool_time(
            [self._msg(0, 1000, 500.0), self._msg(0, 1000, 500.0), self._msg(2000, 3000, 1000.0)],
            [(self._at(200), self._at(400))],
        )
        assert sum(m.generation_duration_ms or 0.0 for m in out[:2]) == pytest.approx(800.0)
        assert out[2].generation_duration_ms == pytest.approx(1000.0), "a different window is a different group"

    def test_concurrent_tools_subtract_their_union_not_their_sum(self):
        """Summing would clamp a real generation to zero.

        The expectation is DERIVED from `union_ms` rather than written as a
        literal, so this cannot drift from the rule the rest of the codebase
        applies — and the sum is asserted separately to be the wrong answer.
        """
        spans = [
            (self._at(100), self._at(500)),
            (self._at(150), self._at(550)),
            (self._at(200), self._at(600)),
            (self._at(250), self._at(650)),
        ]
        out = subtract_tool_time([self._msg(0, 1000, 1000.0)], spans)
        assert out[0].generation_duration_ms == pytest.approx(1000.0 - union_ms(spans))
        assert sum((e - s).total_seconds() * 1000.0 for s, e in spans) > 1000.0, (
            "the fixture must actually over-subtract when summed, or this proves nothing"
        )
        assert out[0].generation_duration_ms > 0.0

    def test_a_window_entirely_covered_by_tools_is_a_measured_zero(self):
        out = subtract_tool_time([self._msg(0, 1000, 1000.0)], [(self._at(0), self._at(1000))])
        assert out[0].generation_duration_ms == 0.0, "a measurement, not an absence"

    def test_a_none_duration_stays_none(self):
        """`None` means no window was ever measured, and CE058 keeps it distinct."""
        out = subtract_tool_time([self._msg(0, 1000, None)], [(self._at(0), self._at(500))])
        assert out[0].generation_duration_ms is None

    def test_a_zero_group_does_not_divide_by_zero(self):
        out = subtract_tool_time([self._msg(0, 1000, 0.0)], [(self._at(0), self._at(500))])
        assert out[0].generation_duration_ms == 0.0

    def test_a_sub_agent_generation_is_skipped(self):
        """Its own tools are not in this span set, and the spawning Agent call
        already covers its whole run."""
        out = subtract_tool_time([self._msg(0, 1000, 900.0, parent_tool_use_id="t1")], [(self._at(0), self._at(500))])
        assert out[0].generation_duration_ms == pytest.approx(900.0)

    def test_a_crash_partial_with_no_messages_and_live_spans_is_safe(self):
        """The shape a crashed turn actually produces.

        `Agent._finalize` builds a record from whatever the collector saw, and
        a turn that died before its first emission has resolved tool calls but
        NO messages. Nothing to group, nothing to subtract — and no
        ZeroDivisionError, no IndexError, and no invented entry.
        """
        out = subtract_tool_time([], [(self._at(0), self._at(500))])
        assert out == []

    def test_non_assistant_entries_pass_through_by_identity(self):
        reconciliation = ReconciliationMessage(
            input_tokens=1, output_tokens=1, cache_creation_tokens=0, cache_read_tokens=0, note="n"
        )
        out = subtract_tool_time([self._msg(0, 1000, 1000.0), reconciliation], [(self._at(0), self._at(200))])
        assert out[1] is reconciliation


class TestAPublishedWindowMustMatchItsOwnBounds:
    """The seam assertion: a group's raw total is the span its bounds describe.

    That equality is what lets `generation_duration_ms` stay a PUBLISHED field
    instead of one the collector derives from the bounds — the migration that
    was considered and cut, on the grounds that this check makes deferring it
    safe. It is largely true by construction (CE061 forces every reducer
    through `timing.close_window`); what it catches is a reducer that bypasses
    the helper, and a third-party agent registered through the
    `coder_eval.plugins` SPI, which no lint rule scoped to `agents/` can see.
    """

    BASE: ClassVar[datetime] = datetime(2026, 9, 11, 9, 0, 0)

    @classmethod
    def _at(cls, ms: float) -> datetime:
        return cls.BASE + timedelta(milliseconds=ms)

    @classmethod
    def _msg(cls, lo: float, hi: float, gen: float | None, **kwargs) -> AssistantMessage:
        return AssistantMessage(started_at=cls._at(lo), completed_at=cls._at(hi), generation_duration_ms=gen, **kwargs)

    def test_a_narrowed_window_raises_and_names_both_numbers(self):
        with pytest.raises(ValueError) as excinfo:
            subtract_tool_time([self._msg(0, 1000, 400.0)], [])
        message = str(excinfo.value)
        assert "400.000000" in message and "1000.000000" in message
        assert "generation_duration_ms" in message

    def test_a_widened_window_raises_too(self):
        with pytest.raises(ValueError):
            subtract_tool_time([self._msg(0, 1000, 1600.0)], [])

    def test_a_window_that_matches_its_bounds_passes(self):
        out = subtract_tool_time([self._msg(0, 1000, 1000.0)], [])
        assert out[0].generation_duration_ms == pytest.approx(1000.0)

    def test_codexs_split_passes_when_the_parts_sum_to_the_window(self):
        """Built with `_flush_message`'s own idiom, not a hand-picked pair.

        Codex divides one window across two sub-messages by output-token share,
        rounding every share but the last to 6 places and giving the last the
        remainder — so the tolerance is exercised against the real rounding
        rather than against exact halves.
        """
        window_ms = 1000.0
        first = round(window_ms * (1.0 / 3.0), 6)
        parts = [first, window_ms - first]
        out = subtract_tool_time([self._msg(0, 1000, part, message_id="m") for part in parts], [])
        assert sum(m.generation_duration_ms or 0.0 for m in out) == pytest.approx(window_ms)

    def test_a_split_whose_parts_sum_to_the_wrong_total_raises(self):
        with pytest.raises(ValueError):
            subtract_tool_time([self._msg(0, 1000, 400.0, message_id="m"), self._msg(0, 1000, 400.0)], [])

    def test_a_zero_group_is_skipped_before_the_check_runs(self):
        """The `raw_total <= 0` skip runs FIRST, and must keep running first.

        A window measured at zero between IDENTICAL bounds would satisfy the
        equality anyway; the case that needs the order is a `0.0` published
        beside bounds that are not identical, which is a shape the tree
        tolerates today. Raising on it would turn a tolerated record into a
        killed turn, so the bounds here are deliberately 500 ms apart.
        """
        out = subtract_tool_time([self._msg(0, 500, 0.0)], [])
        assert out[0].generation_duration_ms == 0.0

    def test_an_unmeasured_window_never_reaches_the_check(self):
        out = subtract_tool_time([self._msg(0, 5000, None)], [])
        assert out[0].generation_duration_ms is None

    def test_a_sub_agent_message_is_excluded_even_with_placeholder_bounds(self):
        """Its bounds are an admitted placeholder and cannot support its duration.

        Codex's recovered child messages carry the CHILD's clock, and Claude's
        synthesized sub-agent terminal stamps one instant on both bounds. They
        are skipped before the group is built, so the check never sees them.
        """
        out = subtract_tool_time([self._msg(0, 0, 900.0, parent_tool_use_id="toolu_agent")], [])
        assert out[0].generation_duration_ms == pytest.approx(900.0)


class TestBuildTurnRecordIsIdempotent:
    """Building the record twice must give the same numbers.

    `EventCollector` is not built once and read once. `EarlyStopWatcher` holds
    ONE across a turn's tool-call rounds and calls `build_turn_record()` on
    every one, and the crash path builds it again from `Agent._finalize`. The
    tool subtraction now happens inside that method, so a version of it that
    mutated would subtract again on every call — and the numbers would depend
    on how many times something happened to look.
    """

    BASE: ClassVar[datetime] = datetime(2026, 9, 11, 9, 0, 0)

    def _collector(self) -> EventCollector:
        at = lambda ms: self.BASE + timedelta(milliseconds=ms)  # noqa: E731
        collector = EventCollector()
        collector.on_event(AgentStartEvent(task_id="t", prompt="go", iteration=1, timestamp=at(0)))
        collector.on_event(
            ToolEndEvent(
                task_id="t",
                turn_id="t1",
                tool=CommandTelemetry(
                    tool_name="bash",
                    tool_id="c1",
                    timestamp=at(700),
                    execution_started_at=at(700),
                    execution_completed_at=at(1200),
                    result_status="success",
                ),
            )
        )
        collector.on_event(
            AgentEndEvent(
                task_id="t",
                status=AgentEndStatus.COMPLETED,
                messages=[
                    AssistantMessage(
                        started_at=at(500), completed_at=at(2000), generation_duration_ms=1500.0, output_tokens=5
                    )
                ],
                usage=TokenUsage(output_tokens=5),
                timestamp=at(2500),
            )
        )
        return collector

    def test_two_builds_agree_on_every_timing_figure(self):
        collector = self._collector()
        first, second = collector.build_turn_record(), collector.build_turn_record()

        assert [m.generation_duration_ms for m in first.messages if m.role == "assistant"] == [
            m.generation_duration_ms for m in second.messages if m.role == "assistant"
        ]
        assert first.harness_startup_ms == second.harness_startup_ms
        assert first.harness_teardown_ms == second.harness_teardown_ms

    def test_the_first_build_already_subtracted_once(self):
        """Guards the other direction: identical-but-wrong would also pass above."""
        record = self._collector().build_turn_record()
        generation = [m.generation_duration_ms for m in record.messages if m.role == "assistant"]
        # A 1500 ms window holding a 500 ms tool.
        assert generation == [pytest.approx(1000.0)]

    def test_the_agents_own_message_objects_are_not_written_through(self):
        """The aliasing case, which is the real reason for `model_copy`.

        `AgentEndEvent(messages=list(...))` copies the LIST, not the messages,
        so the objects the collector receives are the agent's own live state.
        """
        at = lambda ms: self.BASE + timedelta(milliseconds=ms)  # noqa: E731
        message = AssistantMessage(started_at=at(0), completed_at=at(1000), generation_duration_ms=1000.0)
        collector = EventCollector()
        collector.on_event(AgentStartEvent(task_id="t", prompt="go", iteration=1, timestamp=at(0)))
        collector.on_event(
            ToolEndEvent(
                task_id="t",
                turn_id="t1",
                tool=CommandTelemetry(
                    tool_name="bash",
                    tool_id="c1",
                    timestamp=at(200),
                    execution_started_at=at(200),
                    execution_completed_at=at(700),
                    result_status="success",
                ),
            )
        )
        collector.on_event(
            AgentEndEvent(task_id="t", status=AgentEndStatus.COMPLETED, messages=[message], timestamp=at(1000))
        )
        collector.build_turn_record()

        assert message.generation_duration_ms == pytest.approx(1000.0), "the agent's own object must be untouched"


class TestOverheadExcludesSubAgentTools:
    """The head and tail are bracketed on the MAIN thread, commands included.

    `_overhead_ms` filtered its GENERATIONS to the main thread and then passed
    EVERY command as a tool span, so its own claim to keep all four buckets
    measuring one thread was true only by luck: a child nests inside the parent
    Agent call, whose interval the union already covers. Codex's recovered
    child tools carry the CHILD's clock, so nothing made it true by
    construction — and the evalboard's twin DOES filter, so the two agreed by
    accident.
    """

    BASE: ClassVar[datetime] = datetime(2026, 9, 11, 9, 0, 0)

    def test_a_sub_agent_tool_inside_the_head_does_not_shrink_it(self):
        at = lambda ms: self.BASE + timedelta(milliseconds=ms)  # noqa: E731
        collector = EventCollector()
        collector.on_event(AgentStartEvent(task_id="t", prompt="go", iteration=1, timestamp=at(0)))
        # A sub-agent's own tool call, sitting inside what is otherwise head.
        collector.on_event(
            ToolEndEvent(
                task_id="t",
                turn_id="t1",
                tool=CommandTelemetry(
                    tool_name="Bash",
                    tool_id="child-1",
                    timestamp=at(100),
                    execution_started_at=at(100),
                    execution_completed_at=at(400),
                    result_status="success",
                ),
            )
        )
        collector.on_event(
            AgentEndEvent(
                task_id="t",
                status=AgentEndStatus.COMPLETED,
                messages=[
                    AssistantMessage(started_at=at(500), completed_at=at(1000), generation_duration_ms=500.0),
                    # The child generation that OWNS child-1.
                    AssistantMessage(
                        started_at=at(100),
                        completed_at=at(400),
                        generation_duration_ms=300.0,
                        parent_tool_use_id="agent-call",
                        tool_use_ids=["child-1"],
                    ),
                ],
                timestamp=at(1500),
            )
        )
        record = collector.build_turn_record()

        # 500 ms of head, all of it. Counting the child's tool would book 300 ms
        # of it as tool execution that no main-thread bucket claims.
        assert record.harness_startup_ms == pytest.approx(500.0)
        assert record.harness_teardown_ms == pytest.approx(500.0)

    def test_a_main_thread_tool_inside_the_head_still_shrinks_it(self):
        """The control: the filter must exclude children, not all commands."""
        at = lambda ms: self.BASE + timedelta(milliseconds=ms)  # noqa: E731
        collector = EventCollector()
        collector.on_event(AgentStartEvent(task_id="t", prompt="go", iteration=1, timestamp=at(0)))
        collector.on_event(
            ToolEndEvent(
                task_id="t",
                turn_id="t1",
                tool=CommandTelemetry(
                    tool_name="Bash",
                    tool_id="main-1",
                    timestamp=at(100),
                    execution_started_at=at(100),
                    execution_completed_at=at(400),
                    result_status="success",
                ),
            )
        )
        collector.on_event(
            AgentEndEvent(
                task_id="t",
                status=AgentEndStatus.COMPLETED,
                messages=[AssistantMessage(started_at=at(500), completed_at=at(1000), generation_duration_ms=500.0)],
                timestamp=at(1500),
            )
        )
        record = collector.build_turn_record()
        assert record.harness_startup_ms == pytest.approx(200.0), "500 ms of head minus a 300 ms tool"
