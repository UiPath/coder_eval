"""Tests for streaming callback integration in batch execution."""

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from coder_eval.models import ResolvedTask, TaskDefinition
from coder_eval.orchestration.batch import run_batch
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
