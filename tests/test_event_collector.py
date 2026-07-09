"""Unit tests for ``EventCollector`` reduction branches.

Feeds hand-built event lists to a bare ``EventCollector()`` (no agent) and
asserts ``build_turn_record()`` honors the coalescing / filtering / ordering
rules in ``coder_eval/streaming/collector.py``.
"""

from datetime import datetime
from typing import ClassVar

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
    #   commands     -> reduced from the ToolEnd stream
    #   token_usage  -> derived from end.usage.tokens
    #   timestamp    -> record's own creation stamp, not an event field
    _DERIVED: ClassVar[set[str]] = {"commands", "token_usage", "timestamp"}

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
