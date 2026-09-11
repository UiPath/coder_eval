"""Unit tests for ``EventCollector`` reduction branches.

Feeds hand-built event lists to a bare ``EventCollector()`` (no agent) and
asserts ``build_turn_record()`` honors the coalescing / filtering / ordering
rules in ``coder_eval/streaming/collector.py``.
"""

from datetime import datetime
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
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentStartEvent,
    ToolEndEvent,
    TurnStartEvent,
)


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
        msg = AssistantMessage(
            started_at=datetime.now(),
            completed_at=datetime.now(),
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


def _assistant(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    parent_tool_use_id: str | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        started_at=datetime.now(),
        completed_at=datetime.now(),
        generation_duration_ms=1.0,
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
            _assistant(input_tokens=100, output_tokens=40, cache_read_tokens=2000),
            _assistant(input_tokens=50, output_tokens=20, cache_read_tokens=3000),
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
            _assistant(input_tokens=200, output_tokens=80, cache_read_tokens=1000),
            _assistant(input_tokens=300, output_tokens=20, parent_tool_use_id="call_sub"),
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


class TestHarnessOverheadBuckets:
    """The turn's two unexplained ends: before the first generation, after the last.

    Measured live across all five harnesses, these two plus generation plus tool
    execution account for the turn to within 0.1 ms — so what the evalboard shows
    as "Unaccounted" is fully explained rather than merely displayed. The head is
    where the harnesses differ most (OpenCode ~3.0 s of CLI boot + TTFT fused,
    claude-code a measured 0.0 because its first window already covers dispatch),
    which is exactly why it is booked as its own bucket instead of being folded
    into generation.
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
            generation_duration_ms=1.0 if measurable else None,
        )

    @staticmethod
    def _subagent_msg(started: datetime, completed: datetime) -> AssistantMessage:
        """A sub-agent generation: same shape, tagged with the spawning Agent
        call's tool_use_id. Its time is already inside that call's interval."""
        return AssistantMessage(
            started_at=started,
            completed_at=completed,
            generation_duration_ms=1.0,
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
