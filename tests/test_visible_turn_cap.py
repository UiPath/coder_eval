"""``run_limits.max_turns`` must mean the same thing on Codex and Antigravity.

Neither SDK can express the cap natively — each delivers exactly one SDK turn per
``communicate()`` call, so a native counter would clamp at 1 no matter what the task
asked for. Both therefore count VISIBLE turns (resolved tool calls) off one shared
definition, ``EventCollector.visible_turn_count``, rather than two per-agent counters
that happen to agree. See docs/agents/HARNESS_PARITY.md.

Per-agent enforcement (where the cap fires in the loop, and how the run finalizes)
is covered in test_codex_agent.py and test_antigravity_agent.py.
"""

from datetime import datetime

import pytest

from coder_eval.agents.antigravity_agent import AntigravityAgent
from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.models import CommandTelemetry
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import ToolEndEvent, ToolEndStatus


def _tool_end(collector: EventCollector, tool_id: str) -> None:
    collector.on_event(
        ToolEndEvent(
            task_id="t",
            turn_id="turn-1",
            tool=CommandTelemetry(tool_name="Bash", tool_id=tool_id, timestamp=datetime.now(), sequence_number=0),
            status=ToolEndStatus.OK,
        )
    )


def test_collector_visible_turn_count_counts_resolved_tool_calls():
    """The single definition Codex and Antigravity both cap against."""
    collector = EventCollector()
    assert collector.visible_turn_count == 0

    _tool_end(collector, "a")
    _tool_end(collector, "b")

    assert collector.visible_turn_count == 2


def test_collector_visible_turn_count_does_not_double_count_a_tool_id():
    """Keyed on tool_id, so a re-emitted end event cannot inflate the count past the cap."""
    collector = EventCollector()

    _tool_end(collector, "a")
    _tool_end(collector, "a")

    assert collector.visible_turn_count == 1


def test_collector_visible_turn_count_matches_the_built_record():
    """It is the live view of exactly the list ``TurnRecord.commands`` ends up holding."""
    collector = EventCollector()
    for tool_id in ("a", "b", "c"):
        _tool_end(collector, tool_id)

    assert collector.visible_turn_count == len(collector.build_turn_record().commands)


@pytest.mark.parametrize("agent_cls", [CodexAgent, AntigravityAgent])
def test_both_capped_agents_declare_cooperative_stop(agent_cls):
    """The turn cap reuses the cooperative-stop boundary, so both must support it."""
    assert agent_cls.supports_cooperative_stop is True
