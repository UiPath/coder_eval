"""Tests for timeout behavior in the orchestrator."""

import asyncio
import contextlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors import AgentCrashError, CheckerMisuseError, JudgeInfrastructureError
from coder_eval.errors.timeout import TaskTimeoutError, TurnTimeoutError
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    AgentKind,
    ClaudeCodeAgentConfig,
    CommandExecutedCriterion,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    LLMJudgeCriterion,
    RunCommandCriterion,
    SandboxConfig,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
)
from coder_eval.orchestrator import Orchestrator
from coder_eval.sandbox import Sandbox
from coder_eval.streaming.emitter import TurnOutcome
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, AgentStartEvent


def _make_task(*, turn_timeout: float | None = None, task_timeout: float | None = None):
    """Create a minimal TaskDefinition for testing.

    Uses model_construct() to bypass Pydantic's ge= validators when setting
    sub-minimum timeout values needed for fast tests.
    """
    from coder_eval.models import RunLimits

    agent = ClaudeCodeAgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        ignore_patterns=[],
    )
    run_limits = RunLimits.model_construct(
        max_tool_calls=None,
        turn_timeout=turn_timeout,
        task_timeout=task_timeout,
    )
    task = TaskDefinition.model_construct(
        task_id="timeout_test",
        description="Test task",
        initial_prompt="Do something",
        tags=[],
        agent=agent,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="test.py", description="test.py must exist")],
        run_limits=run_limits,
        reference=None,
    )
    return task


def _make_turn_record(iteration: int = 1) -> TurnRecord:
    """Create a minimal TurnRecord for testing."""
    return TurnRecord(
        iteration=iteration,
        user_input="test prompt",
        agent_output="done",
        duration_seconds=1.0,
    )


def _completed_outcome(record: TurnRecord) -> TurnOutcome:
    return TurnOutcome(record=record, status=AgentEndStatus.COMPLETED, error=None)


def _crashed_outcome(record: TurnRecord, error: str) -> TurnOutcome:
    return TurnOutcome(record=record, status=AgentEndStatus.CRASHED, error=error)


def _timeout_outcome(record: TurnRecord, error: str) -> TurnOutcome:
    return TurnOutcome(record=record, status=AgentEndStatus.TIMEOUT, error=error)


