"""Tests for the data models."""

import tempfile
from pathlib import Path

import pytest
import yaml

from coder_eval.models import RepoSource, SandboxConfig, TaskDefinition, TemplateDirSource


def test_load_hello_date_task():
    """Test that the hello_date.yaml task can be loaded."""
    task_file = Path("tasks/hello_date.yaml")
    assert task_file.exists(), "Task file should exist"

    with open(task_file) as f:
        task_data = yaml.safe_load(f)

    # This will raise an error if validation fails
    task = TaskDefinition(**task_data)

    # Basic assertions
    assert task.task_id == "hello_date_smoke_test"
    assert task.max_iterations == 2
    assert task.agent.type == "claude-code"
    assert task.sandbox.driver == "tempdir"
    assert len(task.success_criteria) == 3
    assert task.llm_reviewer.enabled is False


def test_success_criterion_discriminated_union():
    """Test that success criteria are properly discriminated."""
    from coder_eval.models import (
        FileContainsCriterion,
        FileExistsCriterion,
        RunCommandCriterion,
    )

    # Test file_exists
    criterion = FileExistsCriterion(path="test.py", description="Test file")
    assert criterion.type == "file_exists"

    # Test file_contains
    criterion = FileContainsCriterion(path="test.py", includes=["import"], description="Test file")
    assert criterion.type == "file_contains"

    # Test run_command
    criterion = RunCommandCriterion(command="python test.py", description="Run test")
    assert criterion.type == "run_command"


class TestAgentConfig:
    """Tests for AgentConfig fields."""

    def test_max_turns_default_none(self):
        """Test that max_turns defaults to None."""
        from coder_eval.models import AgentConfig, AgentKind

        config = AgentConfig(type=AgentKind.CLAUDE_CODE)
        assert config.max_turns is None

    def test_max_turns_set_from_yaml(self):
        """Test that max_turns can be set (e.g., from task YAML)."""
        from coder_eval.models import AgentConfig, AgentKind

        config = AgentConfig(type=AgentKind.CLAUDE_CODE, max_turns=3)
        assert config.max_turns == 3

    def test_invalid_permission_mode_assignment_rejected(self):
        """Test that assigning invalid permission_mode via attribute raises ValidationError."""
        from pydantic import ValidationError

        from coder_eval.models import AgentConfig, AgentKind

        config = AgentConfig(type=AgentKind.CLAUDE_CODE)
        with pytest.raises(ValidationError):
            config.permission_mode = "foobar"

    def test_valid_permission_mode_assignment_accepted(self):
        """Test that assigning valid permission_mode via attribute works."""
        from coder_eval.models import AgentConfig, AgentKind

        config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="default")
        config.permission_mode = "bypassPermissions"
        assert config.permission_mode == "bypassPermissions"


class TestMultiAgentTaskDefinition:
    """Tests for multi-agent task configuration."""

    def _base_task_data(self) -> dict:
        return {
            "task_id": "test_task",
            "description": "Test",
            "initial_prompt": "Do something",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "exists"}],
        }

    def test_single_agent_works_unchanged(self):
        data = self._base_task_data()
        data["agent"] = {"type": "claude-code"}
        task = TaskDefinition(**data)
        assert task.agent.type == "claude-code"
        assert task.agents is None

    def test_multi_agent_list(self):
        data = self._base_task_data()
        data["agents"] = [
            {"name": "fast", "type": "claude-code", "permission_mode": "bypassPermissions"},
            {"name": "careful", "type": "claude-code", "permission_mode": "acceptEdits"},
        ]
        task = TaskDefinition(**data)
        assert task.agents is not None
        assert len(task.agents) == 2
        assert task.agents[0].name == "fast"
        assert task.agents[1].name == "careful"
        # agent is None for multi-agent tasks (not synthesized)
        assert task.agent is None

    def test_both_agent_and_agents_raises(self):
        from pydantic import ValidationError

        data = self._base_task_data()
        data["agent"] = {"type": "claude-code"}
        data["agents"] = [
            {"name": "a", "type": "claude-code"},
            {"name": "b", "type": "claude-code"},
        ]
        with pytest.raises(ValidationError, match="Only one of 'agent' or 'agents'"):
            TaskDefinition(**data)

    def test_single_agent_in_agents_list_raises(self):
        from pydantic import ValidationError

        data = self._base_task_data()
        data["agents"] = [{"name": "only", "type": "claude-code"}]
        with pytest.raises(ValidationError, match="at least 2 entries"):
            TaskDefinition(**data)

    def test_agents_missing_name_raises(self):
        from pydantic import ValidationError

        data = self._base_task_data()
        data["agents"] = [
            {"type": "claude-code"},
            {"name": "b", "type": "claude-code"},
        ]
        with pytest.raises(ValidationError, match="must have a 'name'"):
            TaskDefinition(**data)

    def test_agents_duplicate_names_raises(self):
        from pydantic import ValidationError

        data = self._base_task_data()
        data["agents"] = [
            {"name": "same", "type": "claude-code"},
            {"name": "same", "type": "claude-code"},
        ]
        with pytest.raises(ValidationError, match="unique"):
            TaskDefinition(**data)

    def test_task_id_with_double_underscore_raises(self):
        from pydantic import ValidationError

        data = self._base_task_data()
        data["task_id"] = "bad__id"
        data["agent"] = {"type": "claude-code"}
        with pytest.raises(ValidationError, match="'__'"):
            TaskDefinition(**data)


