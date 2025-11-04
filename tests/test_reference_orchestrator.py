"""Tests for orchestrator reference loading."""

import pytest

from coder_eval.models import (
    AgentConfig,
    FileExistsCriterion,
    ReferenceSource,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestrator import Orchestrator


class TestOrchestratorReference:
    """Tests for orchestrator reference code loading."""

    def test_load_inline_reference(self, tmp_path):
        """Load inline reference code."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(code="print('hello')"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        orch = Orchestrator(task, run_dir, preserve_sandbox=False, task_file=None)
        reference = orch._load_reference_code()

        assert reference == "print('hello')"

    def test_load_file_reference(self, tmp_path):
        """Load reference from file."""
        # Create reference file
        ref_file = tmp_path / "reference.py"
        ref_file.write_text("print('from file')")

        # Create task file
        task_file = tmp_path / "task.yaml"
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(file="reference.py"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        orch = Orchestrator(task, run_dir, preserve_sandbox=False, task_file=task_file)
        reference = orch._load_reference_code()

        assert reference == "print('from file')"

    def test_reference_caching(self, tmp_path):
        """Reference code is cached after first load."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(code="test"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        orch = Orchestrator(task, run_dir, preserve_sandbox=False, task_file=None)

        ref1 = orch._load_reference_code()
        ref2 = orch._load_reference_code()

        # Same object (cached)
        assert ref1 is ref2

    def test_no_reference(self, tmp_path):
        """Returns None when no reference defined."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        orch = Orchestrator(task, run_dir, preserve_sandbox=False, task_file=None)
        reference = orch._load_reference_code()

        assert reference is None

    def test_file_reference_not_found(self, tmp_path):
        """Error when reference file doesn't exist."""
        task_file = tmp_path / "task.yaml"
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(file="nonexistent.py"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        orch = Orchestrator(task, run_dir, preserve_sandbox=False, task_file=task_file)

        with pytest.raises(FileNotFoundError, match="Reference file not found"):
            orch._load_reference_code()

    def test_file_reference_without_task_file(self, tmp_path):
        """Error when file reference but no task_file provided."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(file="reference.py"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        orch = Orchestrator(task, run_dir, preserve_sandbox=False, task_file=None)

        with pytest.raises(ValueError, match="task_file not set"):
            orch._load_reference_code()
