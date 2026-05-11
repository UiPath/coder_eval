"""Tests for reference solution models."""

import pytest
from pydantic import ValidationError

from coder_eval.models import (
    AgentConfig,
    FileExistsCriterion,
    ReferenceComparisonCriterion,
    ReferenceSource,
    SandboxConfig,
    TaskDefinition,
)


class TestReferenceSource:
    """Tests for ReferenceSource model."""

    def test_code_only(self):
        """Can create reference with inline code."""
        ref = ReferenceSource(code="print('hello')")
        assert ref.code == "print('hello')"
        assert ref.file is None

    def test_file_only(self):
        """Can create reference with file path."""
        ref = ReferenceSource(file="reference/solution.py")
        assert ref.file == "reference/solution.py"
        assert ref.code is None

    def test_exclusive_source(self):
        """Cannot provide both code and file."""
        with pytest.raises(ValidationError, match="Only one of"):
            ReferenceSource(code="print('hello')", file="ref.py")

    def test_requires_source(self):
        """Must provide at least one source."""
        with pytest.raises(ValidationError, match="One of"):
            ReferenceSource()


class TestTaskDefinition:
    """Tests for TaskDefinition with reference field."""

    def test_task_with_inline_reference(self):
        """Task can have inline reference code."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test file exists")],
            reference=ReferenceSource(code="print('reference')"),
        )

        assert task.reference is not None
        assert task.reference.code == "print('reference')"
        assert task.reference.file is None

    def test_task_with_file_reference(self):
        """Task can have file-based reference."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test file exists")],
            reference=ReferenceSource(file="references/solution.py"),
        )

        assert task.reference is not None
        assert task.reference.file == "references/solution.py"
        assert task.reference.code is None

    def test_task_without_reference(self):
        """Task can exist without reference (optional)."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test file exists")],
        )

        assert task.reference is None


class TestReferenceComparisonCriterion:
    """Tests for simplified ReferenceComparisonCriterion."""

    def test_minimal_criterion(self):
        """Can create criterion with just agent_file."""
        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
        )

        assert criterion.agent_file == "solution.py"
        assert criterion.comparison_method == "ast"  # default
        assert criterion.similarity_threshold == 0.8  # default

    def test_custom_comparison_method(self):
        """Can specify comparison method."""
        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            comparison_method="token",
        )

        assert criterion.comparison_method == "token"

    def test_custom_threshold(self):
        """Can specify custom similarity threshold."""
        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            similarity_threshold=0.9,
        )

        assert criterion.similarity_threshold == 0.9

    def test_threshold_validation(self):
        """Threshold must be between 0 and 1."""
        with pytest.raises(ValidationError):
            ReferenceComparisonCriterion(
                description="Compare against reference",
                agent_file="solution.py",
                similarity_threshold=1.5,
            )

        with pytest.raises(ValidationError):
            ReferenceComparisonCriterion(
                description="Compare against reference",
                agent_file="solution.py",
                similarity_threshold=-0.1,
            )