def _make_initialized_orchestrator(task: TaskDefinition, tmp_path) -> Orchestrator:
    """Build an Orchestrator with a pre-initialized EvaluationResult and mock sandbox/checker."""
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator.result = EvaluationResult(
        task_id="timeout_test",
        task_description="Test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox
    orchestrator.success_checker = MagicMock()
    orchestrator._build_monitor()
    return orchestrator


@pytest.mark.asyncio
async def test_turn_timeout_propagates_from_agent(tmp_path) -> None:
    """The orchestrator propagates TurnTimeoutError raised by ``agent.communicate``.

    Coverage split: the threaded watchdog's *firing* behaviour (SIGKILL at
    deadline) is tested in ``tests/test_watchdog.py`` and
    ``tests/test_agent_timeout.py``. This test only verifies propagation
    through ``_evaluation_loop`` → ``execute_with_retry`` (AGENT_TIMEOUT is
    non-retryable) → caller.
    """
    turn_timeout = 0.1
    task = _make_task(turn_timeout=turn_timeout)
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    # Simulate what the real ClaudeCodeAgent does when its ThreadedWatchdog
    # fires: raise TurnTimeoutError from communicate().
    mock_agent = AsyncMock()

    async def timeout_communicate(_prompt, **kwargs):
        await asyncio.sleep(0.01)
        record = TurnRecord(iteration=kwargs["iteration"], user_input=_prompt, agent_output="", crashed=True)
        return _timeout_outcome(record, "agent turn timed out")

    mock_agent.communicate = timeout_communicate
    orchestrator.agent = mock_agent

    with pytest.raises(TurnTimeoutError) as exc_info:
        await orchestrator._evaluation_loop()

    assert exc_info.value.layer == "turn"
    assert exc_info.value.timeout_seconds == turn_timeout
    assert exc_info.value.iteration == 1


@pytest.mark.asyncio
async def test_task_timeout_fires(tmp_path) -> None:
    """Task timeout fires when the overall evaluation loop takes too long.

    The orchestrator's ThreadedWatchdog cancels the running task at the
    deadline; the outer orchestrator converts that into TaskTimeoutError.
    """
    task_timeout = 0.1
    task = _make_task(task_timeout=task_timeout)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    async def slow_loop():
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    result = await orchestrator.run()
    assert result.final_status == "TIMEOUT"
    assert f"Task timed out after {task_timeout}s" in (result.error_message or "")
    assert len(result.post_failure_criteria_results) == 1
    assert result.post_failure_criteria_results[0].evaluation_status == "not_evaluated"


@pytest.mark.asyncio
async def test_task_timeout_populates_elapsed_seconds(tmp_path) -> None:
    """TaskTimeoutError carries elapsed_seconds when raised by the orchestrator."""
    task_timeout = 0.1
    task = _make_task(task_timeout=task_timeout)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    async def slow_loop():
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    with patch("coder_eval.orchestrator.create_error_context") as mock_create:
        mock_create.return_value = {}
        result = await orchestrator.run()

    assert result.final_status == "TIMEOUT"
    assert "Task timed out" in (result.error_message or "")

    error_arg = mock_create.call_args.kwargs["error"]
    assert isinstance(error_arg, TaskTimeoutError)
    assert error_arg.elapsed_seconds is not None
    assert error_arg.elapsed_seconds > 0


@pytest.mark.asyncio
async def test_no_timeout_when_none(tmp_path) -> None:
    """With both timeouts None, the loop runs to completion with no interference."""
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    mock_agent = AsyncMock()
    mock_agent.communicate = AsyncMock(return_value=_completed_outcome(_make_turn_record()))
    orchestrator.agent = mock_agent

    orchestrator.success_checker.check_all_async = AsyncMock(  # type: ignore[union-attr]
        return_value=[CriterionResult(criterion_type="file_exists", description="test", score=1.0)]
    )

    with patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None):
        success = await orchestrator._evaluation_loop()

    assert success is True


@pytest.mark.asyncio
async def test_task_timeout_hard_kills_agent(tmp_path) -> None:
    """When the task timeout fires, the orchestrator must call ``agent.kill_sync``
    (the sync variant, invoked from the watchdog's timer thread)."""
    task = _make_task(task_timeout=0.1)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    mock_agent = MagicMock()
    mock_agent.kill_sync = MagicMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    async def slow_loop():
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    result = await orchestrator.run()
    assert result.final_status == "TIMEOUT"
    mock_agent.kill_sync.assert_called_once()


@pytest.mark.asyncio
async def test_task_timeout_recovers_the_killed_turn(tmp_path) -> None:
    """A hard-killed task's spend lands on the result instead of vanishing.

    The in-flight attempt's ``EventCollector`` is parked on
    ``orchestrator._attempt_collector`` for the duration of the attempt, and
    ``_drain_killed_turn`` is the only reader of that slot: the cancel is a
    BaseException, so no outcome ever returns to the retry wrapper that would
    append it. Without the drain the row reports no turns
    and no cost for a task that spent real money.
    """
    task = _make_task(task_timeout=0.1)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    partial = TurnRecord(
        iteration=1,
        user_input="test prompt",
        agent_output="<partial record>",
        crashed=True,
        token_usage=TokenUsage(uncached_input_tokens=40_000, output_tokens=2_000, total_cost_usd=0.15),
    )

    collector = MagicMock()
    collector.ended = True
    collector.build_turn_record.return_value = partial

    mock_agent = MagicMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    async def slow_loop():
        orchestrator._attempt_collector = collector
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    result = await orchestrator.run()

    assert result.final_status == "TIMEOUT"
    assert result.iterations == [partial]
    assert result.total_token_usage is not None
    assert result.total_token_usage.output_tokens == 2_000
    assert result.total_token_usage.total_cost_usd == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_task_timeout_with_nothing_to_recover_still_lands(tmp_path) -> None:
    """A task killed before its first turn has nothing parked, and that is not an error.

    The recovery is best-effort and runs on the way to a saved row, so an empty slot
    must leave the TIMEOUT row intact rather than raising through teardown.
    """
    task = _make_task(task_timeout=0.1)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    mock_agent = MagicMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    async def slow_loop():
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    result = await orchestrator.run()

    assert result.final_status == "TIMEOUT"
    assert result.iterations == []


