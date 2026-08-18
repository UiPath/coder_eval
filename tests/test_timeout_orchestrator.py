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
        max_turns=None,
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
        raise TurnTimeoutError(turn_timeout, iteration=1)

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
    mock_agent.communicate = AsyncMock(return_value=_make_turn_record())
    orchestrator.agent = mock_agent

    orchestrator.success_checker.check_all_async = AsyncMock(  # type: ignore[union-attr]
        return_value=[CriterionResult(criterion_type="file_exists", description="test", score=1.0)]
    )

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
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

    The agent parks the interrupted turn on ``pending_turn`` when it is cancelled,
    and the task-timeout handler is the only reader of that slot: the cancel is a
    BaseException, so it never reaches the retry executor's per-attempt hook that
    drains it on a turn-level timeout. Without the drain the row reports no turns
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

    mock_agent = MagicMock()
    mock_agent.pending_turn = partial
    mock_agent.discard_pending_turn = AsyncMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    async def slow_loop():
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    result = await orchestrator.run()

    assert result.final_status == "TIMEOUT"
    assert result.iterations == [partial]
    assert result.total_token_usage is not None
    assert result.total_token_usage.output_tokens == 2_000
    assert result.total_token_usage.total_cost_usd == pytest.approx(0.15)
    # Drained through the documented contract, so the slot is left clean.
    mock_agent.discard_pending_turn.assert_awaited_once()


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
    mock_agent.pending_turn = None
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
        raise TurnTimeoutError(turn_timeout, iteration=1)

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

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
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

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
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

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
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
    from coder_eval.errors import AgentCrashError

    task = _make_task(turn_timeout=1.0)
    run_dir = tmp_path / "run" / "per_attempt_budget"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
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
            mock_agent.pending_turn = partial_record
            raise AgentCrashError("mid-turn failure")
        return success_record

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
        patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)),
        patch("asyncio.sleep", side_effect=fast_retry_sleep),
    ):
        success = await orchestrator._evaluation_loop()

    assert success is True
    assert timeouts_seen == [1.0, 1.0], "every attempt must receive turn_timeout fresh"
    # Result.turns: partial (from on_attempt_error) + success (from main flow).
    assert len(orchestrator.result.iterations) == 2
    assert orchestrator.result.iterations[0].crashed is True
    assert orchestrator.result.iterations[1].crashed is False


@pytest.mark.asyncio
async def test_wait_for_backstop_calls_discard_pending_turn(tmp_path):
    """When the outer ``asyncio.wait_for`` fires, the orchestrator must call
    ``agent.discard_pending_turn()`` after ``agent.kill()``.

    The wait_for cancels ``communicate()`` via ``CancelledError``
    (a ``BaseException``), which bypasses the agent's ``except Exception``
    handlers — so the per-turn iteration counter that ``communicate()`` bumped
    at entry never gets rolled back by the agent's normal failure path. The
    orchestrator must invoke ``discard_pending_turn()`` so any future change
    to the AGENT_TIMEOUT retry policy doesn't silently break the
    "partials and the retry share an iteration number" contract.
    """
    task = _make_task(turn_timeout=0.05)
    run_dir = tmp_path / "run" / "discard_pending"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    orchestrator.result = EvaluationResult(
        task_id="discard_pending",
        task_description="discard_pending",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # An Event().wait() coroutine never completes on its own — wait_for must
    # cancel it. Plain asyncio.sleep would be vulnerable to a global sleep
    # patch elsewhere; Event.wait isolates this test from that.
    never_set = asyncio.Event()

    async def hanging_communicate(_prompt, **kwargs):
        await never_set.wait()
        raise AssertionError("unreachable: wait_for should have cancelled this")

    mock_agent = AsyncMock()
    mock_agent.communicate = hanging_communicate
    mock_agent.kill = AsyncMock()
    mock_agent.discard_pending_turn = AsyncMock()
    orchestrator.agent = mock_agent

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    mock_checker = MagicMock()
    orchestrator.success_checker = mock_checker

    with (
        patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)),
        pytest.raises(TurnTimeoutError),
    ):
        await orchestrator._evaluation_loop()

    # kill() and discard_pending_turn() must both have run.
    assert mock_agent.kill.await_count == 1
    assert mock_agent.discard_pending_turn.await_count == 1


@pytest.mark.asyncio
async def test_claude_agent_discard_pending_turn_rolls_back_iteration():
    """ClaudeCodeAgent.discard_pending_turn is slot-gated: it decrements _iteration
    only when pending_turn is set, and is idempotent when the slot is empty.
    """
    from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
    from coder_eval.models import AgentKind, TurnRecord, parse_agent_config

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)

    # Idle agent (no pending turn): discard is a no-op. Negative values would
    # break the "partials and retry share an iteration" contract.
    assert agent._iteration == 0
    await agent.discard_pending_turn()
    assert agent._iteration == 0

    # With pending_turn set: discard clears the slot AND decrements _iteration.
    partial = TurnRecord(iteration=3, user_input="p", agent_output="<partial>", crashed=True)
    agent._iteration = 3
    agent.pending_turn = partial
    await agent.discard_pending_turn()
    assert agent.pending_turn is None
    assert agent._iteration == 2