class TestExpandTaskForAgents:
    """Tests for expand_task_for_agents helper."""

    def _make_multi_agent_task(self) -> TaskDefinition:
        return TaskDefinition(  # type: ignore[call-arg]
            **{
                "task_id": "multi_task",
                "description": "Test",
                "initial_prompt": "Do it",
                "agents": [
                    {"name": "fast", "type": "claude-code", "permission_mode": "bypassPermissions"},
                    {"name": "careful", "type": "claude-code", "permission_mode": "acceptEdits"},
                ],
                "sandbox": {"driver": "tempdir"},
                "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "exists"}],
            }
        )

    def test_single_agent_task_unchanged(self):
        from coder_eval.orchestration.task_loader import expand_task_for_agents

        task = TaskDefinition(  # type: ignore[call-arg]
            **{
                "task_id": "single_task",
                "description": "Test",
                "initial_prompt": "Do it",
                "agent": {"type": "claude-code"},
                "sandbox": {"driver": "tempdir"},
                "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "exists"}],
            }
        )
        result = expand_task_for_agents(task)
        assert result == [task]

    def test_multi_agent_task_expands(self):
        from coder_eval.orchestration.task_loader import expand_task_for_agents

        task = self._make_multi_agent_task()
        result = expand_task_for_agents(task)

        assert len(result) == 2
        assert result[0].task_id == "multi_task"
        assert result[1].task_id == "multi_task"
        assert result[0].agent.name == "fast"
        assert result[1].agent.name == "careful"
        assert result[0].agent.permission_mode == "bypassPermissions"
        assert result[1].agent.permission_mode == "acceptEdits"

    def test_expanded_tasks_have_no_agents_list(self):
        from coder_eval.orchestration.task_loader import expand_task_for_agents

        task = self._make_multi_agent_task()
        for expanded in expand_task_for_agents(task):
            assert expanded.agents is None

    def test_expanded_tasks_share_same_task_id(self):
        from coder_eval.orchestration.task_loader import expand_task_for_agents

        task = self._make_multi_agent_task()
        ids = [t.task_id for t in expand_task_for_agents(task)]
        assert ids == ["multi_task", "multi_task"]


class TestSandboxConfigValidation:
    """Tests for SandboxConfig validation logic."""

    def test_multiple_repo_sources_rejected(self):
        """Test that multiple RepoSource entries are rejected."""
        with pytest.raises(ValueError, match="Only one RepoSource is allowed"):
            SandboxConfig(
                driver="tempdir",
                template_sources=[
                    RepoSource(url="https://github.com/user/repo1.git"),
                    RepoSource(url="https://github.com/user/repo2.git"),
                ],
            )

    def test_multiple_repo_sources_with_other_templates_rejected(self):
        """Test that multiple RepoSource entries are rejected even with other sources."""
        with tempfile.TemporaryDirectory() as tmpdir, pytest.raises(ValueError, match="Only one RepoSource is allowed"):
            SandboxConfig(
                driver="tempdir",
                template_sources=[
                    RepoSource(url="https://github.com/user/repo1.git"),
                    TemplateDirSource(path=str(Path(tmpdir) / "template")),
                    RepoSource(url="https://github.com/user/repo2.git"),
                ],
            )

    def test_single_repo_source_first_is_valid(self):
        """Test that a single RepoSource as the first element is valid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = SandboxConfig(
                driver="tempdir",
                template_sources=[
                    RepoSource(url="https://github.com/user/repo.git"),
                    TemplateDirSource(path=str(Path(tmpdir) / "template")),
                ],
            )
            # Should not raise
            assert len(config.template_sources) == 2
            assert isinstance(config.template_sources[0], RepoSource)

    def test_repo_source_not_first_rejected(self):
        """Test that RepoSource must be the first element."""
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            pytest.raises(ValueError, match="RepoSource must be the first element"),
        ):
            SandboxConfig(
                driver="tempdir",
                template_sources=[
                    TemplateDirSource(path=str(Path(tmpdir) / "template")),
                    RepoSource(url="https://github.com/user/repo.git"),
                ],
            )
