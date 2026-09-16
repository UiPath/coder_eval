"""``TurnMonitor``: the one answerer of the agent's ``should_stop`` poll."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from coder_eval.models import (
    AgentKind,
    CommandTelemetry,
    EarlyStopReason,
    FileExistsCriterion,
    RunLimits,
    SandboxConfig,
    SkillTriggeredCriterion,
    StopEarlyPolicy,
    TaskDefinition,
    TokenUsage,
    parse_agent_config,
)
from coder_eval.orchestration.turn_monitor import TurnMonitor
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentStartEvent,
    StopReason,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
)
from tests._fixtures.live_criteria import FROZEN_TS


def _task(*, criteria: list[Any] | None = None, max_tool_calls: int | None = None) -> TaskDefinition:
    return TaskDefinition(
        task_id="monitor-test",
        description="monitor test task",
        initial_prompt="do the thing",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=criteria or [FileExistsCriterion(path="x", description="x exists")],
        run_limits=RunLimits(max_tool_calls=max_tool_calls),
    )


def _skill_crit(skill: str, *, on_pass: bool) -> SkillTriggeredCriterion:
    return SkillTriggeredCriterion(
        type="skill_triggered",
        description=f"{skill} activation",
        skill_name=skill,
        expected_skill=skill,
        stop_early=StopEarlyPolicy(on_pass="stop" if on_pass else "continue"),
    )


def _cmd(tool_id: str, *, tool_name: str = "Bash", parameters: dict[str, Any] | None = None) -> CommandTelemetry:
    return CommandTelemetry(tool_name=tool_name, tool_id=tool_id, timestamp=FROZEN_TS, parameters=parameters or {})


def _end(tool_id: str, *, status: ToolEndStatus = ToolEndStatus.OK, **kwargs: Any) -> ToolEndEvent:
    return ToolEndEvent(task_id="t", tool=_cmd(tool_id, **kwargs), status=status)


def _feed(monitor: TurnMonitor, events: list[Any]) -> None:
    for event in events:
        monitor.on_event(event)


class TestToolCallCap:
    def test_the_cap_latches_on_the_resolved_call_that_reaches_it(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=3), arm=True)
        _feed(monitor, [AgentStartEvent(task_id="t"), _end("a"), _end("b")])
        assert monitor.should_stop() is None
        monitor.on_event(_end("c"))
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP
        assert monitor.stop_reason is StopReason.TOOL_CALL_CAP
        assert monitor.info is None

    def test_an_unresolved_end_is_not_counted(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=1), arm=True)
        monitor.on_event(_end("orphan", status=ToolEndStatus.UNRESOLVED))
        assert monitor.tool_calls == 0
        assert monitor.should_stop() is None

    def test_a_re_emitted_end_is_not_counted_twice(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=2), arm=True)
        _feed(monitor, [_end("a"), _end("a")])
        assert monitor.tool_calls == 1
        assert monitor.should_stop() is None

    def test_a_tool_start_alone_does_not_count(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=1), arm=True)
        monitor.on_event(ToolStartEvent(task_id="t", tool=_cmd("a")))
        assert monitor.should_stop() is None

    def test_the_count_is_cumulative_across_communicate_calls(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=3), arm=True)
        _feed(monitor, [AgentStartEvent(task_id="t"), _end("a"), _end("b"), AgentEndEvent(task_id="t")])
        assert monitor.should_stop() is None
        _feed(monitor, [AgentStartEvent(task_id="t"), _end("c")])
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP

    def test_a_latched_cap_stops_the_next_attempt_at_its_first_poll(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=1), arm=True)
        _feed(monitor, [AgentStartEvent(task_id="t"), _end("a"), AgentEndEvent(task_id="t", crashed=True)])
        monitor.on_event(AgentStartEvent(task_id="t"))
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP

    def test_no_cap_never_stops(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=None), arm=True)
        _feed(monitor, [_end(str(i)) for i in range(50)])
        assert monitor.should_stop() is None
        assert monitor.tool_calls == 50

    def test_an_unarmed_monitor_still_caps(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria, max_tool_calls=1), arm=False)
        assert not monitor.armed
        monitor.on_event(_end("sk", tool_name="Skill", parameters={"skill": "date-teller"}))
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP
        assert monitor.info is None


class TestEarlyCriterionAndPrecedence:
    def test_arm_false_never_fires_the_criterion(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria), arm=False)
        monitor.on_event(_end("sk", tool_name="Skill", parameters={"skill": "date-teller"}))
        assert monitor.should_stop() is None

    def test_the_armed_stop_wins_a_tie_with_the_cap(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria, max_tool_calls=1), arm=True)
        monitor.on_event(_end("sk", tool_name="Skill", parameters={"skill": "date-teller"}))
        assert monitor.should_stop() is StopReason.EARLY_CRITERION
        assert monitor.info is not None
        assert monitor.info.reason is EarlyStopReason.CRITERION_PASSED

    def test_the_first_latched_reason_is_final(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria, max_tool_calls=1), arm=True)
        monitor.on_event(_end("a"))
        monitor.on_event(_end("sk", tool_name="Skill", parameters={"skill": "date-teller"}))
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP
        assert monitor.info is None

    def test_tool_calls_remaining_at_stop_counts_down_from_the_cap(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria, max_tool_calls=10), arm=True)
        _feed(
            monitor,
            [_end("a"), _end("b"), _end("sk", tool_name="Skill", parameters={"skill": "date-teller"})],
        )
        assert monitor.info is not None
        assert monitor.info.tool_call_index == 3
        assert monitor.info.tool_calls_remaining_at_stop == 7

    def test_tool_calls_remaining_at_stop_is_none_without_a_cap(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria), arm=True)
        monitor.on_event(_end("sk", tool_name="Skill", parameters={"skill": "date-teller"}))
        assert monitor.info is not None
        assert monitor.info.tool_calls_remaining_at_stop is None

    def test_a_raising_verdict_disarms_the_criteria_but_not_the_cap(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria, max_tool_calls=2), arm=True)
        with patch.object(monitor._armed[0][1], "live_verdict", side_effect=RuntimeError("boom")):
            monitor.on_event(_end("a"))
        assert monitor.disarmed
        assert monitor.should_stop() is None
        monitor.on_event(_end("sk", tool_name="Skill", parameters={"skill": "date-teller"}))
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP

    def test_a_verdict_raising_on_the_call_that_reaches_the_cap_still_latches_the_cap(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria, max_tool_calls=1), arm=True)
        with patch.object(monitor._armed[0][1], "live_verdict", side_effect=RuntimeError("boom")):
            monitor.on_event(_end("a"))
        assert monitor.disarmed
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP

    def test_sub_agent_events_are_not_counted(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=1), arm=True)
        monitor.on_event(ToolEndEvent(task_id="t", tool=_cmd("child"), parent_thread_id="parent"))
        assert monitor.tool_calls == 0
        assert monitor.should_stop() is None


class TestUsageAccumulators:
    def test_in_flight_deltas_are_replaced_by_the_authoritative_end_usage(self) -> None:
        monitor = TurnMonitor.for_task(_task(), arm=True)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t"),
                TurnEndEvent(task_id="t", tokens=TokenUsage(uncached_input_tokens=10, output_tokens=5)),
                TurnEndEvent(task_id="t", tokens=None),
                TurnEndEvent(task_id="t", tokens=TokenUsage(uncached_input_tokens=20, output_tokens=5)),
            ],
        )
        assert monitor.usage.uncached_input_tokens == 30
        monitor.on_event(AgentEndEvent(task_id="t", usage=TokenUsage(uncached_input_tokens=28, output_tokens=9)))
        assert (monitor.usage.uncached_input_tokens, monitor.usage.output_tokens) == (28, 9)

    def test_committed_usage_accumulates_across_communicate_calls(self) -> None:
        monitor = TurnMonitor.for_task(_task(), arm=True)
        for _ in range(2):
            _feed(
                monitor,
                [AgentStartEvent(task_id="t"), AgentEndEvent(task_id="t", usage=TokenUsage(output_tokens=4))],
            )
        assert monitor.usage.output_tokens == 8
