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
