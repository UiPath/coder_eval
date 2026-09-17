"""Tests for RunLimits enforcement in the orchestrator."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors import BudgetExceededError, BudgetUnenforceableError
from coder_eval.models import (
    DEFAULT_SIMULATOR_MODEL,
    AgentKind,
    ClaudeCodeAgentConfig,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    RunLimits,
    SandboxConfig,
    SimulationTelemetry,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
)
from coder_eval.orchestrator import Orchestrator
from coder_eval.streaming.emitter import TurnOutcome
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, AgentStartEvent


def _make_task(*, run_limits: RunLimits | None = None) -> TaskDefinition:
    agent = ClaudeCodeAgentConfig.model_construct(
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
    commands: int = 0,
    reply: str | None = None,
) -> TurnRecord:
    from coder_eval.models import CommandTelemetry, ResultSummary

    return TurnRecord(
        iteration=iteration,
        user_input="p",
        agent_output="done",
        duration_seconds=1.0,
        token_usage=TokenUsage(
            uncached_input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cache_read_input_tokens,
            total_cost_usd=total_cost_usd,
        ),
        commands=[
            CommandTelemetry(
                tool_name="Bash",
                tool_id=f"t{iteration}-{i}",
                timestamp=datetime.now(),
            )
            for i in range(commands)
        ],
        result_summary=(ResultSummary(is_error=False, subtype="success", result=reply) if reply is not None else None),
    )


def _reporting_agent(*turns: TurnRecord) -> AsyncMock:
    """A fake agent whose each ``communicate`` reports its turn's usage on the stream, as real agents do."""
    remaining = list(turns)

    async def communicate(user_input, *, iteration, stream_callback=None, timeout=None, should_stop=None):
        turn = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        assert stream_callback is not None
        stream_callback.on_event(AgentStartEvent(task_id="budget_test", prompt=user_input))
        stream_callback.on_event(AgentEndEvent(task_id="budget_test", usage=turn.token_usage or TokenUsage()))
        return TurnOutcome(record=turn, status=AgentEndStatus.COMPLETED, error=None)

    agent = AsyncMock()
    agent.communicate = communicate
    agent.get_sdk_options = MagicMock(return_value={})
    agent.get_environment_info = MagicMock(return_value={})
    return agent


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
    orchestrator._build_monitor()
    return orchestrator


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
        orch.agent = _reporting_agent(turn)

        mock_checker = MagicMock()
        mock_checker.check_all_async = AsyncMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
        )
        orch.success_checker = mock_checker

        with (
            patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
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
        orch = _make_orchestrator(task, tmp_path)
        orch.agent = _reporting_agent(_make_turn(input_tokens=200))
        orch.success_checker.check_all_async = AsyncMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
        )
        with (
            patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
            pytest.raises(BudgetExceededError) as exc,
        ):
            await orch._evaluation_loop()
        assert (exc.value.budget_name, exc.value.actual, exc.value.limit) == ("input_tokens", 200, 10)
        # Criteria still ran before the budget check (single-shot order).
        assert len(orch.result.success_criteria_results) == 1

    @pytest.mark.parametrize(
        ("limits", "turn", "budget_name"),
        [
            (RunLimits(max_output_tokens=10), {"output_tokens": 20}, "output_tokens"),
            (RunLimits(max_total_tokens=25), {"input_tokens": 15, "output_tokens": 15}, "total_tokens"),
            (RunLimits(max_usd=0.10), {"input_tokens": 10, "total_cost_usd": 0.20}, "usd"),
        ],
    )
    async def test_each_budget_trips_through_the_monitor(self, tmp_path, limits, turn, budget_name):
        orch = _make_orchestrator(_make_task(run_limits=limits), tmp_path)
        orch.agent = _reporting_agent(_make_turn(**turn))
        orch.success_checker.check_all_async = AsyncMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
        )
        with (
            patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
            pytest.raises(BudgetExceededError) as exc,
        ):
            await orch._evaluation_loop()
        assert exc.value.budget_name == budget_name

    async def test_an_unpriceable_max_usd_finalizes_error_after_the_first_turn(self, tmp_path):
        task = _make_task(run_limits=RunLimits(max_usd=0.10))
        run_dir = tmp_path / "run" / "unpriceable"
        run_dir.mkdir(parents=True)
        orch = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
        agent = _reporting_agent(_make_turn(input_tokens=10, total_cost_usd=None))

        async def setup() -> None:
            orch._build_monitor()
            orch.sandbox = MagicMock()
            orch.sandbox.sandbox_dir = tmp_path / "sandbox"
            orch.sandbox.sandbox_dir.mkdir()
            orch.agent = agent
            orch.success_checker = MagicMock()
            orch.success_checker.check_all_async = AsyncMock(
                return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
            )

        orch._setup = setup  # type: ignore[method-assign]
        orch._cleanup = AsyncMock()  # type: ignore[method-assign]
        with patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None):
            result = await orch.run()

        assert result.final_status == FinalStatus.ERROR
        assert "run_limits.max_usd could not be enforced" in (result.error_message or "")
        assert "agent.model None" in (result.error_message or "")
        assert result.iteration_count == 1

    async def test_complete_canonical_results_skip_post_failure_regrade(self, tmp_path):
        task = _make_task(run_limits=RunLimits(max_input_tokens=10))
        run_dir = tmp_path / "run" / "complete_budget_result"
        run_dir.mkdir(parents=True)
        orch = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
        orch._setup = AsyncMock()  # type: ignore[method-assign]
        orch._cleanup = AsyncMock()  # type: ignore[method-assign]
        orch._refresh_runtime_tool_versions = MagicMock()  # type: ignore[method-assign]
        err = BudgetExceededError("input_tokens", actual=100, limit=10, task_id=task.task_id, iteration=1)

        async def loop() -> bool:
            assert orch.result is not None
            orch.result.success_criteria_results = [
                CriterionResult(criterion_type="file_exists", description="x", score=1.0)
            ]
            raise err

        orch._evaluation_loop = loop  # type: ignore[method-assign]
        result = await orch.run()

        assert result.final_status == FinalStatus.TOKEN_BUDGET_EXCEEDED
        assert len(result.success_criteria_results) == 1
        assert result.post_failure_criteria_results == []

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
        assert len(result.post_failure_criteria_results) == 1
        assert result.post_failure_criteria_results[0].evaluation_status == "not_evaluated"
        # Inspect the actual create_error_context call to confirm the component label.
        assert mock_ctx.call_args.kwargs["component"] == expected_component


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
        orch.agent = _reporting_agent(_make_turn(input_tokens=200, output_tokens=10))

        mock_checker = MagicMock()
        mock_checker.check_all_async = AsyncMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=0.0)]
        )
        orch.success_checker = mock_checker

        # The UserSimulator must NOT be reached after the budget trip — we
        # configure it but it should not produce another user message.
        mock_simulator = MagicMock()
        # UserSimulator.model is a real str property (the pinned simulator model);
        # an auto-specced MagicMock here fails SimulationTelemetry validation.
        mock_simulator.model = DEFAULT_SIMULATOR_MODEL
        mock_simulator.start = AsyncMock()
        mock_simulator.stop = AsyncMock()
        mock_simulator.next_user_message = AsyncMock()

        with (
            patch("coder_eval.orchestrator.UserSimulator", return_value=mock_simulator),
            patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
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

    async def test_an_unpriceable_max_usd_ends_the_dialog_after_its_first_turn(self, tmp_path):
        from coder_eval.models import SimulationConfig

        sim = SimulationConfig(enabled=True, persona="user", goal="g", max_turns=5, check_criteria="end_of_dialog")
        task = _make_task(run_limits=RunLimits(max_usd=0.10)).model_copy(
            update={"simulation": sim, "initial_prompt": "first message"}
        )
        orch = _make_orchestrator(task, tmp_path)
        orch.agent = _reporting_agent(_make_turn(input_tokens=200, output_tokens=10))
        orch.success_checker.check_all_async = AsyncMock(return_value=[])
        mock_simulator = MagicMock()
        mock_simulator.model = DEFAULT_SIMULATOR_MODEL
        mock_simulator.start = AsyncMock()
        mock_simulator.stop = AsyncMock()
        mock_simulator.next_user_message = AsyncMock()

        with (
            patch("coder_eval.orchestrator.UserSimulator", return_value=mock_simulator),
            patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
            pytest.raises(BudgetUnenforceableError, match="could not be enforced"),
        ):
            await orch._simulation_dialog_loop("first message", tmp_path / "sandbox")

        assert orch.result.simulation is not None
        assert orch.result.simulation.total_turns == 1
        mock_simulator.next_user_message.assert_not_called()


class TestCheckExpectedTurnsUnit:
    """Direct unit tests of Orchestrator._check_expected_tool_calls."""

    def test_noop_when_run_limits_is_none(self, tmp_path, caplog):
        orch = _make_orchestrator(_make_task(), tmp_path)
        orch.result.iterations.append(_make_turn(commands=100))
        with caplog.at_level(logging.WARNING):
            orch._check_expected_tool_calls(iteration=1)
        assert "expected_tool_calls" not in caplog.text.lower()
        assert orch._expected_tool_calls_warning_emitted is False

    def test_noop_when_expected_tool_calls_unset(self, tmp_path, caplog):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(max_tool_calls=10)), tmp_path)
        orch.result.iterations.append(_make_turn(commands=20))
        with caplog.at_level(logging.WARNING):
            orch._check_expected_tool_calls(iteration=1)
        assert "expected_tool_calls" not in caplog.text.lower()
        assert orch._expected_tool_calls_warning_emitted is False

    def test_no_warning_at_exact_equal(self, tmp_path, caplog):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(expected_tool_calls=6)), tmp_path)
        # 5 tools + reply = 6 visible turns; equal → no warning.
        orch.result.iterations.append(_make_turn(iteration=1, commands=3))
        orch.result.iterations.append(_make_turn(iteration=2, commands=2, reply="done"))
        with caplog.at_level(logging.WARNING):
            orch._check_expected_tool_calls(iteration=2)
        assert "Visible turns" not in caplog.text
        assert orch._expected_tool_calls_warning_emitted is False

    def test_warning_fires_once_when_exceeded(self, tmp_path, caplog):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(expected_tool_calls=5)), tmp_path)
        # 2 + 2 = 4 visible turns, still under 5.
        orch.result.iterations.append(_make_turn(iteration=1, commands=2))
        orch.result.iterations.append(_make_turn(iteration=2, commands=2))
        with caplog.at_level(logging.WARNING):
            orch._check_expected_tool_calls(iteration=2)
        assert "Visible turns" not in caplog.text

        # +3 tools = 7 visible turns, over 5 → fires.
        orch.result.iterations.append(_make_turn(iteration=3, commands=3))
        with caplog.at_level(logging.WARNING):
            orch._check_expected_tool_calls(iteration=3)
        assert "Visible tool calls (7) exceeded expected_tool_calls (5)" in caplog.text
        assert orch._expected_tool_calls_warning_emitted is True

        # Re-firing on a later iteration is a no-op.
        caplog.clear()
        orch.result.iterations.append(_make_turn(iteration=4, commands=5))
        with caplog.at_level(logging.WARNING):
            orch._check_expected_tool_calls(iteration=4)
        assert "Visible turns" not in caplog.text

    def test_warning_counts_reply_as_one(self, tmp_path, caplog):
        """A pure-text iteration (0 tools, just a reply) contributes 1 visible turn."""
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(expected_tool_calls=3)), tmp_path)
        # 2 tools, then a 2-tool turn that also emits a final reply.
        # Visible: 2 + 2 + 1(reply) = 5. Crosses 3 → warns.
        orch.result.iterations.append(_make_turn(iteration=1, commands=2))
        orch.result.iterations.append(_make_turn(iteration=2, commands=2, reply="ok"))
        with caplog.at_level(logging.WARNING):
            orch._check_expected_tool_calls(iteration=2)
        assert "Visible tool calls (5) exceeded expected_tool_calls (3)" in caplog.text

    def test_noop_when_result_is_none(self, tmp_path, caplog):
        orch = _make_orchestrator(_make_task(run_limits=RunLimits(expected_tool_calls=1)), tmp_path)
        orch.result = None
        with caplog.at_level(logging.WARNING):
            orch._check_expected_tool_calls(iteration=1)
        assert "Visible turns" not in caplog.text


