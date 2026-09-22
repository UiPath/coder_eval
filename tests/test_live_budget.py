"""Tests for the mid-turn budget stop."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors import BudgetExceededError
from coder_eval.models import (
    AgentKind,
    ClaudeCodeAgentConfig,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
)
from coder_eval.orchestration.live_budget import LiveBudget
from coder_eval.orchestrator import Orchestrator
from coder_eval.streaming.events import AgentStartEvent, TurnEndEvent


def _turn_end(turn_id: str, *, uncached: int = 0, output: int = 0, cost: float | None = None) -> TurnEndEvent:
    tokens = TokenUsage(uncached_input_tokens=uncached, output_tokens=output, total_cost_usd=cost)
    return TurnEndEvent(task_id="t", turn_id=turn_id, tokens=tokens)


class TestLiveBudget:
    def test_none_without_a_budget_cap(self):
        assert LiveBudget.for_limits(None, list) is None
        assert LiveBudget.for_limits(RunLimits(max_turns=5, turn_timeout=60), list) is None

    def test_trips_when_the_turn_in_flight_crosses_the_cap(self):
        budget = LiveBudget(RunLimits(max_total_tokens=1_000), lambda: [TokenUsage(uncached_input_tokens=600)])
        budget.on_event(_turn_end("a", uncached=300))
        assert not budget.should_stop()
        budget.on_event(_turn_end("b", output=200))
        assert budget.should_stop()
        assert budget.breach == ("total_tokens", 1_100, 1_000)

    def test_a_turn_that_ends_twice_counts_once(self):
        budget = LiveBudget(RunLimits(max_output_tokens=150), list)
        budget.on_event(_turn_end("a", output=100))
        budget.on_event(_turn_end("a", output=120))
        assert not budget.should_stop()

    def test_agent_start_resets_the_turn_in_flight(self):
        budget = LiveBudget(RunLimits(max_output_tokens=150), list)
        budget.on_event(_turn_end("a", output=100))
        budget.on_event(AgentStartEvent(task_id="t", model="claude-sonnet-5"))
        budget.on_event(_turn_end("b", output=100))
        assert not budget.should_stop()

    def test_prices_tokens_from_the_rate_card(self):
        budget = LiveBudget(RunLimits(max_usd=1.0), list)
        budget.on_event(AgentStartEvent(task_id="t", model="claude-sonnet-5"))
        budget.on_event(_turn_end("a", output=200_000))
        assert budget.breach is not None
        assert budget.breach[0] == "usd"

    def test_an_unpriced_model_adds_no_cost(self):
        budget = LiveBudget(RunLimits(max_usd=1.0), list)
        budget.on_event(AgentStartEvent(task_id="t", model="not-a-real-model"))
        budget.on_event(_turn_end("a", output=10_000_000))
        assert not budget.should_stop()


class _StreamingTurn:
    """A cooperative turn that streams one TurnEndEvent per API call until told to stop."""

    def __init__(self) -> None:
        self.calls = 0

    async def communicate(self, prompt, *, stream_callback, timeout, max_turns, should_stop):
        stream_callback.on_event(AgentStartEvent(task_id="t", model="claude-sonnet-5"))
        for n in range(100):
            self.calls += 1
            stream_callback.on_event(_turn_end(f"call-{n}", uncached=1_000, output=1_000))
            if should_stop is not None and should_stop():
                break
        usage = TokenUsage(uncached_input_tokens=1_000 * self.calls, output_tokens=1_000 * self.calls)
        return TurnRecord(iteration=1, user_input=prompt, agent_output="", duration_seconds=1.0, token_usage=usage)


async def test_orchestrator_stops_a_turn_mid_flight_on_a_token_budget(tmp_path):
    agent_config = ClaudeCodeAgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits", allowed_tools=None, model=None, ignore_patterns=[]
    )
    task = TaskDefinition.model_construct(
        task_id="live_budget",
        description="t",
        initial_prompt="do something",
        tags=[],
        agent=agent_config,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x", description="x")],
        run_limits=RunLimits(max_total_tokens=10_000),
        reference=None,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    orch.result = EvaluationResult(
        task_id="live_budget",
        task_description="t",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )
    orch.sandbox = MagicMock()
    orch.sandbox.sandbox_dir = tmp_path
    turn = _StreamingTurn()
    orch.agent = AsyncMock(supports_cooperative_stop=True, pending_turn=None, communicate=turn.communicate)
    orch.success_checker = MagicMock()
    orch.success_checker.check_all_async = AsyncMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
    )

    with (
        patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
        pytest.raises(BudgetExceededError) as exc,
    ):
        await orch._evaluation_loop()

    assert turn.calls == 6
    assert exc.value.budget_name == "total_tokens"
