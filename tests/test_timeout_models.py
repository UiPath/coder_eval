"""Tests for timeout-related model fields."""

import pytest
from pydantic import ValidationError

from coder_eval.models import TaskDefinition
from coder_eval.orchestration.config import BatchRunConfig


class TestTaskDefinitionTurnTimeout:
    """Test turn_timeout on TaskDefinition (moved from AgentConfig in 2026-05)."""

    def _minimal_task(self, **overrides):
        defaults = {
            "task_id": "test",
            "description": "test task",
            "initial_prompt": "do something",
            "agent": {"type": "claude-code"},
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "test.py must exist"}],
        }
        defaults.update(overrides)
        return TaskDefinition(**defaults)

    def test_default_is_none(self):
        """turn_timeout defaults to None (disabled)."""
        task = self._minimal_task()
        assert task.turn_timeout is None

    def test_valid_value(self):
        """Accepts valid turn_timeout >= 10."""
        task = self._minimal_task(turn_timeout=60)
        assert task.turn_timeout == 60

    def test_minimum_value(self):
        """Accepts minimum valid value of 10."""
        task = self._minimal_task(turn_timeout=10)
        assert task.turn_timeout == 10

    def test_below_minimum_rejected(self):
        """Rejects turn_timeout < 10."""
        with pytest.raises(ValidationError, match="greater than or equal to 10"):
            self._minimal_task(turn_timeout=5)

    def test_none_accepted(self):
        """Explicitly passing None is accepted."""
        task = self._minimal_task(turn_timeout=None)
        assert task.turn_timeout is None


class TestTaskDefinitionTaskTimeout:
    """Test task_timeout on TaskDefinition."""

    def _minimal_task(self, **overrides):
        """Create a minimal valid TaskDefinition."""
        defaults = {
            "task_id": "test",
            "description": "test task",
            "initial_prompt": "do something",
            "agent": {"type": "claude-code"},
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "test.py must exist"}],
        }
        defaults.update(overrides)
        return TaskDefinition(**defaults)

    def test_default_is_none(self):
        """task_timeout defaults to None (disabled)."""
        task = self._minimal_task()
        assert task.task_timeout is None

    def test_valid_value(self):
        """Accepts valid task_timeout >= 30."""
        task = self._minimal_task(task_timeout=300)
        assert task.task_timeout == 300

    def test_minimum_value(self):
        """Accepts minimum valid value of 30."""
        task = self._minimal_task(task_timeout=30)
        assert task.task_timeout == 30

    def test_below_minimum_rejected(self):
        """Rejects task_timeout < 30."""
        with pytest.raises(ValidationError, match="greater than or equal to 30"):
            self._minimal_task(task_timeout=10)

    def test_none_accepted(self):
        """Explicitly passing None is accepted."""
        task = self._minimal_task(task_timeout=None)
        assert task.task_timeout is None


class TestBatchRunConfigTimeouts:
    """Test timeout fields on BatchRunConfig."""

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
