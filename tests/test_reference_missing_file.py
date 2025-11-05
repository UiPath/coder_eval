"""Tests for reference file loading error handling.

Tests ensure clear FileNotFoundError for missing reference files.
"""

import logging

import pytest

from coder_eval.models import AgentConfig, FileExistsCriterion, ReferenceSource, SandboxConfig, TaskDefinition
from coder_eval.orchestration.evaluation import load_reference_code


def test_load_reference_missing_file_raises(tmp_path):
    """Test that missing reference file raises FileNotFoundError with task context.

    Hypothesis: Missing reference files should raise clear errors.
    Expected: FileNotFoundError with file path and task file in message.

    Context: Lines 552-553 in orchestrator.py check ref_path.exists().
    """
    # Create task file path (doesn't need to exist, just for reference)
    task_file = tmp_path / "task.yaml"

    # Create task with reference to non-existent file
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="output.txt", description="Check output exists")],
        reference=ReferenceSource(file="missing_reference.py"),  # File doesn't exist
    )

    # Attempt to load reference - should raise FileNotFoundError
    with pytest.raises(FileNotFoundError) as exc_info:
        load_reference_code(task, task_file, cached_reference=None, logger=logging.getLogger(__name__))

    # Verify error message contains both file path and task file
    error_msg = str(exc_info.value)
    assert "missing_reference.py" in error_msg
    assert "task.yaml" in error_msg or str(task_file) in error_msg


def test_load_reference_inline_code_works(tmp_path):
    """Test that inline reference code loads successfully.

    Hypothesis: Inline code should not require file I/O.
    Expected: Reference code returned directly from task definition.
    """
    task_file = tmp_path / "task.yaml"

    # Create task with inline reference code
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="output.txt", description="Check output exists")],
        reference=ReferenceSource(code="def solution():\n    return 42"),
    )

    # Load reference - should succeed
    ref_code, _ = load_reference_code(task, task_file, cached_reference=None, logger=logging.getLogger(__name__))

    assert ref_code == "def solution():\n    return 42"


def test_load_reference_existing_file_works(tmp_path):
    """Test that existing reference file loads successfully.

    Hypothesis: Valid file paths should load file content.
    Expected: File content returned.
    """
    # Create task file
    task_file = tmp_path / "task.yaml"

    # Create reference file next to task file
    ref_file = tmp_path / "reference_solution.py"
    ref_file.write_text("def solution():\n    return 'success'")

    # Create task with reference to existing file
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="output.txt", description="Check output exists")],
        reference=ReferenceSource(file="reference_solution.py"),
    )

    # Load reference - should succeed
    ref_code, _ = load_reference_code(task, task_file, cached_reference=None, logger=logging.getLogger(__name__))

    assert ref_code == "def solution():\n    return 'success'"


def test_load_reference_caches_result(tmp_path):
    """Test that reference code is cached after first load.

    Hypothesis: Multiple calls should not re-read file.
    Expected: Same result returned without additional file I/O.
    """
    task_file = tmp_path / "task.yaml"
    ref_file = tmp_path / "reference.py"
    ref_file.write_text("original content")

    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="output.txt", description="Check output exists")],
        reference=ReferenceSource(file="reference.py"),
    )

    # First load
    ref_code_1, cached = load_reference_code(task, task_file, cached_reference=None, logger=logging.getLogger(__name__))
    assert ref_code_1 == "original content"

    # Modify file on disk
    ref_file.write_text("modified content")

    # Second load - should return cached value
    ref_code_2, _ = load_reference_code(task, task_file, cached_reference=cached, logger=logging.getLogger(__name__))
    assert ref_code_2 == "original content"  # Still returns cached value


def test_load_reference_no_reference_returns_none(tmp_path):
    """Test that task without reference returns None.

    Hypothesis: Optional reference field should be handled gracefully.
    Expected: None returned when reference not defined.
    """
    task_file = tmp_path / "task.yaml"

    # Create task without reference
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="output.txt", description="Check output exists")],
        reference=None,
    )

    # Load reference - should return None
    ref_code, _ = load_reference_code(task, task_file, cached_reference=None, logger=logging.getLogger(__name__))

    assert ref_code is None


def test_load_reference_without_task_file_raises(tmp_path):
    """Test that reference file loading without task_file raises ValueError.

    Hypothesis: File paths need task_file for resolution.
    Expected: ValueError when task_file is None.

    Context: Lines 549-550 in orchestrator.py check task_file exists.
    """
    # Create task with file reference
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="output.txt", description="Check output exists")],
        reference=ReferenceSource(file="reference.py"),
    )

    # Attempt to load reference - should raise ValueError
    with pytest.raises(ValueError, match="task_file not set"):
        load_reference_code(task, task_file=None, cached_reference=None, logger=logging.getLogger(__name__))
