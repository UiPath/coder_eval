"""Tests for timeout behavior in the orchestrator."""

import asyncio
import contextlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors.timeout import TaskTimeoutError, TurnTimeoutError
from coder_eval.models import (
    AgentKind,
    ClaudeCodeAgentConfig,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    SandboxConfig,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
)
from coder_eval.orchestrator import Orchestrator


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


def _make_success_checker(*, passing: bool) -> MagicMock:
    """A success_checker mock whose check_all_async reports pass/fail for one criterion."""
    checker = MagicMock()
    score = 1.0 if passing else 0.0
    checker.check_all_async = AsyncMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="test", score=score)]
    )
    return checker


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
    # success_checker is None (never set — _setup was mocked), so
    # _grade_after_forced_kill's precondition guard falls back to TIMEOUT
    # without attempting grading. This exercises that fallback path
    # specifically, not just a status that happens to match.
    assert result.success_criteria_results == []


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


@pytest.mark.asyncio
async def test_turn_timeout_grades_success_when_agent_finished(tmp_path) -> None:
    """A TurnTimeoutError whose salvaged partial turn satisfies success criteria
    must result in SUCCESS, not ERROR -- the agent's real output must still be graded."""
    task = _make_task(turn_timeout=0.1)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")

    async def fake_setup() -> None:
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_dir = tmp_path / "sandbox"
        mock_sandbox.sandbox_dir.mkdir()
        orchestrator.sandbox = mock_sandbox
        orchestrator.success_checker = _make_success_checker(passing=True)

    orchestrator._setup = fake_setup  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    partial = TurnRecord(iteration=1, user_input="p", agent_output="<done>", crashed=True)

    mock_agent = MagicMock()
    mock_agent.pending_turn = partial
    mock_agent.discard_pending_turn = AsyncMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)

    async def timeout_communicate(_prompt, **kwargs):
        raise TurnTimeoutError(0.1, iteration=1)

    mock_agent.communicate = timeout_communicate
    orchestrator.agent = mock_agent

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        result = await orchestrator.run()

    assert result.final_status == "SUCCESS"
    assert result.success_criteria_results
    # A genuinely successful, correctly-graded run must not carry the timeout
    # exception's message/traceback forward -- "SUCCESS (plain, no special
    # marker)" per the plan's decision.
    assert result.error_message is None
    assert result.error_details is None


@pytest.mark.asyncio
async def test_turn_timeout_grades_timeout_status_when_criteria_fail(tmp_path) -> None:
    """A TurnTimeoutError whose salvaged partial turn does NOT satisfy criteria
    must result in TIMEOUT (not ERROR, not FAILURE), with the timeout mark preserved."""
    task = _make_task(turn_timeout=0.1)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")

    async def fake_setup() -> None:
        mock_sandbox = MagicMock()
        mock_sandbox.sandbox_dir = tmp_path / "sandbox"
        mock_sandbox.sandbox_dir.mkdir()
        orchestrator.sandbox = mock_sandbox
        orchestrator.success_checker = _make_success_checker(passing=False)

    orchestrator._setup = fake_setup  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    partial = TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True)

    mock_agent = MagicMock()
    mock_agent.pending_turn = partial
    mock_agent.discard_pending_turn = AsyncMock()
    mock_agent.get_sdk_options = MagicMock(return_value=None)

    async def timeout_communicate(_prompt, **kwargs):
        raise TurnTimeoutError(0.1, iteration=1)

    mock_agent.communicate = timeout_communicate
    orchestrator.agent = mock_agent

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        result = await orchestrator.run()

    assert result.final_status == "TIMEOUT"
    assert "timed out" in (result.error_message or "").lower()
    assert result.success_criteria_results


@pytest.mark.asyncio
async def test_task_timeout_grades_success_when_agent_finished(tmp_path) -> None:
    """A TaskTimeoutError whose recovered turn satisfies criteria results in SUCCESS."""
    task = _make_task(task_timeout=0.1)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox
    orchestrator.success_checker = _make_success_checker(passing=True)

    mock_agent = MagicMock()
    mock_agent.pending_turn = None
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    async def slow_loop():
        orchestrator.result.iterations.append(_make_turn_record())
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        result = await orchestrator.run()

    assert result.final_status == "SUCCESS"
    assert result.success_criteria_results
    assert result.error_message is None
    assert result.error_details is None


