"""Tests for reference solution models."""

import pytest
from pydantic import ValidationError

from coder_eval.models import (
    FileExistsCriterion,
    ReferenceComparisonCriterion,
    ReferenceSource,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
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

    def test_reference_source_forbids_extras(self):
        """A typo like ``directry`` raises ValidationError before model_validator runs.

        Without ``extra='forbid'`` the typo would land in ``__pydantic_extra__`` and
        the check_exclusive_source validator would then raise its generic 'One of'
        message — concealing the actual mistake. With strict mode the user sees the
        misspelled field name directly.
        """
        with pytest.raises(ValidationError) as excinfo:
            ReferenceSource(code="x", directry="foo/")  # type: ignore[call-arg]
        assert "directry" in str(excinfo.value)
        assert "extra" in str(excinfo.value).lower()

    def test_reference_source_typo_in_yaml_load(self, tmp_path):
        """Loading a task YAML with ``directry:`` surfaces the typo with the actual key.

        End-to-end check that the strict mode triggers when the field comes from
        the loader path, not just direct kwargs.
        """
        import yaml

        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(
            "code: null\ndirectry: foo/\n",
            encoding="utf-8",
        )
        data = yaml.safe_load(bad_yaml.read_text(encoding="utf-8"))
        with pytest.raises(ValidationError) as excinfo:
            ReferenceSource(**data)
        assert "directry" in str(excinfo.value)


class TestTaskDefinition:
    """Tests for TaskDefinition with reference field."""

    def test_task_with_inline_reference(self):
        """Task can have inline reference code."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
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
            agent=parse_agent_config(type="claude-code"),
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
            agent=parse_agent_config(type="claude-code"),
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
