"""Unit tests for ``EventCollector`` reduction branches.

Feeds hand-built event lists to a bare ``EventCollector()`` (no agent) and
asserts ``build_turn_record()`` honors the coalescing / filtering / ordering
rules in ``coder_eval/streaming/collector.py``.
"""

from datetime import datetime
from typing import ClassVar

from coder_eval.models import (
    AgentUsage,
    AssistantMessage,
    CommandTelemetry,
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
                    usage=AgentUsage(tokens=TokenUsage()),  # all-zero, no cost
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
                    usage=AgentUsage(tokens=TokenUsage(output_tokens=5)),
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
                    usage=AgentUsage(tokens=TokenUsage(total_cost_usd=0.0)),
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
                    usage=AgentUsage(tokens=TokenUsage(output_tokens=10)),
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
                AgentEndEvent(task_id=TASK_ID, usage=AgentUsage(tokens=TokenUsage(output_tokens=1))),
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
                AgentEndEvent(task_id=TASK_ID, usage=AgentUsage(tokens=TokenUsage(output_tokens=1))),
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
            usage=AgentUsage(tokens=TokenUsage(output_tokens=7)),
            model_used="model-z",
            assistant_turn_count=3,
            messages=[msg],
            num_turns=3,
            max_turns_exhausted=True,
            result_summary=ResultSummary(is_error=False, subtype="success", result="all done"),
            crashed=True,
            crash_reason="boom",
            sub_agent_usage=[AgentUsage(tokens=TokenUsage(output_tokens=2), tool_uses=1)],
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
            # messages / sub_agent_usage are copied into new lists; compare contents.
            if isinstance(event_value, list):
                assert record_value == list(event_value), f"{name} did not round-trip"
            else:
                assert record_value == event_value, f"{name}: record={record_value!r} event={event_value!r}"


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
