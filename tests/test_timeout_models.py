"""Tests for timeout-related model fields."""

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentConfig, TaskDefinition
from coder_eval.orchestration.config import BatchRunConfig


class TestAgentConfigTurnTimeout:
    """Test turn_timeout_seconds on AgentConfig."""

    def test_default_is_none(self):
        """turn_timeout_seconds defaults to None (disabled)."""
        config = AgentConfig(type="claude-code")
        assert config.turn_timeout_seconds is None

    def test_valid_value(self):
        """Accepts valid turn_timeout_seconds >= 10."""
        config = AgentConfig(type="claude-code", turn_timeout_seconds=60)
        assert config.turn_timeout_seconds == 60

    def test_minimum_value(self):
        """Accepts minimum valid value of 10."""
        config = AgentConfig(type="claude-code", turn_timeout_seconds=10)
        assert config.turn_timeout_seconds == 10

    def test_below_minimum_rejected(self):
        """Rejects turn_timeout_seconds < 10."""
        with pytest.raises(ValidationError, match="greater than or equal to 10"):
            AgentConfig(type="claude-code", turn_timeout_seconds=5)

    def test_none_accepted(self):
        """Explicitly passing None is accepted."""
        config = AgentConfig(type="claude-code", turn_timeout_seconds=None)
        assert config.turn_timeout_seconds is None


class TestTaskDefinitionTaskTimeout:
    """Test task_timeout_seconds on TaskDefinition."""

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
        """task_timeout_seconds defaults to None (disabled)."""
        task = self._minimal_task()
        assert task.task_timeout_seconds is None

    def test_valid_value(self):
        """Accepts valid task_timeout_seconds >= 30."""
        task = self._minimal_task(task_timeout_seconds=300)
        assert task.task_timeout_seconds == 300

    def test_minimum_value(self):
        """Accepts minimum valid value of 30."""
        task = self._minimal_task(task_timeout_seconds=30)
        assert task.task_timeout_seconds == 30

    def test_below_minimum_rejected(self):
        """Rejects task_timeout_seconds < 30."""
        with pytest.raises(ValidationError, match="greater than or equal to 30"):
            self._minimal_task(task_timeout_seconds=10)

    def test_none_accepted(self):
        """Explicitly passing None is accepted."""
        task = self._minimal_task(task_timeout_seconds=None)
        assert task.task_timeout_seconds is None


class TestBatchRunConfigTimeouts:
    """Test timeout fields on BatchRunConfig."""

    def test_defaults_are_none(self):
        """Both timeout overrides default to None."""
        from pathlib import Path

        config = BatchRunConfig(run_dir=Path("/tmp/test"))
        assert config.task_timeout_seconds is None
        assert config.turn_timeout_seconds is None

    def test_valid_values(self):
        """Accepts valid timeout overrides."""
        from pathlib import Path

        config = BatchRunConfig(
            run_dir=Path("/tmp/test"),
            task_timeout_seconds=300,
            turn_timeout_seconds=60,
        )
        assert config.task_timeout_seconds == 300
        assert config.turn_timeout_seconds == 60

    def test_task_timeout_below_minimum_rejected(self):
        """Rejects task_timeout_seconds < 30."""
        from pathlib import Path

        with pytest.raises(ValidationError, match="greater than or equal to 30"):
            BatchRunConfig(run_dir=Path("/tmp/test"), task_timeout_seconds=10)

    def test_turn_timeout_below_minimum_rejected(self):
        """Rejects turn_timeout_seconds < 10."""
        from pathlib import Path

        with pytest.raises(ValidationError, match="greater than or equal to 10"):
            BatchRunConfig(run_dir=Path("/tmp/test"), turn_timeout_seconds=3)
