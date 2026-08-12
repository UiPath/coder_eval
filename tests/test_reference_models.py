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


def _task(**overrides):
    """A minimal valid TaskDefinition, with per-test overrides."""
    kwargs = {
        "task_id": "test",
        "description": "Test task",
        "initial_prompt": "Do something",
        "agent": parse_agent_config(type="claude-code"),
        "sandbox": SandboxConfig(driver="tempdir"),
        "success_criteria": [FileExistsCriterion(path="test.py", description="Test file exists")],
    }
    kwargs.update(overrides)
    return TaskDefinition(**kwargs)


class TestReferenceSource:
    """Tests for ReferenceSource model."""

    def test_directory_only(self):
        """A reference is a directory path relative to the task YAML."""
        ref = ReferenceSource(directory="reference/")
        assert ref.directory == "reference/"

    def test_requires_directory(self):
        """`directory` is required — there is no inline/file form any more."""
        with pytest.raises(ValidationError, match="directory"):
            ReferenceSource()  # type: ignore[call-arg]

    def test_rejects_blank_directory(self):
        with pytest.raises(ValidationError, match="non-empty path"):
            ReferenceSource(directory="   ")

    def test_code_and_file_forms_removed(self):
        """The old string forms are rejected outright, not silently ignored.

        A task YAML carried over from the string-reference era must fail loudly:
        silently dropping `code:` would run the judge with no reference at all.
        """
        # Match a DISTINCTIVE fragment of the migration message, not just the
        # field name: ``extra="forbid"`` already emits "code -- Extra inputs are
        # not permitted", so asserting `"code" in ...` stayed green even with the
        # _reject_removed_string_forms validator deleted -- i.e. the test passed
        # while the actionable migration guidance it exists for was gone.
        migration = "was removed — a reference is now always a DIRECTORY"
        with pytest.raises(ValidationError, match=migration):
            ReferenceSource(code="print('hello')")  # type: ignore[call-arg]

        with pytest.raises(ValidationError, match=migration):
            ReferenceSource(file="ref.py")  # type: ignore[call-arg]

        # And the message must point somewhere.
        with pytest.raises(ValidationError) as excinfo:
            ReferenceSource(code="print('hello')")  # type: ignore[call-arg]
        assert "reference: {directory: <dir>}" in str(excinfo.value)

    def test_reference_source_forbids_extras(self):
        """A typo like ``directry`` names the misspelled field in the error."""
        with pytest.raises(ValidationError) as excinfo:
            ReferenceSource(directory="ok/", directry="foo/")  # type: ignore[call-arg]
        assert "directry" in str(excinfo.value)
        assert "extra" in str(excinfo.value).lower()

    def test_reference_source_typo_in_yaml_load(self, tmp_path):
        """The strict mode also triggers on the loader path, not just kwargs."""
        import yaml

        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("directry: foo/\n", encoding="utf-8")
        data = yaml.safe_load(bad_yaml.read_text(encoding="utf-8"))
        with pytest.raises(ValidationError) as excinfo:
            ReferenceSource(**data)
        assert "directry" in str(excinfo.value)


class TestTaskDefinition:
    """Tests for TaskDefinition with reference field."""

    def test_task_with_directory_reference(self):
        task = _task(reference=ReferenceSource(directory="references/"))
        assert task.reference is not None
        assert task.reference.directory == "references/"

    def test_task_without_reference(self):
        """Task can exist without reference (optional)."""
        assert _task().reference is None

    def test_reference_comparison_without_reference_block_is_rejected(self):
        """A consumer with no reference would silently score 0.0 — fail at load."""
        with pytest.raises(ValidationError, match="consume the reference solution"):
            _task(
                success_criteria=[
                    ReferenceComparisonCriterion(
                        description="Compare",
                        agent_file="solution.py",
                        reference_file="solution.py",
                    )
                ]
            )

    def test_include_reference_default_without_reference_block_is_allowed(self):
        """include_reference defaults to True and silently no-ops with no reference —
        most judge tasks have no reference at all, so this must not be an error."""
        from coder_eval.models import LLMJudgeCriterion

        task = _task(success_criteria=[LLMJudgeCriterion(description="Judge", prompt="grade it")])
        assert task.reference is None

    def test_reference_dir_token_in_files_without_reference_block_is_rejected(self):
        """`$REFERENCE_DIR/...` in a judge's files: needs a reference: block."""
        from coder_eval.models import LLMJudgeCriterion

        with pytest.raises(ValidationError, match="consume the reference solution"):
            _task(
                success_criteria=[
                    LLMJudgeCriterion(
                        description="Judge",
                        prompt="grade it",
                        files=["$REFERENCE_DIR/rubric.md"],
                    )
                ]
            )

    @pytest.mark.parametrize(
        "command",
        [
            'diff -r "$REFERENCE_DIR" out/',
            'diff -r "${REFERENCE_DIR}" out/',  # brace form: standard shell, missed by a substring test
            "cat $REFERENCE_DIR/solution.py",
        ],
    )
    def test_run_command_using_reference_dir_without_reference_block_is_rejected(self, command):
        """With no reference the env var is simply absent, so the command runs
        with an empty argument and misbehaves instead of failing."""
        from coder_eval.models import RunCommandCriterion

        with pytest.raises(ValidationError, match="consume the reference solution"):
            _task(success_criteria=[RunCommandCriterion(description="cmp", command=command)])

    def test_unrelated_variable_with_the_same_prefix_is_not_a_consumer(self):
        """$REFERENCE_DIRECTORY is a different variable; a raw substring test
        hard-failed load on it."""
        from coder_eval.models import RunCommandCriterion

        task = _task(success_criteria=[RunCommandCriterion(description="x", command="echo $REFERENCE_DIRECTORY")])
        assert task.reference is None

    def test_reference_consumer_with_reference_block_is_accepted(self):
        task = _task(
            reference=ReferenceSource(directory="references/"),
            success_criteria=[
                ReferenceComparisonCriterion(
                    description="Compare",
                    agent_file="solution.py",
                    reference_file="solution.py",
                )
            ],
        )
        assert task.reference is not None


class TestReferenceComparisonCriterion:
    """Tests for simplified ReferenceComparisonCriterion."""

    def test_minimal_criterion(self):
        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="solution.py",
        )

        assert criterion.agent_file == "solution.py"
        assert criterion.reference_file == "solution.py"
        assert criterion.comparison_method == "ast"  # default
        assert criterion.similarity_threshold == 0.8  # default

    def test_reference_file_is_required(self):
        """Without it there is no way to pick a file out of the reference dir."""
        with pytest.raises(ValidationError, match="reference_file"):
            ReferenceComparisonCriterion(  # type: ignore[call-arg]
                description="Compare against reference",
                agent_file="solution.py",
            )

    def test_custom_comparison_method(self):
        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="solution.py",
            comparison_method="token",
        )

        assert criterion.comparison_method == "token"

    def test_custom_threshold(self):
        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="solution.py",
            similarity_threshold=0.9,
        )

        assert criterion.similarity_threshold == 0.9

    def test_threshold_validation(self):
        """Threshold must be between 0 and 1."""
        for bad in (1.5, -0.1):
            with pytest.raises(ValidationError):
                ReferenceComparisonCriterion(
                    description="Compare against reference",
                    agent_file="solution.py",
                    reference_file="solution.py",
                    similarity_threshold=bad,
                )
