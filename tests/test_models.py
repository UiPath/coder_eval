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


class TestSandboxConfigValidation:
    """Tests for SandboxConfig validation logic."""

    def test_multiple_repo_sources_rejected(self):
        """Test that multiple RepoSource entries are rejected."""
        with pytest.raises(ValueError, match="Only one RepoSource is allowed"):
            SandboxConfig(
                driver="tempdir",
                python_version="3.13",
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
                python_version="3.13",
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
                python_version="3.13",
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
                python_version="3.13",
                template_sources=[
                    TemplateDirSource(path=str(Path(tmpdir) / "template")),
                    RepoSource(url="https://github.com/user/repo.git"),
                ],
            )
