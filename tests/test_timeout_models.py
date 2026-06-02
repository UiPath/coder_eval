"""Tests for timeout-related model fields (now under RunLimits)."""

import pytest
from pydantic import ValidationError

from coder_eval.models import RunLimits, TaskDefinition
from coder_eval.orchestration.config import BatchRunConfig


class TestRunLimitsTurnTimeout:
    """Test turn_timeout on RunLimits (relocated from TaskDefinition in 2026-05-12)."""

    def _minimal_task(self, turn_timeout=None):
        defaults = {
            "task_id": "test",
            "description": "test task",
            "initial_prompt": "do something",
            "agent": {"type": "claude-code"},
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "test.py must exist"}],
        }
        if turn_timeout is not None:
            defaults["run_limits"] = RunLimits(turn_timeout=turn_timeout)
        return TaskDefinition(**defaults)

    def test_default_is_none(self):
        """run_limits defaults to None when no caps are set."""
        task = self._minimal_task()
        assert task.run_limits is None

    def test_valid_value(self):
        """Accepts valid turn_timeout >= 10."""
        task = self._minimal_task(turn_timeout=60)
        assert task.run_limits is not None
        assert task.run_limits.turn_timeout == 60

    def test_minimum_value(self):
        """Accepts minimum valid value of 10."""
        task = self._minimal_task(turn_timeout=10)
        assert task.run_limits is not None
        assert task.run_limits.turn_timeout == 10

    def test_below_minimum_rejected(self):
        """Rejects turn_timeout < 10."""
        with pytest.raises(ValidationError, match="greater than or equal to 10"):
            RunLimits(turn_timeout=5)


class TestRunLimitsTaskTimeout:
    """Test task_timeout on RunLimits."""

    def _minimal_task(self, task_timeout=None):
        defaults = {
            "task_id": "test",
            "description": "test task",
            "initial_prompt": "do something",
            "agent": {"type": "claude-code"},
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "test.py must exist"}],
        }
        if task_timeout is not None:
            defaults["run_limits"] = RunLimits(task_timeout=task_timeout)
        return TaskDefinition(**defaults)

    def test_default_is_none(self):
        """run_limits defaults to None when no caps are set."""
        task = self._minimal_task()
        assert task.run_limits is None

    def test_valid_value(self):
        """Accepts valid task_timeout >= 30."""
        task = self._minimal_task(task_timeout=300)
        assert task.run_limits is not None
        assert task.run_limits.task_timeout == 300

    def test_minimum_value(self):
        """Accepts minimum valid value of 30."""
        task = self._minimal_task(task_timeout=30)
        assert task.run_limits is not None
        assert task.run_limits.task_timeout == 30

    def test_below_minimum_rejected(self):
        """Rejects task_timeout < 30."""
        with pytest.raises(ValidationError, match="greater than or equal to 30"):
            RunLimits(task_timeout=10)


class TestTimeoutOverridesViaEngine:
    """Timeout overrides now flow through BatchRunConfig.overrides and the engine.

    The flat task_timeout/turn_timeout fields were collapsed into the generic
    `-D`/--set mechanism; below-minimum values are rejected at RunLimits
    reconstruction (and at the CLI by typer's `min=`).
    """

    def _task(self):
        return TaskDefinition(
            task_id="t",
            description="x",
            initial_prompt="do",
            agent={"type": "claude-code"},
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
        )

    def test_overrides_default_empty(self):
        from tests._path_helpers import tmp_subdir

        config = BatchRunConfig(run_dir=tmp_subdir("test"))
        assert config.overrides == {}

    def test_valid_values_apply(self):
        from coder_eval.orchestration.overrides import apply_overrides

        task = self._task()
        apply_overrides(task, {"run_limits.task_timeout": 300, "run_limits.turn_timeout": 60})
        assert task.run_limits is not None
        assert task.run_limits.task_timeout == 300
        assert task.run_limits.turn_timeout == 60

    def test_task_timeout_below_minimum_rejected(self):
        """Rejects task_timeout < 30, surfaced as a clean path-prefixed OverrideError."""
        from coder_eval.orchestration.overrides import OverrideError, apply_overrides

        with pytest.raises(OverrideError, match=r"-D run_limits\.task_timeout:.*greater than or equal to 30"):
            apply_overrides(self._task(), {"run_limits.task_timeout": 10})

    def test_turn_timeout_below_minimum_rejected(self):
        """Rejects turn_timeout < 10, surfaced as a clean path-prefixed OverrideError."""
        from coder_eval.orchestration.overrides import OverrideError, apply_overrides

        with pytest.raises(OverrideError, match=r"-D run_limits\.turn_timeout:.*greater than or equal to 10"):
            apply_overrides(self._task(), {"run_limits.turn_timeout": 3})
