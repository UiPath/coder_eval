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
        agent=AgentConfig(type="claude-code", turn_timeout_seconds=turn_timeout),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="test.py", description="test.py must exist")],
        task_timeout_seconds=task_timeout,
    )


class TestBatchTimeoutOverrides:
    """Test that BatchRunConfig timeout overrides are applied to tasks."""

    def test_task_timeout_override(self):
        """BatchRunConfig.task_timeout_seconds overrides task value."""
        task = _make_task(task_timeout=600)
        assert task.task_timeout_seconds == 600

        # Simulate what run_batch does
        config = BatchRunConfig(run_dir=Path("/tmp/test"), task_timeout_seconds=300)
        if config.task_timeout_seconds is not None:
            task.task_timeout_seconds = config.task_timeout_seconds

        assert task.task_timeout_seconds == 300

    def test_turn_timeout_override(self):
        """BatchRunConfig.turn_timeout_seconds overrides task agent value."""
        task = _make_task(turn_timeout=120)
        assert task.agent.turn_timeout_seconds == 120

        # Simulate what run_batch does
        config = BatchRunConfig(run_dir=Path("/tmp/test"), turn_timeout_seconds=60)
        if config.turn_timeout_seconds is not None:
            task.agent.turn_timeout_seconds = config.turn_timeout_seconds

        assert task.agent.turn_timeout_seconds == 60

    def test_none_override_does_not_clobber(self):
        """None overrides don't clobber task YAML values."""
        task = _make_task(task_timeout=600, turn_timeout=120)

        # Simulate what run_batch does with None overrides
        config = BatchRunConfig(run_dir=Path("/tmp/test"))
        if config.task_timeout_seconds is not None:
            task.task_timeout_seconds = config.task_timeout_seconds
        if config.turn_timeout_seconds is not None:
            task.agent.turn_timeout_seconds = config.turn_timeout_seconds

        # Original values preserved
        assert task.task_timeout_seconds == 600
        assert task.agent.turn_timeout_seconds == 120

    def test_none_default_preserved_without_override(self):
        """When task uses None defaults and no CLI override, values stay None."""
        task = _make_task()
        assert task.task_timeout_seconds is None
        assert task.agent.turn_timeout_seconds is None

        config = BatchRunConfig(run_dir=Path("/tmp/test"))
        if config.task_timeout_seconds is not None:
            task.task_timeout_seconds = config.task_timeout_seconds
        if config.turn_timeout_seconds is not None:
            task.agent.turn_timeout_seconds = config.turn_timeout_seconds

        assert task.task_timeout_seconds is None
        assert task.agent.turn_timeout_seconds is None

    def test_override_none_defaults_with_values(self):
        """Override works when task YAML uses None (default) timeout values."""
        task = _make_task()
        assert task.task_timeout_seconds is None
        assert task.agent.turn_timeout_seconds is None

        config = BatchRunConfig(run_dir=Path("/tmp/test"), task_timeout_seconds=300, turn_timeout_seconds=60)
        if config.task_timeout_seconds is not None:
            task.task_timeout_seconds = config.task_timeout_seconds
        if config.turn_timeout_seconds is not None:
            task.agent.turn_timeout_seconds = config.turn_timeout_seconds

        assert task.task_timeout_seconds == 300
        assert task.agent.turn_timeout_seconds == 60
