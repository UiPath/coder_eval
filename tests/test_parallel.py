"""Tests for parallel task execution."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.models import AgentState, PreservationMode, ResolvedTask, TaskDefinition
from coder_eval.orchestration.batch import run_batch
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_sequential_mode(tmp_path):
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
        success_criteria=[{"type": "file_exists", "path": "test.txt", "description": "Check for test.txt"}],
    )

    # Mock agent so we don't spawn the real claude CLI
    mock_agent = MagicMock()
    mock_agent.start = AsyncMock(side_effect=RuntimeError("mock agent crash"))
    mock_agent.stop = AsyncMock()
    mock_agent.get_state.return_value = AgentState.ERROR

    # Configure batch execution with sequential mode
    run_dir = tmp_path / "run"
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=1,  # Sequential
        preservation_mode=PreservationMode.NONE,
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
    with patch.object(Orchestrator, "_create_agent", new=AsyncMock(return_value=mock_agent)):
        summary, _results = await run_batch([resolved_task], config)

    # Verify summary
    assert summary.tasks_run == 1
    assert run_dir.exists()

    # Verify run summary was created
    summary_file = run_dir / "run.json"
    assert summary_file.exists(), "Run summary should be created"


@pytest.mark.asyncio
async def test_orchestrator_run_dir_is_the_resolved_logging_dir(tmp_path):
    """Orchestrator's run_dir is whatever logging_dir_template resolved to for this
    task -- ResolvedTask.run_dir, verbatim.

    There is deliberately NO workspace_dir special case here any more. "Flat" used to
    be a mode this seam detected (effective_run_dir = config.run_dir when
    workspace_dir was set, so Harbor's task.json landed at a predictable path); it is
    now simply what a static logging template resolves to, which the companion test
    below pins. Patches Orchestrator itself (rather than driving a full run) so the
    assertion is on the value this seam computes.
    """
    task = TaskDefinition(
        task_id="test_workspace_dir",
        description="Test workspace_dir flat run_dir",
        initial_prompt="Test prompt",
        agent={"type": "claude-code"},
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "test.txt", "description": "Check for test.txt"}],
    )
    task_file = tmp_path / "test_task.yaml"
    task_file.write_text("task_id: test_workspace_dir\n")

    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=1,
        preservation_mode=PreservationMode.NONE,
        workspace_dir=workspace_dir,
    )

    nested_run_dir = run_dir / "default" / "test_workspace_dir" / "default"
    resolved_task = ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=nested_run_dir,
        variant_id="default",
        original_task_id="test_workspace_dir",
    )

    mock_orchestrator = MagicMock()
    mock_orchestrator.run = AsyncMock(return_value=MagicMock(duration_seconds=0.0))
    mock_orchestrator_cls = MagicMock(return_value=mock_orchestrator)

    with patch("coder_eval.orchestrator.Orchestrator", mock_orchestrator_cls):
        await run_batch([resolved_task], config)

    assert mock_orchestrator_cls.call_count == 1
    assert mock_orchestrator_cls.call_args.kwargs["run_dir"] == nested_run_dir, (
        "Orchestrator's run_dir must be the task's resolved logging dir, not config.run_dir"
    )


@pytest.mark.asyncio
async def test_a_static_logging_template_makes_the_run_dir_flat(tmp_path):
    """The replacement for the deleted effective_run_dir special case: Harbor gets a
    flat task.json path by RESOLVING a static logging template to it, not by the
    orchestration seam noticing workspace_dir and substituting config.run_dir."""
    from coder_eval.path_utils import resolve_dir_template

    assert resolve_dir_template(
        "/logs/agent", run_dir=tmp_path / "run", variant_id="default", task_id="t", replicate_index=0
    ) == Path("/logs/agent")


@pytest.mark.asyncio
async def test_artifacts_dir_template_resolved_and_threaded_into_orchestrator(tmp_path):
    """--artifacts-dir reaches Orchestrator already RESOLVED to a concrete path.

    A static template (no placeholders) resolves to itself -- the identity case that
    lets Harbor pass a literal container path with no special-casing anywhere."""
    task = TaskDefinition(
        task_id="test_capture_workspace",
        description="Test capture_workspace threading",
        initial_prompt="Test prompt",
        agent={"type": "claude-code"},
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "test.txt", "description": "Check for test.txt"}],
    )
    task_file = tmp_path / "test_task.yaml"
    task_file.write_text("task_id: test_capture_workspace\n")

    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=1,
        preservation_mode=PreservationMode.NONE,
        workspace_dir=workspace_dir,
        artifacts_dir_template="/work/output",
    )

    resolved_task = ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=run_dir / "default" / "test_capture_workspace" / "default",
        variant_id="default",
        original_task_id="test_capture_workspace",
    )

    mock_orchestrator = MagicMock()
    mock_orchestrator.run = AsyncMock(return_value=MagicMock(duration_seconds=0.0))
    mock_orchestrator_cls = MagicMock(return_value=mock_orchestrator)

    with patch("coder_eval.orchestrator.Orchestrator", mock_orchestrator_cls):
        await run_batch([resolved_task], config)

    assert mock_orchestrator_cls.call_args.kwargs["artifacts_dir"] == Path("/work/output")


@pytest.mark.asyncio
async def test_artifacts_dir_template_rejected_for_docker_driver(tmp_path):
    """A non-default artifacts_dir_template is a no-op for driver: docker (the in-container
    Orchestrator has no way to receive it) -- run_batch refuses it loud rather than silently
    ignoring the override, mirroring --workspace-dir's existing docker rejection."""
    task = TaskDefinition(
        task_id="test_docker_artifacts",
        description="Test artifacts_dir_template + docker rejection",
        initial_prompt="Test prompt",
        agent={"type": "claude-code"},
        sandbox={"driver": "docker", "docker": {"image": "coder-eval-agent"}},
        success_criteria=[{"type": "file_exists", "path": "test.txt", "description": "Check for test.txt"}],
    )
    task_file = tmp_path / "test_task.yaml"
    task_file.write_text("task_id: test_docker_artifacts\n")

    run_dir = tmp_path / "run"
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=1,
        preservation_mode=PreservationMode.NONE,
        artifacts_dir_template="/work/output",
    )

    resolved_task = ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=run_dir / "default" / "test_docker_artifacts" / "default",
        variant_id="default",
        original_task_id="test_docker_artifacts",
    )

    with pytest.raises(ValueError, match=r"--artifacts-dir is not for sandbox\.driver: docker"):
        await run_batch([resolved_task], config)


