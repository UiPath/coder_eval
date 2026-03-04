"""Tests for timeout behavior in the orchestrator."""

import asyncio
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


def _make_task(*, turn_timeout: int | None = None, task_timeout: int | None = None, max_iterations: int = 3):
    """Create a minimal TaskDefinition for testing.

    Uses model_construct() to bypass Pydantic's ge= validators when setting
    sub-minimum timeout values needed for fast tests.
    """
    agent = AgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        max_turns=None,
        turn_timeout_seconds=turn_timeout,
        additional_ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="timeout_test",
        description="Test task",
        initial_prompt="Do something",
        max_iterations=max_iterations,
        tags=[],
        agent=agent,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="test.py", description="test.py must exist")],
        task_timeout_seconds=task_timeout,
        llm_reviewer=None,
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


@pytest.mark.asyncio
async def test_turn_timeout_fires(tmp_path):
    """Turn timeout fires when agent.communicate() is slow."""
    task = _make_task(turn_timeout=1)  # 1 second timeout (bypasses ge=10 via model_construct)
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir)

    # Set up mocks as if _setup() ran
    orchestrator.result = EvaluationResult(
        task_id="timeout_test",
        task_description="Test",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Mock agent that sleeps longer than the turn timeout
    mock_agent = AsyncMock()

    async def slow_communicate(_prompt, **kwargs):
        await asyncio.sleep(10)
        return _make_turn_record()

    mock_agent.communicate = slow_communicate
    orchestrator.agent = mock_agent

    # Mock sandbox
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    # Mock success checker
    mock_checker = MagicMock()
    orchestrator.success_checker = mock_checker

    # Run the evaluation loop — should raise TurnTimeoutError
    with pytest.raises(TurnTimeoutError) as exc_info:
        await orchestrator._evaluation_loop()

    assert exc_info.value.layer == "turn"
    assert exc_info.value.timeout_seconds == 1
    assert exc_info.value.iteration == 1


@pytest.mark.asyncio
async def test_task_timeout_fires(tmp_path):
    """Task timeout fires when the overall evaluation loop takes too long."""
    task = _make_task(task_timeout=1, max_iterations=10)  # 1 second task timeout
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir)

    # Mock _setup to be a no-op
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]

    # Mock _cleanup to be a no-op
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    # Mock _evaluation_loop to be slow
    async def slow_loop():
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    result = await orchestrator.run()
    assert result.final_status == "ERROR"
    assert "Task timed out" in (result.error_message or "")


@pytest.mark.asyncio
async def test_task_timeout_populates_elapsed_seconds(tmp_path):
    """TaskTimeoutError includes elapsed_seconds when raised by the orchestrator."""
    task = _make_task(task_timeout=1, max_iterations=10)  # 1 second task timeout
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir)

    # Mock _setup to be a no-op
    orchestrator._setup = AsyncMock()  # type: ignore[method-assign]

    # Mock _cleanup to be a no-op
    orchestrator._cleanup = AsyncMock()  # type: ignore[method-assign]

    # Mock _evaluation_loop to be slow
    async def slow_loop():
        await asyncio.sleep(10)
        return False

    orchestrator._evaluation_loop = slow_loop  # type: ignore[method-assign]

    # Intercept the TaskTimeoutError to verify elapsed_seconds is populated
    with patch("coder_eval.orchestrator.create_error_context") as mock_create:
        mock_create.return_value = {}
        result = await orchestrator.run()

    assert result.final_status == "ERROR"
    assert "Task timed out" in (result.error_message or "")

    # Verify create_error_context received a TaskTimeoutError with elapsed_seconds
    error_arg = mock_create.call_args.kwargs["error"]
    assert isinstance(error_arg, TaskTimeoutError)
    assert error_arg.elapsed_seconds is not None
    assert error_arg.elapsed_seconds > 0


@pytest.mark.asyncio
async def test_no_timeout_when_none(tmp_path):
    """No timeout wrapping when timeouts are None (default)."""
    task = _make_task(max_iterations=1)  # defaults: turn=None, task=None
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir)

    # Set up result
    orchestrator.result = EvaluationResult(
        task_id="timeout_test",
        task_description="Test",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Mock fast agent
    mock_agent = AsyncMock()
    mock_agent.communicate = AsyncMock(return_value=_make_turn_record())
    orchestrator.agent = mock_agent

    # Mock sandbox
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    # Mock success checker that always passes
    mock_checker = MagicMock()
    mock_checker.check_all = MagicMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="test", score=1.0)]
    )
    orchestrator.success_checker = mock_checker

    # Should complete successfully without timeout
    with patch("coder_eval.orchestrator.load_reference_code", return_value=(None, None)):
        success = await orchestrator._evaluation_loop()

    assert success is True


@pytest.mark.asyncio
async def test_turn_timeout_fires_before_task_timeout(tmp_path):
    """Turn timeout (inner) fires before task timeout (outer) when a single turn is slow."""
    task = _make_task(turn_timeout=1, task_timeout=60)  # Turn: 1s, Task: 60s
    run_dir = tmp_path / "run" / "timeout_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir)

    # Set up result
    orchestrator.result = EvaluationResult(
        task_id="timeout_test",
        task_description="Test",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Mock agent that sleeps longer than turn timeout but within task timeout
    mock_agent = AsyncMock()

    async def slow_communicate(_prompt, **kwargs):
        await asyncio.sleep(10)
        return _make_turn_record()

    mock_agent.communicate = slow_communicate
    orchestrator.agent = mock_agent

    # Mock sandbox
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    mock_checker = MagicMock()
    orchestrator.success_checker = mock_checker

    # Should raise TurnTimeoutError (not TaskTimeoutError)
    with pytest.raises(TurnTimeoutError):
        await orchestrator._evaluation_loop()
