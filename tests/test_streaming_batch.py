"""Tests for streaming callback integration in batch execution."""

import inspect
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.models import AgentKind, EvaluationResult, FinalStatus, ResolvedTask, TaskDefinition
from coder_eval.orchestration.batch import _create_error_task_result, run_batch
from coder_eval.orchestration.config import BatchRunConfig


def test_batch_run_accepts_stream_callback_factory():
    """run_batch accepts a stream_callback_factory parameter."""
    sig = inspect.signature(run_batch)
    assert "stream_callback_factory" in sig.parameters


def test_batch_config_no_stream_mode():
    """BatchRunConfig does not carry stream_mode (handled at CLI level)."""
    assert "stream_mode" not in BatchRunConfig.model_fields


@pytest.mark.asyncio
async def test_run_batch_reraises_keyboard_interrupt_from_gather(tmp_path: Path):
    """run_batch should re-raise KeyboardInterrupt when a task raises it."""
    task = TaskDefinition(
        task_id="test-task",
        description="A test task",
        initial_prompt="Do something",
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "out.txt", "description": "File exists"}],
    )
    resolved = [
        ResolvedTask(
            task=task,
            task_file=tmp_path / "task.yaml",
            run_dir=tmp_path / "run" / "v1" / "test-task" / "00",
            variant_id="v1",
            replicate_index=0,
        ),
    ]
    config = BatchRunConfig(run_dir=tmp_path / "run", max_parallel=1)

    # Mock asyncio.gather to return a KeyboardInterrupt as one of the results
    # (this simulates what happens with return_exceptions=True when a task raises KeyboardInterrupt)
    async def fake_gather(*coros, **kwargs):
        # Close the coroutines to avoid "was never awaited" warnings
        for coro in coros:
            coro.close()
        return [KeyboardInterrupt()]

    with patch("asyncio.gather", side_effect=fake_gather), pytest.raises(KeyboardInterrupt):
        await run_batch(resolved, config)


def test_create_error_task_result_carries_replicate_index(tmp_path: Path):
    """_create_error_task_result must thread replicate_index into the returned TaskResult."""
    tr = _create_error_task_result(
        tmp_path / "task.yaml",
        RuntimeError("boom"),
        task_id="my-task",
        variant_id="v1",
        replicate_index=2,
    )
    assert tr.replicate_index == 2


@pytest.mark.asyncio
async def test_run_single_carries_replicate_index_on_happy_path(tmp_path: Path):
    """TaskResult.replicate_index is populated from ResolvedTask in the happy path."""
    task = TaskDefinition(
        task_id="rep-task",
        description="A test task",
        initial_prompt="Do something",
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "out.txt", "description": "File exists"}],
    )
    resolved = [
        ResolvedTask(
            task=task,
            task_file=tmp_path / "task.yaml",
            run_dir=tmp_path / "run" / "v1" / "rep-task" / "01",
            variant_id="v1",
            replicate_index=1,
        ),
    ]
    config = BatchRunConfig(run_dir=tmp_path / "run", max_parallel=1)

    fake_result = EvaluationResult(
        task_id="rep-task",
        task_description="A test task",
        variant_id="v1",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        environment_info={},
        weighted_score=1.0,
    )

    with patch("coder_eval.orchestrator.Orchestrator") as mock_orch_cls:
        mock_orch_inst = mock_orch_cls.return_value
        mock_orch_inst.run = AsyncMock(return_value=fake_result)
        _, task_results = await run_batch(resolved, config)

    assert len(task_results) == 1
    assert task_results[0].replicate_index == 1