@pytest.mark.asyncio
async def test_turn_timeout_not_rewrapped_as_task_timeout(tmp_path) -> None:
    """A per-turn TurnTimeoutError propagates unchanged even when a larger
    task_timeout is also configured — the outer orchestrator must not
    re-wrap it as TaskTimeoutError."""
    turn_timeout = 0.1
    task = _make_task(turn_timeout=turn_timeout, task_timeout=60)
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    mock_agent = AsyncMock()

    async def turn_out_communicate(_prompt, **kwargs):
        await asyncio.sleep(0.01)
        record = TurnRecord(iteration=kwargs["iteration"], user_input=_prompt, agent_output="", crashed=True)
        return _timeout_outcome(record, "agent turn timed out")

    mock_agent.communicate = turn_out_communicate
    orchestrator.agent = mock_agent

    with pytest.raises(TurnTimeoutError):
        await orchestrator._evaluation_loop()


@pytest.mark.parametrize(
    "terminal_error",
    [
        pytest.param(
            TurnTimeoutError(1200, task_id="timeout_test", iteration=1),
            id="turn-timeout",
        ),
        pytest.param(AgentCrashError("agent subprocess crashed"), id="agent-crash"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_agent_error_records_safe_artifact_evidence_without_rescoring(
    tmp_path, terminal_error: Exception
) -> None:
    """Agent failures preserve artifact truth before the live sandbox is removed."""
    task = _make_task(turn_timeout=1200, task_timeout=1500)
    task.success_criteria = [
        CommandExecutedCriterion(
            type="command_executed",
            tool_name="Bash",
            description="agent ran validator",
        ),
        FileExistsCriterion(type="file_exists", path="artifact.txt", description="artifact exists"),
        RunCommandCriterion(
            type="run_command",
            command="touch should-not-run",
            description="sandbox command",
        ),
        LLMJudgeCriterion(
            type="llm_judge",
            prompt="Grade the artifact.",
            description="paid judge",
        ),
    ]
    run_dir = tmp_path / "run" / "post_failure_evidence"
    run_dir.mkdir(parents=True)
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._refresh_runtime_tool_versions = MagicMock()  # type: ignore[method-assign]
    orchestrator._evaluation_loop = AsyncMock(side_effect=terminal_error)  # type: ignore[method-assign]

    sandbox = Sandbox(SandboxConfig(driver="tempdir"), task_id=task.task_id)
    sandbox_dir = sandbox.setup()
    (sandbox_dir / "artifact.txt").write_text("finished", encoding="utf-8")
    orchestrator.sandbox = sandbox

    checker = SuccessChecker(sandbox)
    checker.check_all_async = AsyncMock(wraps=checker.check_all_async)  # type: ignore[method-assign]
    orchestrator.success_checker = checker

    async def cleanup() -> None:
        sandbox.cleanup()

    orchestrator._cleanup = cleanup  # type: ignore[method-assign]

    mock_agent = MagicMock()
    mock_agent.kill_sync = MagicMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    result = await orchestrator.run()

    assert result.final_status == "ERROR"
    assert result.error_message == str(terminal_error)
    assert result.weighted_score == 0.0
    assert result.success_criteria_results == []
    assert len(result.post_failure_criteria_results) == 4
    agent_dependent, artifact, command, judge = result.post_failure_criteria_results
    assert artifact.score == 1.0
    assert artifact.evaluation_status == "evaluated"
    for unavailable in (agent_dependent, command, judge):
        assert unavailable.score == 0.0
        assert unavailable.evaluation_status == "not_evaluated"
        assert "not a deterministic, read-only artifact check" in (unavailable.details or "")

    checked_criteria = checker.check_all_async.await_args.args[0]
    assert [criterion.type for criterion in checked_criteria] == ["file_exists"]
    assert not sandbox_dir.exists(), "cleanup must run after diagnostic grading"

    persisted = EvaluationResult.model_validate_json((run_dir / "task.json").read_text())
    assert persisted.final_status == "ERROR"
    assert persisted.weighted_score == 0.0
    assert [r.evaluation_status for r in persisted.post_failure_criteria_results] == [
        "not_evaluated",
        "evaluated",
        "not_evaluated",
        "not_evaluated",
    ]


@pytest.mark.parametrize(
    "recovery_error",
    [
        pytest.param(JudgeInfrastructureError("judge unavailable"), id="judge-infrastructure"),
        pytest.param(CheckerMisuseError("checker contract violated"), id="checker-misuse"),
        pytest.param(None, id="result-count-mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_post_failure_checker_error_preserves_terminal_agent_error(
    tmp_path, recovery_error: Exception | None
) -> None:
    task = _make_task(turn_timeout=1200, task_timeout=1500)
    task.success_criteria = [
        FileExistsCriterion(type="file_exists", path="artifact.txt", description="artifact exists")
    ]
    orchestrator = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._refresh_runtime_tool_versions = MagicMock()  # type: ignore[method-assign]
    terminal_error = TurnTimeoutError(1200, task_id=task.task_id, iteration=1)
    orchestrator._evaluation_loop = AsyncMock(side_effect=terminal_error)  # type: ignore[method-assign]
    orchestrator.sandbox = MagicMock()
    orchestrator.success_checker = MagicMock()
    if recovery_error is None:
        orchestrator.success_checker.check_all_async = AsyncMock(return_value=[])
        expected_type = "ValueError"
        expected_message = "Post-failure checker returned 0 results for 1 runnable criteria"
    else:
        orchestrator.success_checker.check_all_async = AsyncMock(side_effect=recovery_error)
        expected_type = type(recovery_error).__name__
        expected_message = str(recovery_error)
    orchestrator.agent = MagicMock()
    orchestrator.agent.get_sdk_options.return_value = None

    result = await orchestrator.run()

    assert result.final_status == "ERROR"
    assert result.error_message == str(terminal_error)
    assert result.weighted_score == 0.0
    assert len(result.post_failure_criteria_results) == 1
    unavailable = result.post_failure_criteria_results[0]
    assert unavailable.evaluation_status == "not_evaluated"
    assert expected_type in (unavailable.details or "")
    assert expected_message in (unavailable.details or "")


@pytest.mark.asyncio
async def test_task_timeout_during_post_failure_grading_preserves_agent_error(tmp_path) -> None:
    task = _make_task(turn_timeout=1200, task_timeout=0.1)
    run_dir = tmp_path / "run" / "diagnostic_timeout"
    run_dir.mkdir(parents=True)
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._refresh_runtime_tool_versions = MagicMock()  # type: ignore[method-assign]
    terminal_error = TurnTimeoutError(1200, task_id=task.task_id, iteration=1)
    orchestrator._evaluation_loop = AsyncMock(side_effect=terminal_error)  # type: ignore[method-assign]
    orchestrator.sandbox = MagicMock()
    orchestrator.success_checker = MagicMock()

    async def slow_check(*_args, **_kwargs):
        await asyncio.sleep(10)

    orchestrator.success_checker.check_all_async = slow_check
    orchestrator.agent = MagicMock()
    orchestrator.agent.kill_sync = MagicMock()
    orchestrator.agent.get_sdk_options.return_value = None

    result = await orchestrator.run()

    assert result.final_status == "ERROR"
    assert result.error_message == str(terminal_error)
    assert result.weighted_score == 0.0
    assert len(result.post_failure_criteria_results) == 1
    unavailable = result.post_failure_criteria_results[0]
    assert unavailable.evaluation_status == "not_evaluated"
    assert "task_timeout budget expired during post-failure grading" in (unavailable.details or "")


def test_runtime_timeout_warning_is_emitted_once(tmp_path, caplog) -> None:
    import logging

    task = _make_task(turn_timeout=1200, task_timeout=1500)
    orchestrator = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="test-variant")

    with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
        orchestrator._warn_on_ineffective_task_timeout()
        orchestrator._warn_on_ineffective_task_timeout()

    messages = [record.message for record in caplog.records if "single iteration" in record.message]
    assert len(messages) == 1
    assert "A larger task_timeout cannot extend the agent's single iteration" in messages[0]
    assert "the agent budget is turn_timeout" in messages[0]


@pytest.mark.asyncio
async def test_task_timeout_fires_when_inner_coro_swallows_cancel(tmp_path) -> None:
    """Belt-and-suspenders: if ``_evaluation_loop`` catches ``CancelledError``
    internally (as anyio cancel scopes do), the post-loop ``wd.fired`` check
    still triggers ``TaskTimeoutError``.
    """
    task_timeout = 0.1
    task = _make_task(task_timeout=task_timeout)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    async def cancel_swallowing_loop() -> bool:
        # Simulate anyio's behaviour: catch CancelledError and keep going.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = cancel_swallowing_loop  # type: ignore[method-assign]

    result = await orchestrator.run()
    assert result.final_status == "TIMEOUT"
    assert f"Task timed out after {task_timeout}s" in (result.error_message or "")


@pytest.mark.asyncio
async def test_turn_timeout_is_per_attempt_not_cycle(tmp_path):
    """Each retry attempt gets a fresh turn_timeout, not a shared cycle budget.

    Asserts the contract via call inspection: every attempt receives the
    same ``timeout=turn_timeout`` kwarg. A shared retry-cycle budget would
    decrement (or omit) the second-attempt timeout.
    """
    task = _make_task(turn_timeout=1.0)
    run_dir = tmp_path / "run" / "per_attempt_budget"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._build_monitor()
    orchestrator.result = EvaluationResult(
        task_id="per_attempt_budget",
        task_description="per-attempt budget",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    partial_record = TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True)
    success_record = _make_turn_record()

    timeouts_seen: list[float | None] = []

    async def flaky_communicate(_prompt, **kwargs):
        timeouts_seen.append(kwargs.get("timeout"))
        if len(timeouts_seen) == 1:
            return _crashed_outcome(partial_record, "mid-turn failure")
        return _completed_outcome(success_record)

    mock_agent = AsyncMock()
    mock_agent.communicate = flaky_communicate
    orchestrator.agent = mock_agent

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    mock_checker = MagicMock()
    mock_checker.check_all_async = AsyncMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
    )
    orchestrator.success_checker = mock_checker

    # Skip executor backoff sleeps so the test stays fast.
    async def fast_retry_sleep(delay: float) -> None:
        return None

    with (
        patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
        patch("asyncio.sleep", side_effect=fast_retry_sleep),
    ):
        success = await orchestrator._evaluation_loop()

    assert success is True
    assert timeouts_seen == [1.0, 1.0], "every attempt must receive turn_timeout fresh"
    # Result.iterations: the CRASHED outcome's partial (appended by
    # `_communicate_with_retry`) + the retry's success record (appended by the
    # main flow).
    assert len(orchestrator.result.iterations) == 2
    assert orchestrator.result.iterations[0].crashed is True
    assert orchestrator.result.iterations[1].crashed is False


@pytest.mark.asyncio
async def test_crash_then_success_appends_both_records_same_iteration(tmp_path) -> None:
    """A crashed attempt's partial and the retry's record both land on
    ``result.iterations``, in order, sharing the iteration number — a retry
    resumes the same turn rather than starting a new one.
    """
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    partial = TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True)
    success = _make_turn_record(iteration=1)
    calls: list[int] = []

    async def flaky_communicate(_prompt, **kwargs):
        calls.append(kwargs["iteration"])
        if len(calls) == 1:
            return _crashed_outcome(partial, "mid-turn failure")
        return _completed_outcome(success)

    mock_agent = AsyncMock()
    mock_agent.communicate = flaky_communicate
    orchestrator.agent = mock_agent
    orchestrator.success_checker.check_all_async = AsyncMock(  # type: ignore[union-attr]
        return_value=[CriterionResult(criterion_type="file_exists", description="test", score=1.0)]
    )

    async def fast_retry_sleep(delay: float) -> None:
        return None

    with (
        patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
        patch("asyncio.sleep", side_effect=fast_retry_sleep),
    ):
        success_result = await orchestrator._evaluation_loop()

    assert success_result is True
    assert calls == [1, 1]
    assert orchestrator.result.iterations == [partial, success]


@pytest.mark.asyncio
async def test_timeout_outcome_is_not_retried(tmp_path) -> None:
    """A TIMEOUT outcome is terminal: ``communicate`` is called exactly once,
    and its record is appended to ``result.iterations`` before the
    ``TurnTimeoutError`` propagates.
    """
    task = _make_task(turn_timeout=5.0)
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    record = TurnRecord(iteration=1, user_input="p", agent_output="", crashed=True)
    calls = 0

    async def timeout_once(_prompt, **kwargs):
        nonlocal calls
        calls += 1
        return _timeout_outcome(record, "timed out")

    mock_agent = AsyncMock()
    mock_agent.communicate = timeout_once
    orchestrator.agent = mock_agent

    with pytest.raises(TurnTimeoutError):
        await orchestrator._evaluation_loop()

    assert calls == 1
    assert orchestrator.result.iterations == [record]


@pytest.mark.asyncio
async def test_wait_for_backstop_appends_record_when_agent_ended_its_turn(tmp_path, monkeypatch) -> None:
    """The ``wait_for`` backstop kills a hung agent and, when the attempt's
    ``EventCollector`` already saw an ``AgentEndEvent`` before the hang, appends
    that record to ``result.iterations``.
    """
    monkeypatch.setattr("coder_eval.orchestrator._WAIT_FOR_GRACE_SECONDS", 0.05)
    task = _make_task(turn_timeout=0.05)
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    never_set = asyncio.Event()

    async def hanging_but_ended(_prompt, *, stream_callback, **kwargs):
        stream_callback.on_event(AgentStartEvent(task_id=task.task_id, iteration=kwargs["iteration"], prompt=_prompt))
        stream_callback.on_event(
            AgentEndEvent(
                task_id=task.task_id, iteration=kwargs["iteration"], crashed=True, status=AgentEndStatus.CRASHED
            )
        )
        await never_set.wait()
        raise AssertionError("unreachable: wait_for should have cancelled this")

    mock_agent = AsyncMock()
    mock_agent.communicate = hanging_but_ended
    mock_agent.kill = AsyncMock()
    orchestrator.agent = mock_agent

    with (
        patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
        pytest.raises(TurnTimeoutError),
    ):
        await orchestrator._evaluation_loop()

    assert mock_agent.kill.await_count == 1
    assert len(orchestrator.result.iterations) == 1
    assert orchestrator.result.iterations[0].crashed is True


@pytest.mark.asyncio
async def test_wait_for_backstop_appends_nothing_when_agent_never_ended(tmp_path, monkeypatch) -> None:
    """When the hung agent never got as far as an ``AgentEndEvent``, the
    backstop's kill has nothing to recover and appends nothing.
    """
    monkeypatch.setattr("coder_eval.orchestrator._WAIT_FOR_GRACE_SECONDS", 0.05)
    task = _make_task(turn_timeout=0.05)
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    never_set = asyncio.Event()

    async def hanging_never_started(_prompt, **kwargs):
        await never_set.wait()
        raise AssertionError("unreachable: wait_for should have cancelled this")

    mock_agent = AsyncMock()
    mock_agent.communicate = hanging_never_started
    mock_agent.kill = AsyncMock()
    orchestrator.agent = mock_agent

    with (
        patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
        pytest.raises(TurnTimeoutError),
    ):
        await orchestrator._evaluation_loop()

    assert mock_agent.kill.await_count == 1
    assert orchestrator.result.iterations == []


@pytest.mark.asyncio
async def test_task_timeout_recovers_in_flight_turn_via_attempt_collector(tmp_path) -> None:
    """A real task-timeout cancellation, hitting the agent mid-``communicate``,
    is recovered through ``orchestrator._attempt_collector`` (not
    ``agent.pending_turn``) by ``_drain_killed_turn``.
    """
    task = _make_task(task_timeout=0.1)
    run_dir = tmp_path / "run" / "drain_killed_turn"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._build_monitor()
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox
    orchestrator.success_checker = MagicMock()

    async def hang_after_ending(_prompt, *, stream_callback, **kwargs):
        stream_callback.on_event(AgentStartEvent(task_id=task.task_id, iteration=kwargs["iteration"], prompt=_prompt))
        stream_callback.on_event(
            AgentEndEvent(
                task_id=task.task_id, iteration=kwargs["iteration"], crashed=True, status=AgentEndStatus.CRASHED
            )
        )
        await asyncio.Event().wait()

    mock_agent = AsyncMock()
    mock_agent.communicate = hang_after_ending
    mock_agent.kill_sync = MagicMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    result = await orchestrator.run()

    assert result.final_status == "TIMEOUT"
    assert len(result.iterations) == 1
    assert result.iterations[0].crashed is True


@pytest.mark.asyncio
async def test_task_timeout_after_finished_attempt_appends_nothing_extra(tmp_path) -> None:
    """A task timeout that fires AFTER the agent's attempt already finished
    (e.g. during a slow criteria check) finds ``_attempt_collector`` already
    cleared to ``None`` and drains nothing extra.
    """
    task = _make_task(task_timeout=0.15)
    run_dir = tmp_path / "run" / "drain_after_finished_attempt"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._build_monitor()
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    record = _make_turn_record()

    async def quick_communicate(_prompt, **kwargs):
        callback = kwargs["stream_callback"]
        callback.on_event(AgentStartEvent(task_id="t", iteration=kwargs["iteration"]))
        callback.on_event(AgentEndEvent(task_id="t", iteration=kwargs["iteration"]))
        return _completed_outcome(record)

    mock_agent = AsyncMock()
    mock_agent.communicate = quick_communicate
    mock_agent.kill_sync = MagicMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    mock_checker = MagicMock()

    async def slow_check(*_args, **_kwargs):
        await asyncio.sleep(10)
        return []

    mock_checker.check_all_async = slow_check
    orchestrator.success_checker = mock_checker

    result = await orchestrator.run()

    assert orchestrator._attempt_collector is None
    assert result.final_status == "TIMEOUT"
    assert result.iterations == [record]


@pytest.mark.asyncio
async def test_unhandled_end_status_raises_runtime_error_and_ends_error(tmp_path, monkeypatch) -> None:
    """An outcome status outside the returned/crash/timeout allowlist is a
    harness bug: it raises ``RuntimeError`` and the task ends ``ERROR``.
    """
    monkeypatch.setattr("coder_eval.orchestrator._RETURNED_END_STATUSES", frozenset())
    task = _make_task()
    run_dir = tmp_path / "run" / "unhandled_status"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._build_monitor()
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox
    orchestrator.success_checker = MagicMock()

    record = _make_turn_record()
    calls: list[int] = []

    async def completed_communicate(_prompt, **kwargs):
        calls.append(kwargs["iteration"])
        return _completed_outcome(record)

    mock_agent = AsyncMock()
    mock_agent.communicate = completed_communicate
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    result = await orchestrator.run()

    assert result.final_status == "ERROR"
    assert "unhandled end status" in (result.error_message or "")
    assert calls == [1], "an unhandled status is a harness bug, never a retried turn"


@pytest.mark.asyncio
async def test_every_attempt_crashing_appends_every_partial_before_the_error(tmp_path) -> None:
    """CRASHED on the final retry still appends its record before ``AgentCrashError`` escapes."""
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)
    partials = [TurnRecord(iteration=1, user_input="p", agent_output=f"<partial {n}>", crashed=True) for n in range(3)]
    calls: list[int] = []

    async def crashing_communicate(_prompt, **kwargs):
        calls.append(kwargs["iteration"])
        return _crashed_outcome(partials[len(calls) - 1], "provider exploded")

    mock_agent = AsyncMock()
    mock_agent.communicate = crashing_communicate
    orchestrator.agent = mock_agent

    async def fast_retry_sleep(delay: float) -> None:
        return None

    with (
        patch("coder_eval.orchestrator.resolve_reference_dir", return_value=None),
        patch("asyncio.sleep", side_effect=fast_retry_sleep),
        pytest.raises(AgentCrashError, match="provider exploded"),
    ):
        await orchestrator._evaluation_loop()

    assert calls == [1, 1, 1]
    assert orchestrator.result.iterations == partials
    assert orchestrator._attempt_collector is None
