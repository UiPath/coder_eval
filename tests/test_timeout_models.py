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


class TestBatchRunConfigTimeouts:
    """Test timeout override fields on BatchRunConfig (CLI flags)."""

    def test_defaults_are_none(self):
        """Both timeout overrides default to None."""
        from pathlib import Path

        config = BatchRunConfig(run_dir=Path("/tmp/test"))
        assert config.task_timeout is None
        assert config.turn_timeout is None

    def test_valid_values(self):
        """Accepts valid timeout overrides."""
        from pathlib import Path

        config = BatchRunConfig(
            run_dir=Path("/tmp/test"),
            task_timeout=300,
            turn_timeout=60,
        )
        assert config.task_timeout == 300
        assert config.turn_timeout == 60

    def test_task_timeout_below_minimum_rejected(self):
        """Rejects task_timeout < 30."""
        from pathlib import Path

        with pytest.raises(ValidationError, match="greater than or equal to 30"):
            BatchRunConfig(run_dir=Path("/tmp/test"), task_timeout=10)

    def test_turn_timeout_below_minimum_rejected(self):
        """Rejects turn_timeout < 10."""
        from pathlib import Path

        with pytest.raises(ValidationError, match="greater than or equal to 10"):
            BatchRunConfig(run_dir=Path("/tmp/test"), turn_timeout=3)
