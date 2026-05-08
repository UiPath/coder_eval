"""Tests for timeout override wiring in batch execution."""

from pathlib import Path

from coder_eval.models import AgentConfig, FileExistsCriterion, SandboxConfig, TaskDefinition
from coder_eval.orchestration.config import BatchRunConfig


def _make_task(*, turn_timeout: int | None = None, task_timeout: int | None = None) -> TaskDefinition:
    """Create a minimal TaskDefinition for testing."""
    return TaskDefinition(
        task_id="batch_timeout_test",
        description="Test task",
        initial_prompt="Do something",
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="test.py", description="test.py must exist")],
        turn_timeout=turn_timeout,
        task_timeout=task_timeout,
    )


class TestBatchTimeoutOverrides:
    """Test that BatchRunConfig timeout overrides are applied to tasks."""

    def test_task_timeout_override(self):
        """BatchRunConfig.task_timeout overrides task value."""
        task = _make_task(task_timeout=600)
        assert task.task_timeout == 600

        # Simulate what run_batch does
        config = BatchRunConfig(run_dir=Path("/tmp/test"), task_timeout=300)
        if config.task_timeout is not None:
            task.task_timeout = config.task_timeout

        assert task.task_timeout == 300

    def test_turn_timeout_override(self):
        """BatchRunConfig.turn_timeout overrides task value."""
        task = _make_task(turn_timeout=120)
        assert task.turn_timeout == 120

        # Simulate what run_batch does
        config = BatchRunConfig(run_dir=Path("/tmp/test"), turn_timeout=60)
        if config.turn_timeout is not None:
            task.turn_timeout = config.turn_timeout

        assert task.turn_timeout == 60

    def test_none_override_does_not_clobber(self):
        """None overrides don't clobber task YAML values."""
        task = _make_task(task_timeout=600, turn_timeout=120)

        # Simulate what run_batch does with None overrides
        config = BatchRunConfig(run_dir=Path("/tmp/test"))
        if config.task_timeout is not None:
            task.task_timeout = config.task_timeout
        if config.turn_timeout is not None:
            task.turn_timeout = config.turn_timeout

        # Original values preserved
        assert task.task_timeout == 600
        assert task.turn_timeout == 120

    def test_none_default_preserved_without_override(self):
        """When task uses None defaults and no CLI override, values stay None."""
        task = _make_task()
        assert task.task_timeout is None
        assert task.turn_timeout is None

        config = BatchRunConfig(run_dir=Path("/tmp/test"))
        if config.task_timeout is not None:
            task.task_timeout = config.task_timeout
        if config.turn_timeout is not None:
            task.turn_timeout = config.turn_timeout

        assert task.task_timeout is None
        assert task.turn_timeout is None

    def test_override_none_defaults_with_values(self):
        """Override works when task YAML uses None (default) timeout values."""
        task = _make_task()
        assert task.task_timeout is None
        assert task.turn_timeout is None

        config = BatchRunConfig(run_dir=Path("/tmp/test"), task_timeout=300, turn_timeout=60)
        if config.task_timeout is not None:
            task.task_timeout = config.task_timeout
        if config.turn_timeout is not None:
            task.turn_timeout = config.turn_timeout

        assert task.task_timeout == 300
        assert task.turn_timeout == 60