@pytest.mark.asyncio
async def test_logging_dir_template_rejected_for_docker_driver(tmp_path):
    """A non-default logging_dir_template is rejected for driver: docker, mirroring
    --artifacts-dir's rejection -- the container's real artifacts land under the
    resolved logging dir (DockerRunner's output_dir), so an override desyncs it from
    the artifacts_dir_template default that clear_rerun_artifacts/--resume rely on."""
    task = TaskDefinition(
        task_id="test_docker_logging",
        description="Test logging_dir_template + docker rejection",
        initial_prompt="Test prompt",
        agent={"type": "claude-code"},
        sandbox={"driver": "docker", "docker": {"image": "coder-eval-agent"}},
        success_criteria=[{"type": "file_exists", "path": "test.txt", "description": "Check for test.txt"}],
    )
    task_file = tmp_path / "test_task.yaml"
    task_file.write_text("task_id: test_docker_logging\n")

    run_dir = tmp_path / "run"
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=1,
        preservation_mode=PreservationMode.NONE,
        logging_dir_template="${run_dir}/flat/${task}",
    )

    resolved_task = ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=run_dir / "flat" / "test_docker_logging",
        variant_id="default",
        original_task_id="test_docker_logging",
    )

    with pytest.raises(ValueError, match=r"--logging-dir is not for sandbox\.driver: docker"):
        await run_batch([resolved_task], config)


def _make_resolved_task(tmp_path: Path, task_id: str, run_dir: Path) -> ResolvedTask:
    task = TaskDefinition(
        task_id=task_id,
        description="Test multi-task collision",
        initial_prompt="Test prompt",
        agent={"type": "claude-code"},
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "test.txt", "description": "Check for test.txt"}],
    )
    task_file = tmp_path / f"{task_id}.yaml"
    task_file.write_text(f"task_id: {task_id}\n")
    return ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=run_dir,
        variant_id="default",
        original_task_id=task_id,
    )


@pytest.mark.asyncio
async def test_static_logging_template_rejected_for_multiple_tasks(tmp_path):
    """A logging_dir_template that resolves every task to the SAME directory is refused
    for a multi-task run -- each task's task.json/task.log would overwrite the last."""
    run_dir = tmp_path / "run"
    config = BatchRunConfig(run_dir=run_dir, max_parallel=1, logging_dir_template="${run_dir}/flat")
    tasks = [
        _make_resolved_task(tmp_path, "task_a", run_dir / "flat"),
        _make_resolved_task(tmp_path, "task_b", run_dir / "flat"),
    ]

    with pytest.raises(ValueError, match=r"--logging-dir's template resolves two or more"):
        await run_batch(tasks, config)


@pytest.mark.asyncio
async def test_static_artifacts_template_rejected_for_multiple_tasks(tmp_path):
    """Same collision guard, for --artifacts-dir: distinct logging dirs but a shared
    artifacts_dir_template still collides on the artifacts side."""
    run_dir = tmp_path / "run"
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=1,
        artifacts_dir_template="${run_dir}/shared-artifacts",
    )
    tasks = [
        _make_resolved_task(tmp_path, "task_a", run_dir / "default" / "task_a" / "00"),
        _make_resolved_task(tmp_path, "task_b", run_dir / "default" / "task_b" / "00"),
    ]

    with pytest.raises(ValueError, match=r"--artifacts-dir's template resolves two or more"):
        await run_batch(tasks, config)


@pytest.mark.asyncio
async def test_dir_templates_default_to_todays_layout(tmp_path):
    """The defaults must reproduce the historical layout exactly, so an unspecified run
    writes to byte-identical paths."""
    cfg = BatchRunConfig(run_dir=tmp_path / "run")
    assert cfg.logging_dir_template == "${run_dir}/${variant}/${task}/${repeat}"
    assert cfg.artifacts_dir_template == "${run_dir}/${variant}/${task}/${repeat}/artifacts/${task}"


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