@pytest.mark.asyncio
class TestExpectedTurnsSingleShot:
    """Drive the real _evaluation_loop and assert expected_tool_calls warning never aborts."""

    async def test_warning_does_not_abort_run(self, tmp_path, caplog):
        task = _make_task(run_limits=RunLimits(expected_tool_calls=2))
        # 4 tools + reply = 5 visible turns, exceeds 2.
        turn = _make_turn(iteration=1, commands=4, reply="done")

        orch = _make_orchestrator(task, tmp_path)
        mock_agent = AsyncMock()
        mock_agent.communicate = AsyncMock(
            return_value=TurnOutcome(record=turn, status=AgentEndStatus.COMPLETED, error=None)
        )
        orch.agent = mock_agent
        mock_checker = MagicMock()
        mock_checker.check_all_async = AsyncMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
        )
        orch.success_checker = mock_checker

        with (
            patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
            caplog.at_level(logging.WARNING),
        ):
            all_passed = await orch._evaluation_loop()

        assert all_passed is True
        assert orch._expected_tool_calls_warning_emitted is True
        assert "Visible tool calls (5) exceeded expected_tool_calls (2)" in caplog.text


@pytest.mark.asyncio
class TestExpectedTurnsSimulation:
    """Drive the simulation dialog loop and confirm expected_tool_calls fires once."""

    async def test_warning_fires_in_simulation_and_does_not_abort(self, tmp_path, caplog):
        """A simulation turn that trips the soft target logs once; the dialog
        continues until the simulator decides to stop. The warning must fire
        before the tool_calls_exhausted break so a turn that trips both still
        emits the soft-target signal."""
        from coder_eval.models import SimulationConfig
        from coder_eval.simulation.user_simulator import SimulatorResult

        sim = SimulationConfig(
            enabled=True,
            persona="user",
            goal="get the agent to do x",
            max_turns=5,
            check_criteria="end_of_dialog",
        )
        task = _make_task(run_limits=RunLimits(expected_tool_calls=3))
        task = task.model_copy(update={"simulation": sim, "initial_prompt": "first message"})

        orch = _make_orchestrator(task, tmp_path)
        # Each agent turn = 1 tool call → cumulative still under 3 after one turn.
        turn = _make_turn(commands=1)
        mock_agent = AsyncMock()
        mock_agent.communicate = AsyncMock(
            return_value=TurnOutcome(record=turn, status=AgentEndStatus.COMPLETED, error=None)
        )
        orch.agent = mock_agent

        mock_checker = MagicMock()
        mock_checker.check_all_async = AsyncMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
        )
        orch.success_checker = mock_checker

        # Simulator emits the stop token on the second prompt so the dialog
        # terminates cleanly after the warning has fired.
        mock_simulator = MagicMock()
        # UserSimulator.model is a real str property (the pinned simulator model);
        # an auto-specced MagicMock here fails SimulationTelemetry validation.
        mock_simulator.model = DEFAULT_SIMULATOR_MODEL
        mock_simulator.start = AsyncMock()
        mock_simulator.stop = AsyncMock()
        mock_simulator.next_user_message = AsyncMock(
            return_value=SimulatorResult(text="ok", raw_text="ok", stop_requested=True, input_tokens=0, output_tokens=0)
        )

        with (
            patch("coder_eval.orchestrator.UserSimulator", return_value=mock_simulator),
            patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
            caplog.at_level(logging.WARNING),
        ):
            await orch._simulation_dialog_loop("first message", tmp_path / "sandbox")

        # The simulator-driven loop only sends one agent turn before hitting
        # the stop token, so cumulative is 1 (not over 3). Confirm no warning.
        assert "Visible turns" not in caplog.text
        assert orch._expected_tool_calls_warning_emitted is False
        # Run completed cleanly.
        assert orch.result.simulation is not None
        assert orch.result.simulation.stop_reason == "stop_token"

    async def test_warning_fires_when_single_simulation_turn_exceeds(self, tmp_path, caplog):
        """A first agent turn whose own num_turns already exceeds the soft
        target trips the warning before the simulator is asked for a follow-up."""
        from coder_eval.models import SimulationConfig
        from coder_eval.simulation.user_simulator import SimulatorResult

        sim = SimulationConfig(
            enabled=True,
            persona="user",
            goal="get the agent to do x",
            max_turns=5,
            check_criteria="end_of_dialog",
        )
        task = _make_task(run_limits=RunLimits(expected_tool_calls=2))
        task = task.model_copy(update={"simulation": sim, "initial_prompt": "first message"})

        orch = _make_orchestrator(task, tmp_path)
        # 4 tools + reply = 5 visible turns, exceeds 2.
        turn = _make_turn(commands=4, reply="done")
        mock_agent = AsyncMock()
        mock_agent.communicate = AsyncMock(
            return_value=TurnOutcome(record=turn, status=AgentEndStatus.COMPLETED, error=None)
        )
        orch.agent = mock_agent

        mock_checker = MagicMock()
        mock_checker.check_all_async = AsyncMock(
            return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
        )
        orch.success_checker = mock_checker

        mock_simulator = MagicMock()
        # UserSimulator.model is a real str property (the pinned simulator model);
        # an auto-specced MagicMock here fails SimulationTelemetry validation.
        mock_simulator.model = DEFAULT_SIMULATOR_MODEL
        mock_simulator.start = AsyncMock()
        mock_simulator.stop = AsyncMock()
        mock_simulator.next_user_message = AsyncMock(
            return_value=SimulatorResult(text="ok", raw_text="ok", stop_requested=True, input_tokens=0, output_tokens=0)
        )

        with (
            patch("coder_eval.orchestrator.UserSimulator", return_value=mock_simulator),
            patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
            caplog.at_level(logging.WARNING),
        ):
            await orch._simulation_dialog_loop("first message", tmp_path / "sandbox")

        assert "Visible tool calls (5) exceeded expected_tool_calls (2)" in caplog.text
        assert orch._expected_tool_calls_warning_emitted is True


class TestBuildSimulationTelemetry:
    """Direct field-mapping tests for the _build_simulation_telemetry SSOT builder."""

    def test_maps_all_fields(self, tmp_path):
        from coder_eval.simulation import DialogStopReason

        orch = _make_orchestrator(_make_task(), tmp_path)
        orch.replicate_index = 3

        telemetry = orch._build_simulation_telemetry(
            n_trials=5,
            stop_reason=DialogStopReason.STOP_TOKEN,
            total_turns=4,
            sim_in=120,
            sim_out=34,
            sim_failures=1,
        )

        assert isinstance(telemetry, SimulationTelemetry)
        assert telemetry.n_trials == 5
        assert telemetry.replicate_index == 3  # pulled from the orchestrator, not a param
        assert telemetry.stop_reason == "stop_token"
        assert telemetry.total_turns == 4
        assert telemetry.simulator_input_tokens == 120
        assert telemetry.simulator_output_tokens == 34
        assert telemetry.simulator_failures == 1