@pytest.mark.asyncio
async def test_task_timeout_grades_timeout_status_when_criteria_fail(tmp_path) -> None:
    """A TaskTimeoutError whose recovered turn does NOT satisfy criteria still results
    in TIMEOUT (unchanged from today for the failing case)."""
    task = _make_task(task_timeout=0.1)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox
    orchestrator.success_checker = _make_success_checker(passing=False)

    mock_agent = MagicMock()
    mock_agent.pending_turn = None
    mock_agent.get_sdk_options = MagicMock(return_value=None)
    orchestrator.agent = mock_agent

    async def slow_loop():
        orchestrator.result.iterations.append(_make_turn_record())
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        result = await orchestrator.run()

    assert result.final_status == "TIMEOUT"
    assert result.success_criteria_results


@pytest.mark.asyncio
async def test_grade_after_forced_kill_falls_back_when_success_checker_missing(tmp_path) -> None:
    """No success_checker (setup never completed) falls back without raising."""
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)
    orchestrator.success_checker = None

    await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    assert orchestrator.result is not None
    assert orchestrator.result.final_status == FinalStatus.TIMEOUT


@pytest.mark.asyncio
async def test_grade_after_forced_kill_falls_back_when_check_all_async_raises(tmp_path) -> None:
    """check_all_async raising falls back to fallback_status without propagating."""
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)
    orchestrator.success_checker.check_all_async = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[union-attr]

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    assert orchestrator.result is not None
    assert orchestrator.result.final_status == FinalStatus.TIMEOUT


@pytest.mark.asyncio
async def test_grade_after_forced_kill_skips_regrade_when_already_graded(tmp_path) -> None:
    """The belt-and-suspenders TaskTimeoutError (run() fires it after
    _evaluation_loop already completed a normal grading pass) must not
    re-run check_all_async -- that would double-spend any llm_judge/agent_judge
    criterion for no new information. Re-derive status from the existing
    results instead.

    The shortcut requires the recorded grade to cover the whole recorded
    trajectory (``_graded_iteration_count == len(result.iterations)``, stamped
    by every grading path); see
    ``test_grade_after_forced_kill_regrades_when_existing_results_predate_the_last_turn``
    for the stale-snapshot case that must NOT take it."""
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)
    orchestrator.success_checker.check_all_async = AsyncMock(  # type: ignore[union-attr]
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=0.0)]
    )
    orchestrator.result.iterations = [_make_turn_record(1)]
    orchestrator.result.success_criteria_results = [
        CriterionResult(criterion_type="file_exists", description="x", score=1.0)
    ]
    orchestrator._graded_iteration_count = len(orchestrator.result.iterations)

    await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    orchestrator.success_checker.check_all_async.assert_not_awaited()  # type: ignore[union-attr]
    assert orchestrator.result.final_status == FinalStatus.SUCCESS
    assert orchestrator.result.error_message is None


@pytest.mark.asyncio
async def test_grade_after_forced_kill_keeps_the_fallback_status_when_grading_is_cancelled(tmp_path) -> None:
    """Regression test (code-review finding): a BaseException during grading
    must not leave the row at the constructor default.

    `except Exception` deliberately does not catch `CancelledError` (a
    BaseException), so if the fallback were only committed inside the handler,
    a Ctrl-C or batch-level cancel landing in `check_all_async` would persist
    `final_status=FAILURE` while `error_message` says the task timed out.
    """
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    async def cancelled_check_all_async(*args, **kwargs):
        raise asyncio.CancelledError()

    orchestrator.success_checker.check_all_async = cancelled_check_all_async  # type: ignore[union-attr]

    with (
        patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)),
        contextlib.suppress(asyncio.CancelledError),
    ):
        await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    assert orchestrator.result.final_status == FinalStatus.TIMEOUT


