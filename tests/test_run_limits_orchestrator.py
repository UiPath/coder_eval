"""Tests for RunLimits enforcement in the orchestrator."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors import BudgetExceededError
from coder_eval.models import (
    AgentConfig,
    AgentKind,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
)
from coder_eval.orchestrator import Orchestrator


def _make_task(*, run_limits: RunLimits | None = None) -> TaskDefinition:
    agent = AgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        ignore_patterns=[],
    )
    return TaskDefinition.model_construct(
        task_id="budget_test",
        description="Test budget",
        initial_prompt="do something",
        tags=[],
        agent=agent,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x", description="x must exist")],
        run_limits=run_limits,
        reference=None,
    )


def _make_turn(
    *,
    iteration: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    total_cost_usd: float | None = None,
) -> TurnRecord:
    return TurnRecord(
        iteration=iteration,
        user_input="p",
        agent_output="done",
        duration_seconds=1.0,
        token_usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cache_read_input_tokens,
            total_cost_usd=total_cost_usd,
        ),
    )


def _make_orchestrator(task: TaskDefinition, tmp_path) -> Orchestrator:
    run_dir = tmp_path / "run" / "budget_test"
    run_dir.mkdir(parents=True)
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    orchestrator.result = EvaluationResult(
        task_id="budget_test",
        task_description="t",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )
    sandbox = MagicMock()
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = sandbox
    orchestrator.success_checker = MagicMock()
    return orchestrator


class TestCheckRunLimitsUnit:
    """Direct tests of the _check_run_limits helper."""

    def test_noop_when_no_limits(self, tmp_path):
        orch = _make_orchestrator(_make_task(), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=100))
        # Should not raise.
        orch._check_run_limits(iteration=1)

    def test_noop_when_no_turns(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_total_tokens=1)), tmp_path)
        # No turns recorded yet — no usage to check.
        orch._check_run_limits(iteration=0)

    def test_input_token_trip(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_input_tokens=1000)), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=2000))
        with pytest.raises(BudgetExceededError) as exc:
            orch._check_run_limits(iteration=1)
        assert exc.value.budget_name == "input_tokens"
        assert exc.value.actual == 2000
        assert exc.value.limit == 1000

    def test_output_token_trip(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_output_tokens=1000)), tmp_path)
        orch.result.turns.append(_make_turn(output_tokens=2000))
        with pytest.raises(BudgetExceededError) as exc:
            orch._check_run_limits(iteration=1)
        assert exc.value.budget_name == "output_tokens"

    def test_total_token_trip(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_total_tokens=2500)), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=1500, output_tokens=1500))
        with pytest.raises(BudgetExceededError) as exc:
            orch._check_run_limits(iteration=1)
        assert exc.value.budget_name == "total_tokens"

    def test_cost_trip(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_usd=0.10)), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=10, total_cost_usd=0.20))
        with pytest.raises(BudgetExceededError) as exc:
            orch._check_run_limits(iteration=1)
        assert exc.value.budget_name == "usd"
        assert exc.value.actual == pytest.approx(0.20)

    def test_cost_skipped_when_no_cost_reported(self, tmp_path, caplog):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_usd=0.10)), tmp_path)
        # turn has token_usage but no total_cost_usd
        orch.result.turns.append(_make_turn(input_tokens=10, total_cost_usd=None))
        with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
            orch._check_run_limits(iteration=1)
        assert any("max_usd budget configured but no turn reported cost" in m for m in caplog.messages)

    def test_cost_warning_only_once(self, tmp_path, caplog):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_usd=0.10)), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=10, total_cost_usd=None))
        with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
            orch._check_run_limits(iteration=1)
            orch._check_run_limits(iteration=2)
        warns = [m for m in caplog.messages if "max_usd budget configured" in m]
        assert len(warns) == 1

    def test_count_cached_input_false_default(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_input_tokens=1000)), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=500, cache_read_input_tokens=600))
        # 500 < 1000 — cache reads don't count by default.
        orch._check_run_limits(iteration=1)

    def test_count_cached_input_true(self, tmp_path):
        orch = _make_orchestrator(
            _make_task(run_limits=RunLimits(max_input_tokens=1000, count_cached_input=True)), tmp_path
        )
        orch.result.turns.append(_make_turn(input_tokens=500, cache_read_input_tokens=600))
        with pytest.raises(BudgetExceededError) as exc:
            orch._check_run_limits(iteration=1)
        assert exc.value.actual == 1100

    def test_cumulative_across_turns(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_input_tokens=1000)), tmp_path)
        orch.result.turns.append(_make_turn(iteration=1, input_tokens=600))
        orch.result.turns.append(_make_turn(iteration=2, input_tokens=500))
        with pytest.raises(BudgetExceededError):
            orch._check_run_limits(iteration=2)


async def _run_orchestrator(
    task: TaskDefinition, tmp_path, *, raising_error: BudgetExceededError | None = None
) -> EvaluationResult:
    """Drive Orchestrator.run() with mocked _setup/_cleanup and a stub eval loop.

    When ``raising_error`` is set, the stubbed ``_evaluation_loop`` raises it —
    this exercises the real ``except BudgetExceededError`` arm in run().
    """
    run_dir = tmp_path / "run" / "budget_test"
    run_dir.mkdir(parents=True, exist_ok=True)
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    orch._setup = AsyncMock()  # type: ignore[method-assign]
    orch._cleanup = AsyncMock()  # type: ignore[method-assign]

    async def loop():
        if raising_error is not None:
            raise raising_error
        return True

    orch._evaluation_loop = loop  # type: ignore[method-assign]
    return await orch.run()


@pytest.mark.asyncio
class TestSingleShotEnforcement:
    """End-to-end orchestrator path: single-shot loop + run() exception arm."""

    async def _run_eval_loop_with_turn(self, task: TaskDefinition, tmp_path, turn: TurnRecord) -> EvaluationResult:
        orch = _make_orchestrator(task, tmp_path)
        mock_agent = AsyncMock()
        mock_agent.communicate = AsyncMock(return_value=turn)
        orch.agent = mock_agent

        mock_checker = MagicMock()
        mock_checker.check_all = MagicMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
        )
        orch.success_checker = mock_checker

        with (
            patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)),
            contextlib.suppress(BudgetExceededError),
        ):
            await orch._evaluation_loop()
        assert orch.result is not None
        return orch.result

    async def test_under_budget_passes(self, tmp_path):
        task = _make_task(run_limits=RunLimits(max_total_tokens=10_000))
        turn = _make_turn(input_tokens=100, output_tokens=100, total_cost_usd=0.001)
        result = await self._run_eval_loop_with_turn(task, tmp_path, turn)
        assert len(result.success_criteria_results) == 1

    async def test_input_budget_trip_records_criteria(self, tmp_path):
        task = _make_task(run_limits=RunLimits(max_input_tokens=10))
        turn = _make_turn(input_tokens=200)
        result = await self._run_eval_loop_with_turn(task, tmp_path, turn)
        # Criteria still ran before budget check (single-shot order).
        assert len(result.success_criteria_results) == 1

    @pytest.mark.parametrize(
        "budget_name,expected_status,expected_component",
        [
            ("input_tokens", FinalStatus.TOKEN_BUDGET_EXCEEDED, "orchestrator.run_limits.tokens"),
            ("output_tokens", FinalStatus.TOKEN_BUDGET_EXCEEDED, "orchestrator.run_limits.tokens"),
            ("total_tokens", FinalStatus.TOKEN_BUDGET_EXCEEDED, "orchestrator.run_limits.tokens"),
            ("usd", FinalStatus.COST_BUDGET_EXCEEDED, "orchestrator.run_limits.cost"),
        ],
    )
    async def test_run_arm_maps_budget_to_status(
        self, tmp_path, budget_name: str, expected_status: FinalStatus, expected_component: str
    ):
        """Drive the real ``except BudgetExceededError`` arm in Orchestrator.run().

        Guards against regressions like flipping the if/else or adding a typo'd
        budget_name that would silently fall through the wrong branch.
        """
        from unittest.mock import patch as _patch

        task = _make_task(run_limits=RunLimits(max_input_tokens=10))
        err = BudgetExceededError(budget_name, actual=100, limit=10, task_id=task.task_id, iteration=1)
        with _patch("coder_eval.orchestrator.create_error_context") as mock_ctx:
            mock_ctx.return_value = {}
            result = await _run_orchestrator(task, tmp_path, raising_error=err)

        assert result.final_status == expected_status
        assert "budget exceeded" in (result.error_message or "")
        # Captured error_log_tail key allowlist must include both new statuses.
        assert result.error_details == {}
        # Inspect the actual create_error_context call to confirm the component label.
        assert mock_ctx.call_args.kwargs["component"] == expected_component


class TestCostDataAvailableFlag:
    """The cost_data_available flag is set on result.environment_info in _finalize_result."""

    @staticmethod
    def _invoke_finalize(orch: Orchestrator) -> None:
        """Run _finalize_result with side-effecting persistence (report writes) patched out.

        Both ``write_task_html`` and ``spill_judge_transcripts`` are imported lazily
        inside ``_finalize_result``, so they must be patched on their defining
        modules rather than on ``coder_eval.orchestrator``.
        """
        import time as _time
        from unittest.mock import patch as _patch

        # The report_path lives under tmp_path so the write_text call lands
        # in a real (test-scoped) file and we don't need to mock pathlib.
        with (
            _patch("coder_eval.reports_html.write_task_html", return_value=None),
            _patch("coder_eval.evaluation.judge_persistence.spill_judge_transcripts", return_value=None),
        ):
            orch._finalize_result(_time.time())

    def test_flag_true_when_costs_reported(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_usd=0.5)), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=10, total_cost_usd=0.001))
        self._invoke_finalize(orch)
        assert orch.result.environment_info["cost_data_available"] is True

    def test_flag_false_when_no_cost_reported(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_usd=0.5)), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=10, total_cost_usd=None))
        self._invoke_finalize(orch)
        assert orch.result.environment_info["cost_data_available"] is False

    def test_flag_absent_when_no_max_usd_budget(self, tmp_path):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_total_tokens=1000)), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=10, total_cost_usd=0.001))
        self._invoke_finalize(orch)
        assert "cost_data_available" not in orch.result.environment_info

    def test_flag_absent_when_no_run_limits(self, tmp_path):
        orch = _make_orchestrator(_make_task(), tmp_path)
        orch.result.turns.append(_make_turn(input_tokens=10, total_cost_usd=0.001))
        self._invoke_finalize(orch)
        assert "cost_data_available" not in orch.result.environment_info


@pytest.mark.asyncio
class TestSimulationBudgetAbort:
    """The simulation arm raises BudgetExceededError mid-dialog and records telemetry."""

    async def test_dialog_aborts_with_run_limit_stop_reason(self, tmp_path):
        """A budget breach in _simulation_dialog_loop must:
        - raise BudgetExceededError to the caller (Orchestrator.run),
        - set stop_reason=RUN_LIMIT_EXCEEDED in the finally block's SimulationTelemetry,
        - run end-of-dialog criteria for partial credit.
        """
        from coder_eval.models import SimulationConfig

        sim = SimulationConfig(
            enabled=True,
            persona="user",
            goal="get the agent to do x",
            max_turns=5,
            check_criteria="end_of_dialog",
        )
        task = _make_task(run_limits=RunLimits(max_input_tokens=100))
        task = task.model_copy(update={"simulation": sim, "initial_prompt": "first message"})

        orch = _make_orchestrator(task, tmp_path)
        # The agent's first turn reports tokens above the budget.
        turn = _make_turn(input_tokens=200, output_tokens=10)
        mock_agent = AsyncMock()
        mock_agent.communicate = AsyncMock(return_value=turn)
        orch.agent = mock_agent

        mock_checker = MagicMock()
        mock_checker.check_all = MagicMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=0.0)]
        )
        orch.success_checker = mock_checker

        # The UserSimulator must NOT be reached after the budget trip — we
        # configure it but it should not produce another user message.
        mock_simulator = MagicMock()
        mock_simulator.start = AsyncMock()
        mock_simulator.stop = AsyncMock()
        mock_simulator.next_user_message = AsyncMock()

        with (
            patch("coder_eval.orchestrator.UserSimulator", return_value=mock_simulator),
            patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)),
            pytest.raises(BudgetExceededError),
        ):
            await orch._simulation_dialog_loop("first message", tmp_path / "sandbox")

        # End-of-dialog criteria must have run for partial credit before the raise.
        assert len(orch.result.success_criteria_results) == 1
        # Finally block must have written telemetry with the dedicated stop_reason.
        assert orch.result.simulation is not None
        assert orch.result.simulation.stop_reason == "run_limit_exceeded"
        assert orch.result.simulation.total_turns == 1
        # Simulator must not have been asked for another message after the budget trip.
        mock_simulator.next_user_message.assert_not_called()
