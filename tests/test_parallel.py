"""Tests for parallel task execution."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from coder_eval.models import AgentState, ResolvedTask, TaskDefinition
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_sequential_mode(tmp_path, mocker):
    """Test that max_parallel=1 maintains sequential execution."""
    # Create a valid task file
    task_content = """
task_id: test_sequential
description: Test sequential execution
initial_prompt: "Test prompt"
agent:
  type: claude-code
sandbox:
  driver: tempdir
  python: null
max_iterations: 1
success_criteria:
  - type: file_exists
    path: test.txt
    description: "Check for test.txt"
"""
    task_file = tmp_path / "test_task.yaml"
    task_file.write_text(task_content)

    # Build a ResolvedTask from the task file
    task = TaskDefinition(
        task_id="test_sequential",
        description="Test sequential execution",
        initial_prompt="Test prompt",
        agent={"type": "claude-code"},
        sandbox={"driver": "tempdir"},
        max_iterations=1,
        success_criteria=[{"type": "file_exists", "path": "test.txt", "description": "Check for test.txt"}],
    )

    # Mock agent so we don't spawn the real claude CLI
    mock_agent = MagicMock()
    mock_agent.start = AsyncMock(side_effect=RuntimeError("mock agent crash"))
    mock_agent.stop = AsyncMock()
    mock_agent.get_state.return_value = AgentState.ERROR
    mocker.patch.object(Orchestrator, "_create_agent", new=AsyncMock(return_value=mock_agent))

    # Configure batch execution with sequential mode
    run_dir = tmp_path / "run"
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=1,  # Sequential
        preserve_sandbox=False,
        max_iterations=1,
    )

    resolved_task = ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=run_dir / "default" / "test_sequential" / "default",
        variant_id="default",
        original_task_id="test_sequential",
    )

    # This should complete without raising an exception
    # The task will fail (ERROR status) but that's expected - we're testing the execution flow
    summary, _results = await Orchestrator.run_batch([resolved_task], config)

    # Verify summary
    assert summary.tasks_run == 1
    assert run_dir.exists()

    # Verify run summary was created
    summary_file = run_dir / "run.json"
    assert summary_file.exists(), "Run summary should be created"


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency(tmp_path):
    """Test that semaphore actually limits concurrent tasks."""
    max_parallel = 2
    semaphore = asyncio.Semaphore(max_parallel)

    # Track concurrent executions
    concurrent_count = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    async def mock_task(task_id: int) -> dict:
        """Mock task that tracks concurrency."""
        nonlocal concurrent_count, max_concurrent

        async with lock:
            concurrent_count += 1
            if concurrent_count > max_concurrent:
                max_concurrent = concurrent_count

        # Simulate work
        await asyncio.sleep(0.1)

        async with lock:
            concurrent_count -= 1

        return {"task_id": f"task_{task_id}", "result": "success", "duration": 0.1}

    # Create wrapped tasks with semaphore
    async def run_with_sem(task_id: int):
        async with semaphore:
            return await mock_task(task_id)

    # Run 5 tasks with max_parallel=2
    tasks = [run_with_sem(i) for i in range(5)]
    results = await asyncio.gather(*tasks)

    # Verify results
    assert len(results) == 5
    assert max_concurrent <= max_parallel, f"Max concurrent was {max_concurrent}, limit was {max_parallel}"
    assert max_concurrent == max_parallel, "Semaphore should allow up to max_parallel tasks"


@pytest.mark.asyncio
async def test_parallel_with_exceptions():
    """Test that one task exception doesn't stop others."""
    semaphore = asyncio.Semaphore(3)

    async def failing_task():
        await asyncio.sleep(0.05)
        raise ValueError("Task failed!")

    async def successful_task(task_id: int):
        await asyncio.sleep(0.05)
        return {"task_id": f"task_{task_id}", "success": True}

    # Mix failing and successful tasks
    tasks = [
        successful_task(1),
        failing_task(),  # This should fail
        successful_task(2),
        failing_task(),  # This should also fail
        successful_task(3),
    ]

    # Wrap with semaphore
    async def run_with_sem(coro):
        async with semaphore:
            return await coro

    wrapped = [run_with_sem(task) for task in tasks]

    # Use return_exceptions=True to capture failures
    results = await asyncio.gather(*wrapped, return_exceptions=True)

    # Verify we got all 5 results (some errors, some success)
    assert len(results) == 5

    # Count successes and failures
    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 3, "Should have 3 successful tasks"
    assert len(failures) == 2, "Should have 2 failed tasks"

    # Verify failure types
    for failure in failures:
        assert isinstance(failure, ValueError)
        assert str(failure) == "Task failed!"


@pytest.mark.asyncio
async def test_parallel_timing():
    """Test that parallel execution is actually faster than sequential."""
    task_duration = 0.2  # 200ms per task
    num_tasks = 4

    async def slow_task(task_id: int):
        await asyncio.sleep(task_duration)
        return {"task_id": f"task_{task_id}", "success": True}

    # Sequential execution (max_parallel=1)
    semaphore_seq = asyncio.Semaphore(1)

    async def run_seq(task_id: int):
        async with semaphore_seq:
            return await slow_task(task_id)

    start_seq = time.time()
    await asyncio.gather(*[run_seq(i) for i in range(num_tasks)])
    sequential_time = time.time() - start_seq

    # Parallel execution (max_parallel=4)
    semaphore_par = asyncio.Semaphore(4)

    async def run_par(task_id: int):
        async with semaphore_par:
            return await slow_task(task_id)

    start_par = time.time()
    await asyncio.gather(*[run_par(i) for i in range(num_tasks)])
    parallel_time = time.time() - start_par

    # Sequential should take ~800ms (4 * 200ms)
    # Parallel should take ~200ms (all run concurrently)
    assert sequential_time >= (num_tasks * task_duration * 0.9), "Sequential execution should take sum of task times"
    assert parallel_time < sequential_time / 2, "Parallel should be at least 2x faster"
    assert parallel_time < task_duration * 1.5, "Parallel should take approximately one task duration"


# Note: test_error_result_creation removed - error handling now tested in test_orchestrator.py
# (see test_create_error_result)