@pytest.mark.asyncio
async def test_grade_after_forced_kill_quiesces_the_agent_before_reading_the_sandbox(tmp_path) -> None:
    """Regression test (code-review finding): grading must not race a live agent.

    On a TurnTimeoutError the agent raised at its OWN internal deadline —
    nothing has torn the harness down yet (Antigravity's `kill_sync` is
    intent-only, and `_cleanup()` runs in run()'s finally, after this grading
    pass). Without an explicit quiesce, a backgrounded build would still be
    writing into the sandbox while the criteria read it.
    """
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)
    call_order: list[str] = []

    async def recording_kill():
        call_order.append("kill")

    async def recording_check_all_async(*args, **kwargs):
        call_order.append("grade")
        return [CriterionResult(criterion_type="file_exists", description="x", score=1.0)]

    orchestrator.agent = MagicMock()
    orchestrator.agent.kill = recording_kill
    orchestrator.success_checker.check_all_async = recording_check_all_async  # type: ignore[union-attr]

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    assert call_order == ["kill", "grade"]
    assert orchestrator.result.final_status == FinalStatus.SUCCESS


@pytest.mark.asyncio
async def test_grade_after_forced_kill_grades_even_if_quiescing_the_agent_fails(tmp_path) -> None:
    """The quiesce is best-effort: a failing kill() must not skip grading."""
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    async def failing_kill():
        raise RuntimeError("harness already gone")

    orchestrator.agent = MagicMock()
    orchestrator.agent.kill = failing_kill
    orchestrator.success_checker.check_all_async = AsyncMock(  # type: ignore[union-attr]
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
    )

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    orchestrator.success_checker.check_all_async.assert_awaited()  # type: ignore[union-attr]
    assert orchestrator.result.final_status == FinalStatus.SUCCESS


@pytest.mark.asyncio
async def test_grade_after_forced_kill_gates_armed_only_when_the_watcher_fired(tmp_path) -> None:
    """The FIRED-ONLY gate contract must hold on the forced-kill path too.

    _grade_after_forced_kill back-fills ``result.early_stop`` from the watcher
    (a hard-killed run never reaches _evaluation_loop's own assignment), which
    is what makes the armed branch of ``_gate_passed`` reachable here at all.
    Once it fires, gating is the WEIGHTED ARMED subset -- a failing UNARMED
    criterion stays advisory and must not veto SUCCESS, exactly as on the
    normal early-stop path CLAUDE.md documents.
    """
    from coder_eval.models import EarlyStopInfo, EarlyStopReason
    from coder_eval.orchestration.early_stop import EarlyStopWatcher
    from coder_eval.orchestrator import DEFAULT_STOP_EARLY_GATE_THRESHOLD

    # file_exists is not a LiveSuccessCriterion, so it cannot carry a
    # stop_early block at all -- it is unarmed by construction, and here it
    # also fails, which is exactly the advisory-criterion case under test.
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)
    # spec'd so a typo'd attribute fails loudly, and .info is set EXPLICITLY:
    # a bare MagicMock().info is a truthy Mock, which would make the armed
    # branch look reachable even if the production back-fill were wrong.
    orchestrator._early_stop_watcher = MagicMock(spec=EarlyStopWatcher)
    orchestrator._early_stop_watcher.info = EarlyStopInfo(
        reason=EarlyStopReason.CRITERION_PASSED,
        deciding_criterion_type="file_exists",
        deciding_criterion_description="test.py must exist",
        armed_criteria=["file_exists"],
        sdk_turn_index=1,
        tool_call_index=0,
        elapsed_seconds=0.5,
        gate_threshold=1.0,
    )
    orchestrator.result.iterations = [_make_turn_record(1)]
    # The unarmed criterion fails; with strict-AND this would be TIMEOUT.
    orchestrator.success_checker.check_all_async = AsyncMock(  # type: ignore[union-attr]
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=0.0)]
    )
    # The gate methods are wrapped, not replaced, so the REAL weighted-armed
    # verdict decides the outcome and the assertions below pin both the
    # dispatch AND the threshold that gets forwarded.
    with (
        patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)),
        patch.object(
            EvaluationResult, "armed_criteria_passed", autospec=True, side_effect=lambda self, c, t: True
        ) as armed_gate,
        patch.object(EvaluationResult, "all_criteria_passed", autospec=True) as strict_gate,
    ):
        await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    strict_gate.assert_not_called()
    armed_gate.assert_called_once()
    # the resolved gate threshold must be forwarded, not defaulted away
    assert armed_gate.call_args.args[1] is task.success_criteria
    assert armed_gate.call_args.args[2] == DEFAULT_STOP_EARLY_GATE_THRESHOLD
    assert orchestrator.result.early_stop is not None
    # ...and the failing UNARMED criterion did not veto SUCCESS
    assert orchestrator.result.success_criteria_results[0].score == 0.0
    assert orchestrator.result.final_status == FinalStatus.SUCCESS


