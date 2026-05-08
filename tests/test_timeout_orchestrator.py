"""Tests for timeout behavior in the orchestrator."""

import asyncio
import contextlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors.timeout import TaskTimeoutError, TurnTimeoutError
from coder_eval.models import (
    AgentConfig,
    AgentKind,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
    TurnRecord,
)
from coder_eval.orchestrator import Orchestrator


def _make_task(*, turn_timeout: float | None = None, task_timeout: float | None = None):
    """Create a minimal TaskDefinition for testing.

    Uses model_construct() to bypass Pydantic's ge= validators when setting
    sub-minimum timeout values needed for fast tests.
    """
    agent = AgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="timeout_test",
        description="Test task",
        initial_prompt="Do something",
        tags=[],
        agent=agent,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="test.py", description="test.py must exist")],
        max_turns=None,
        turn_timeout=turn_timeout,
        task_timeout=task_timeout,
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

    orchestrator.success_checker.check_all = MagicMock(  # type: ignore[union-attr]
        return_value=[CriterionResult(criterion_type="file_exists", description="test", score=1.0)]
    )

    with patch("coder_eval.orchestrator.load_reference_code", return_value=(None, None)):
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
    mock_checker.check_all = MagicMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="x", score=1.0)]
    )
    orchestrator.success_checker = mock_checker

    # Skip executor backoff sleeps so the test stays fast.
    async def fast_retry_sleep(delay: float) -> None:
        return None

    with (
        patch("coder_eval.orchestrator.load_reference_code", return_value=(None, None)),
        patch("asyncio.sleep", side_effect=fast_retry_sleep),
    ):
        success = await orchestrator._evaluation_loop()

    assert success is True
    assert timeouts_seen == [1.0, 1.0], "every attempt must receive turn_timeout fresh"
    # Result.turns: partial (from on_attempt_error) + success (from main flow).
    assert len(orchestrator.result.turns) == 2
    assert orchestrator.result.turns[0].crashed is True
    assert orchestrator.result.turns[1].crashed is False


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
        patch("coder_eval.orchestrator.load_reference_code", return_value=(None, None)),
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
    from coder_eval.models import AgentConfig, AgentKind, TurnRecord

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
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
