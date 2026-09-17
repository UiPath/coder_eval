"""``TurnMonitor``: the one answerer of the agent's ``should_stop`` poll."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from coder_eval.errors import BudgetExceededError, BudgetUnenforceableError
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
    TurnStartEvent,
)
from tests._fixtures.live_criteria import FROZEN_TS


def _task(
    *,
    criteria: list[Any] | None = None,
    max_tool_calls: int | None = None,
    limits: RunLimits | None = None,
    model: str | None = None,
) -> TaskDefinition:
    return TaskDefinition(
        task_id="monitor-test",
        description="monitor test task",
        initial_prompt="do the thing",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, model=model),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=criteria or [FileExistsCriterion(path="x", description="x exists")],
        run_limits=limits if limits is not None else RunLimits(max_tool_calls=max_tool_calls),
    )


def _turn(usage: TokenUsage) -> list[Any]:
    return [AgentStartEvent(task_id="t"), AgentEndEvent(task_id="t", usage=usage)]


def _raised(monitor: TurnMonitor) -> BudgetExceededError:
    with pytest.raises(BudgetExceededError) as exc:
        monitor.raise_if_over_budget(iteration=1)
    return exc.value


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


def _turn_start(turn_id: str) -> TurnStartEvent:
    return TurnStartEvent(task_id="t", turn_id=turn_id)


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


class TestModelTurnCap:
    @staticmethod
    def _monitor(max_turns: int | None, *, arm: bool = True, **limits: Any) -> TurnMonitor:
        return TurnMonitor.for_task(_task(limits=RunLimits(max_turns=max_turns, **limits)), arm=arm)

    def test_the_cap_latches_when_the_turn_past_it_starts(self) -> None:
        monitor = self._monitor(2)
        _feed(monitor, [AgentStartEvent(task_id="t"), _turn_start("t1"), _turn_start("t2")])
        assert monitor.should_stop() is None
        monitor.on_event(_turn_start("t3"))
        assert monitor.should_stop() is StopReason.MODEL_TURN_CAP
        assert monitor.info is None
        assert monitor.model_turns == 3

    def test_exactly_the_cap_never_latches(self) -> None:
        monitor = self._monitor(2)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t"),
                _turn_start("t1"),
                TurnEndEvent(task_id="t", turn_id="t1"),
                _turn_start("t2"),
                TurnEndEvent(task_id="t", turn_id="t2"),
                AgentEndEvent(task_id="t"),
            ],
        )
        assert monitor.should_stop() is None
        assert monitor.model_turns == 2

    def test_a_repeated_turn_id_in_one_call_counts_once(self) -> None:
        monitor = self._monitor(1)
        _feed(monitor, [AgentStartEvent(task_id="t"), _turn_start("t1"), _turn_start("t1")])
        assert monitor.should_stop() is None
        assert monitor.model_turns == 1

    def test_the_same_turn_id_in_a_new_call_counts_again(self) -> None:
        monitor = self._monitor(1)
        _feed(monitor, [AgentStartEvent(task_id="t"), _turn_start("turn_1"), AgentEndEvent(task_id="t")])
        assert monitor.should_stop() is None
        _feed(monitor, [AgentStartEvent(task_id="t"), _turn_start("turn_1")])
        assert monitor.should_stop() is StopReason.MODEL_TURN_CAP
        assert monitor.model_turns == 2

    def test_a_used_up_cap_stops_the_next_attempt_at_its_first_turn(self) -> None:
        monitor = self._monitor(1)
        _feed(monitor, [AgentStartEvent(task_id="t"), _turn_start("t1"), AgentEndEvent(task_id="t", crashed=True)])
        assert monitor.should_stop() is None
        _feed(monitor, [AgentStartEvent(task_id="t"), _turn_start("t1")])
        assert monitor.should_stop() is StopReason.MODEL_TURN_CAP

    def test_nested_turn_starts_are_not_counted(self) -> None:
        monitor = self._monitor(1)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t"),
                *(
                    _turn_start(turn_id).model_copy(update={"thread_id": "agent_1", "parent_thread_id": "agent_1"})
                    for turn_id in ("t1", "t2")
                ),
            ],
        )
        assert monitor.model_turns == 0
        assert monitor.should_stop() is None

    def test_no_cap_never_stops(self) -> None:
        monitor = self._monitor(None)
        _feed(monitor, [_turn_start(f"t{i}") for i in range(50)])
        assert monitor.should_stop() is None
        assert monitor.model_turns == 50

    def test_an_unarmed_monitor_still_caps(self) -> None:
        monitor = self._monitor(1, arm=False)
        _feed(monitor, [_turn_start("t1"), _turn_start("t2")])
        assert monitor.should_stop() is StopReason.MODEL_TURN_CAP

    def test_the_tool_call_cap_latched_first_is_final(self) -> None:
        monitor = self._monitor(1, max_tool_calls=1)
        _feed(monitor, [_turn_start("t1"), _end("a"), _turn_start("t2")])
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP


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

    def test_the_early_stop_turn_index_counts_a_reopened_turn_once(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria), arm=True)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t"),
                _turn_start("t1"),
                _turn_start("t1"),
                _end("sk", tool_name="Skill", parameters={"skill": "date-teller"}),
            ],
        )
        assert monitor.info is not None
        assert monitor.info.sdk_turn_index == 1

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


class TestTokenBudgets:
    @pytest.mark.parametrize(
        ("limits", "usage", "name", "actual"),
        [
            (RunLimits(max_input_tokens=100), TokenUsage(uncached_input_tokens=101), "input_tokens", 101),
            (RunLimits(max_output_tokens=10), TokenUsage(output_tokens=11), "output_tokens", 11),
            (
                RunLimits(max_total_tokens=100),
                TokenUsage(uncached_input_tokens=60, output_tokens=41),
                "total_tokens",
                101,
            ),
            (
                RunLimits(max_input_tokens=100, count_cached_input=True),
                TokenUsage(uncached_input_tokens=50, cache_read_input_tokens=51),
                "input_tokens",
                101,
            ),
            (
                RunLimits(max_input_tokens=100, count_cache_creation=True),
                TokenUsage(uncached_input_tokens=50, cache_creation_input_tokens=51),
                "input_tokens",
                101,
            ),
        ],
    )
    def test_each_bucket_rule_breaches(self, limits: RunLimits, usage: TokenUsage, name: str, actual: int) -> None:
        monitor = TurnMonitor.for_task(_task(limits=limits), arm=True)
        _feed(monitor, _turn(usage))
        assert monitor.should_stop() is StopReason.TOKEN_BUDGET
        error = _raised(monitor)
        assert (error.budget_name, error.actual) == (name, actual)

    def test_cache_buckets_do_not_count_unless_flagged(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_input_tokens=100)), arm=True)
        _feed(monitor, _turn(TokenUsage(uncached_input_tokens=50, cache_read_input_tokens=500)))
        assert monitor.should_stop() is None
        monitor.raise_if_over_budget(iteration=1)

    def test_the_cap_is_exclusive(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_output_tokens=10)), arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=10)))
        assert monitor.should_stop() is None

    def test_an_in_flight_delta_latches_mid_turn(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_output_tokens=10)), arm=True)
        _feed(monitor, [AgentStartEvent(task_id="t"), TurnEndEvent(task_id="t", tokens=TokenUsage(output_tokens=11))])
        assert monitor.should_stop() is StopReason.TOKEN_BUDGET

    def test_a_latched_breach_is_final_even_when_the_end_usage_lands_under_the_cap(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_output_tokens=10)), arm=True)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t"),
                TurnEndEvent(task_id="t", tokens=TokenUsage(output_tokens=12)),
                AgentEndEvent(task_id="t", usage=TokenUsage(output_tokens=9)),
            ],
        )
        error = _raised(monitor)
        assert (error.budget_name, error.actual, error.limit) == ("output_tokens", 12, 10)

    def test_a_breach_seen_only_at_turn_end_is_raised_after_the_turn(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_output_tokens=10)), arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=6)) + _turn(TokenUsage(output_tokens=6)))
        assert _raised(monitor).actual == 12

    def test_the_cap_outranks_a_later_budget_breach_but_the_budget_still_raises(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_tool_calls=1, max_output_tokens=10)), arm=True)
        _feed(
            monitor,
            [AgentStartEvent(task_id="t"), _end("a"), AgentEndEvent(task_id="t", usage=TokenUsage(output_tokens=11))],
        )
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP
        assert _raised(monitor).budget_name == "output_tokens"

    def test_a_token_breach_outranks_a_usd_breach(self) -> None:
        limits = RunLimits(max_output_tokens=10, max_usd=0.01)
        monitor = TurnMonitor.for_task(_task(limits=limits), arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=11, total_cost_usd=1.0)))
        assert monitor.should_stop() is StopReason.TOKEN_BUDGET
        assert _raised(monitor).budget_name == "output_tokens"


class TestUsdBudget:
    def test_the_reported_cost_prices_the_turn(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_usd=0.10)), arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=1, total_cost_usd=0.20)))
        assert monitor.should_stop() is StopReason.USD_BUDGET
        error = _raised(monitor)
        assert error.budget_name == "usd"
        assert error.actual == pytest.approx(0.20)

    def test_the_rate_card_prices_a_turn_with_no_reported_cost(self) -> None:
        task = _task(limits=RunLimits(max_usd=0.50), model="claude-haiku-4-5")
        monitor = TurnMonitor.for_task(task, arm=True)
        _feed(monitor, _turn(TokenUsage(uncached_input_tokens=1_000_000)))
        assert monitor.cost_usd() == pytest.approx(1.0)
        assert monitor.should_stop() is StopReason.USD_BUDGET

    def test_a_reported_cost_is_enough_when_the_model_is_not_on_the_card(self) -> None:
        task = _task(limits=RunLimits(max_usd=0.10), model="openrouter/anthropic/claude-haiku-4.5")
        monitor = TurnMonitor.for_task(task, arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=100, total_cost_usd=0.02)))
        assert monitor.cost_usd() == pytest.approx(0.02)
        monitor.raise_if_over_budget(iteration=1)

    def test_turns_are_priced_on_their_own_and_summed(self) -> None:
        task = _task(limits=RunLimits(max_usd=10.0), model="claude-haiku-4-5")
        monitor = TurnMonitor.for_task(task, arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=1, total_cost_usd=0.25)))
        _feed(monitor, _turn(TokenUsage(uncached_input_tokens=1_000_000)))
        assert monitor.cost_usd() == pytest.approx(1.25)

    @pytest.mark.parametrize("model", [None, "openrouter/anthropic/claude-haiku-4.5"])
    def test_an_unpriceable_turn_makes_max_usd_unenforceable(self, model: str | None) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_usd=0.10), model=model), arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=100)))
        assert monitor.cost_usd() is None
        assert monitor.should_stop() is None
        with pytest.raises(BudgetUnenforceableError, match=r"run_limits\.max_usd could not be enforced"):
            monitor.raise_if_over_budget(iteration=1)

    def test_an_empty_crashed_attempt_costs_nothing_and_does_not_poison_the_retry(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_usd=0.10)), arm=True)
        _feed(monitor, [AgentStartEvent(task_id="t"), AgentEndEvent(task_id="t", crashed=True)])
        _feed(monitor, _turn(TokenUsage(output_tokens=10, total_cost_usd=0.01)))
        assert monitor.cost_usd() == pytest.approx(0.01)
        monitor.raise_if_over_budget(iteration=1)

    def test_the_model_the_harness_reports_prices_a_turn_when_agent_model_is_unset(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_usd=0.50)), arm=True)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t", model="claude-haiku-4-5"),
                AgentEndEvent(task_id="t", usage=TokenUsage(uncached_input_tokens=1_000_000)),
            ],
        )
        assert monitor.cost_usd() == pytest.approx(1.0)
        assert monitor.should_stop() is StopReason.USD_BUDGET

    def test_the_reported_model_prices_in_flight_deltas(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_usd=0.50)), arm=True)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t", model="claude-haiku-4-5"),
                TurnEndEvent(task_id="t", tokens=TokenUsage(uncached_input_tokens=1_000_000)),
            ],
        )
        assert monitor.should_stop() is StopReason.USD_BUDGET

    def test_a_sub_agent_model_on_the_stream_does_not_reprice_the_configured_model(self) -> None:
        task = _task(limits=RunLimits(max_usd=100.0), model="claude-sonnet-4-6")
        monitor = TurnMonitor.for_task(task, arm=True)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t", model="claude-sonnet-4-6"),
                TurnStartEvent(task_id="t", model="claude-haiku-4-5"),
                AgentEndEvent(
                    task_id="t", usage=TokenUsage(uncached_input_tokens=1_000_000), model_used="claude-haiku-4-5"
                ),
            ],
        )
        from coder_eval.pricing import calculate_cost

        assert monitor.cost_usd() == pytest.approx(calculate_cost("claude-sonnet-4-6", 1_000_000, 0))

    def test_a_reported_zero_on_a_priced_model_is_priced_from_the_rate_card(self) -> None:
        task = _task(limits=RunLimits(max_usd=0.50), model="claude-haiku-4-5")
        monitor = TurnMonitor.for_task(task, arm=True)
        _feed(monitor, _turn(TokenUsage(uncached_input_tokens=1_000_000, total_cost_usd=0.0)))
        assert monitor.cost_usd() == pytest.approx(1.0)
        assert monitor.should_stop() is StopReason.USD_BUDGET

    def test_a_reported_zero_on_an_unpriced_model_is_an_enforceable_zero(self) -> None:
        task = _task(limits=RunLimits(max_usd=0.10), model="openrouter/free/not-on-the-card")
        monitor = TurnMonitor.for_task(task, arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=100, total_cost_usd=0.0)))
        assert monitor.cost_usd() == 0.0
        monitor.raise_if_over_budget(iteration=1)

    def test_a_non_finite_reported_cost_on_an_unpriced_model_is_unpriceable(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_usd=0.10)), arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=10, total_cost_usd=float("nan"))))
        with pytest.raises(BudgetUnenforceableError):
            monitor.raise_if_over_budget(iteration=1)

    def test_an_unpriceable_turn_without_max_usd_is_fine(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_output_tokens=1000)), arm=True)
        _feed(monitor, _turn(TokenUsage(output_tokens=100)))
        monitor.raise_if_over_budget(iteration=1)

    def test_an_unpriced_in_flight_delta_contributes_nothing(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_usd=0.10)), arm=True)
        _feed(monitor, [AgentStartEvent(task_id="t"), TurnEndEvent(task_id="t", tokens=TokenUsage(output_tokens=5))])
        assert monitor.cost_usd() == 0.0
        assert monitor.should_stop() is None


class TestSubAgentScope:
    """A nested (sub-agent) event reaches the collector and the budgets, never the cap, criteria or model."""

    @staticmethod
    def _nested(event: Any) -> Any:
        return event.model_copy(update={"thread_id": "agent_1", "parent_thread_id": "agent_1"})

    def test_nested_tool_calls_do_not_reach_the_cap_but_reach_the_collector(self) -> None:
        monitor = TurnMonitor.for_task(_task(max_tool_calls=1), arm=True)
        _feed(monitor, [AgentStartEvent(task_id="t"), self._nested(_end("child"))])
        assert monitor.tool_calls == 0
        assert monitor.should_stop() is None
        assert [c.tool_id for c in monitor._collector.build_turn_record().commands] == ["child"]
        monitor.on_event(_end("main"))
        assert monitor.should_stop() is StopReason.TOOL_CALL_CAP

    def test_nested_tool_calls_do_not_reach_armed_criteria(self) -> None:
        criteria = [_skill_crit("date-teller", on_pass=True)]
        monitor = TurnMonitor.for_task(_task(criteria=criteria), arm=True)
        skill = _end("sk", tool_name="Skill", parameters={"skill": "date-teller"})
        with patch.object(TurnMonitor, "_evaluate_impl") as evaluate:
            _feed(monitor, [self._nested(ToolStartEvent(task_id="t", tool=skill.tool)), self._nested(skill)])
        evaluate.assert_not_called()
        assert monitor.should_stop() is None

    def test_a_nested_model_never_becomes_the_reported_model(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_usd=100.0)), arm=True)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t"),
                self._nested(TurnStartEvent(task_id="t", model="claude-haiku-4-5")),
                AgentEndEvent(task_id="t", usage=TokenUsage(uncached_input_tokens=1_000_000)),
            ],
        )
        assert monitor.cost_usd() is None

    def test_nested_tokens_count_toward_budgets(self) -> None:
        monitor = TurnMonitor.for_task(_task(limits=RunLimits(max_output_tokens=10)), arm=True)
        _feed(
            monitor,
            [
                AgentStartEvent(task_id="t"),
                self._nested(TurnEndEvent(task_id="t", tokens=TokenUsage(output_tokens=11))),
            ],
        )
        assert monitor.should_stop() is StopReason.TOKEN_BUDGET

    async def test_a_recovered_codex_sub_agent_tool_does_not_reach_the_cap(self, tmp_path, monkeypatch) -> None:
        """Codex's rollout recovery tags each inner call with its spawning call; the monitor counts only the spawn."""
        import json
        from datetime import datetime

        from coder_eval.agents.codex_agent import CodexAgent, _CodexDecoder
        from coder_eval.models import TimingBasis
        from coder_eval.streaming.emitter import TurnEmitter
        from coder_eval.testing import ScriptedClock

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        child = "019e0000-aaaa-7000-8000-00000000000a"
        sessions = tmp_path / "sessions" / "2026" / "09" / "16"
        sessions.mkdir(parents=True)
        (sessions / f"rollout-2026-09-16T00-00-00-{child}.jsonl").write_text(
            "\n".join(
                json.dumps({"type": "response_item", "payload": item})
                for item in (
                    {"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": '{"cmd":"ls"}'},
                    {"type": "function_call_output", "call_id": "c1", "output": "a"},
                    {"type": "function_call", "name": "exec_command", "call_id": "c2", "arguments": '{"cmd":"pwd"}'},
                    {"type": "function_call_output", "call_id": "c2", "output": "/"},
                )
            )
        )
        monitor = TurnMonitor.for_task(_task(max_tool_calls=2), arm=True)
        emitter = TurnEmitter(
            task_id="t",
            iteration=1,
            prompt="go",
            model="gpt-5.5",
            basis=TimingBasis.CLI_EPOCH_MS,
            clock=ScriptedClock(datetime(2026, 9, 16)),
            sinks=[monitor],
        )
        emitter.begin()
        emitter.open_tool("call_spawn", "Agent", {}, started_at=None)
        emitter.close_tool("call_spawn", status=ToolEndStatus.OK, completed_at=None)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5.5"))
        decoder = _CodexDecoder(agent, emitter, turn_id="codex-1")
        decoder.spawned_children = [(child, "call_spawn", None)]

        await agent._recover_subagent_tool_calls(decoder)

        assert monitor.tool_calls == 1
        assert monitor.should_stop() is None
        nested = {c.tool_id for c in monitor._collector.build_turn_record().commands} - {"call_spawn"}
        assert nested == {f"sub:{child}:c1", f"sub:{child}:c2"}