@pytest.mark.asyncio
async def test_grade_after_forced_kill_gates_strict_and_when_the_watcher_never_fired(tmp_path) -> None:
    """Converse of the above: an armed run whose watcher never fired has a full
    trajectory, so it gates strict-AND over every gating criterion -- arming a
    criterion must never change the verdict of a run it did not cut."""
    from coder_eval.orchestration.early_stop import EarlyStopWatcher

    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)
    orchestrator._early_stop_watcher = MagicMock(spec=EarlyStopWatcher)
    orchestrator._early_stop_watcher.info = None  # armed, but never fired
    orchestrator.result.iterations = [_make_turn_record(1)]
    orchestrator.success_checker.check_all_async = AsyncMock(  # type: ignore[union-attr]
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=0.0)]
    )
    # No patch on all_criteria_passed: the REAL strict-AND gate runs and must
    # reject score=0.0 against pass_threshold=0.9 on its own.
    with (
        patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)),
        patch.object(EvaluationResult, "armed_criteria_passed", autospec=True) as armed_gate,
    ):
        await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    armed_gate.assert_not_called()
    assert orchestrator.result.early_stop is None
    assert orchestrator.result.final_status == FinalStatus.TIMEOUT


@pytest.mark.asyncio
async def test_grade_after_forced_kill_regrades_when_existing_results_predate_the_last_turn(tmp_path) -> None:
    """Regression test (code-review finding): the skip-regrade shortcut must NOT
    fire on stale results.

    With ``simulation.check_criteria: every_turn``/``both``,
    ``_run_dialog_criteria_check`` replaces ``success_criteria_results`` on
    EVERY dialog turn. A ``TurnTimeoutError`` several turns later would then hit
    the already-graded branch and re-derive the final status from a snapshot
    taken before the turns that actually blew the budget -- reporting SUCCESS
    for a dialog whose later turns regressed the sandbox. The shortcut is only
    sound when the recorded grade covers the whole recorded trajectory.
    """
    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)
    fresh = [CriterionResult(criterion_type="file_exists", description="x", score=0.0)]
    orchestrator.success_checker.check_all_async = AsyncMock(return_value=fresh)  # type: ignore[union-attr]

    # A passing grade recorded when the trajectory was 1 turn long...
    orchestrator.result.success_criteria_results = [
        CriterionResult(criterion_type="file_exists", description="x", score=1.0)
    ]
    orchestrator._graded_iteration_count = 1
    # ...but two more turns have since been recorded.
    orchestrator.result.iterations = [_make_turn_record(i) for i in range(3)]

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        await orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT)

    orchestrator.success_checker.check_all_async.assert_awaited()  # type: ignore[union-attr]
    assert orchestrator.result.final_status == FinalStatus.TIMEOUT


@pytest.mark.asyncio
async def test_grade_after_forced_kill_falls_back_when_grading_exceeds_its_grace_budget(tmp_path, monkeypatch) -> None:
    """Grading after a forced kill must not itself become an unbounded tail on
    an already-blown budget -- bound it and fall back like any other grading
    failure."""
    from coder_eval import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "_GRADE_AFTER_FORCED_KILL_TIMEOUT_SECONDS", 0.05)

    task = _make_task()
    orchestrator = _make_initialized_orchestrator(task, tmp_path)

    async def hanging_check_all_async(*args, **kwargs):
        await asyncio.sleep(999)

    orchestrator.success_checker.check_all_async = hanging_check_all_async  # type: ignore[union-attr]

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        await asyncio.wait_for(orchestrator._grade_after_forced_kill(fallback_status=FinalStatus.TIMEOUT), timeout=5.0)

    assert orchestrator.result.final_status == FinalStatus.TIMEOUT
