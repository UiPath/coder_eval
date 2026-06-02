"""Tests for timeout override wiring in batch execution.

After 2026-05-12, the canonical home for ``max_turns`` / ``task_timeout`` /
``turn_timeout`` is ``run_limits.*``. ``BatchRunConfig`` keeps its flat
shape (CLI flag surface) and patches into ``run_limits`` via field merge —
the same wiring used in ``_apply_cli_overrides``.
"""

from coder_eval.models import (
    FileExistsCriterion,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.config import BatchRunConfig
from tests._path_helpers import tmp_subdir


def _make_task(*, turn_timeout: int | None = None, task_timeout: int | None = None) -> TaskDefinition:
    """Create a minimal TaskDefinition for testing."""
    run_limits: RunLimits | None = None
    if turn_timeout is not None or task_timeout is not None:
        run_limits = RunLimits(turn_timeout=turn_timeout, task_timeout=task_timeout)
    return TaskDefinition(
        task_id="batch_timeout_test",
        description="Test task",
        initial_prompt="Do something",
        agent=parse_agent_config(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="test.py", description="test.py must exist")],
        run_limits=run_limits,
    )


def _apply_timeouts(task: TaskDefinition, config: BatchRunConfig) -> None:
    """Apply BatchRunConfig.overrides timeout entries via the override engine."""
    from coder_eval.orchestration.overrides import apply_overrides

    apply_overrides(task, config.overrides)


class TestBatchTimeoutOverrides:
    """Test that BatchRunConfig timeout overrides field-merge into task.run_limits."""

    def test_task_timeout_override(self):
        """A run_limits.task_timeout override replaces the task value."""
        task = _make_task(task_timeout=600)
        assert task.run_limits is not None
        assert task.run_limits.task_timeout == 600

        config = BatchRunConfig(run_dir=tmp_subdir("test"), overrides={"run_limits.task_timeout": 300})
        _apply_timeouts(task, config)

        assert task.run_limits.task_timeout == 300

    def test_turn_timeout_override(self):
        """A run_limits.turn_timeout override replaces the task value."""
        task = _make_task(turn_timeout=120)
        assert task.run_limits is not None
        assert task.run_limits.turn_timeout == 120

        config = BatchRunConfig(run_dir=tmp_subdir("test"), overrides={"run_limits.turn_timeout": 60})
        _apply_timeouts(task, config)

        assert task.run_limits.turn_timeout == 60

    def test_none_override_does_not_clobber(self):
        """Empty overrides don't clobber task YAML values."""
        task = _make_task(task_timeout=600, turn_timeout=120)

        config = BatchRunConfig(run_dir=tmp_subdir("test"))
        _apply_timeouts(task, config)

        assert task.run_limits is not None
        assert task.run_limits.task_timeout == 600
        assert task.run_limits.turn_timeout == 120

    def test_none_default_preserved_without_override(self):
        """When task uses None defaults and no override, run_limits stays None."""
        task = _make_task()
        assert task.run_limits is None

        config = BatchRunConfig(run_dir=tmp_subdir("test"))
        _apply_timeouts(task, config)

        assert task.run_limits is None

    def test_override_none_defaults_with_values(self):
        """Override works when task YAML had no run_limits at all."""
        task = _make_task()
        assert task.run_limits is None

        config = BatchRunConfig(
            run_dir=tmp_subdir("test"),
            overrides={"run_limits.task_timeout": 300, "run_limits.turn_timeout": 60},
        )
        _apply_timeouts(task, config)

        assert task.run_limits is not None
        assert task.run_limits.task_timeout == 300
        assert task.run_limits.turn_timeout == 60
