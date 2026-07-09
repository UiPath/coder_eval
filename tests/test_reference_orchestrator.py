"""Tests for orchestrator reference loading."""

import pytest

from coder_eval.models import (
    FileExistsCriterion,
    ReferenceSource,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.evaluation import load_reference_code


class TestOrchestratorReference:
    """Tests for orchestrator reference code loading."""

    def test_load_inline_reference(self, tmp_path):
        """Load inline reference code."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(code="print('hello')"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        reference, _ = load_reference_code(task, task_file=None, cached_reference=None)

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
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(file="reference.py"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        reference, _ = load_reference_code(task, task_file=task_file, cached_reference=None)

        assert reference == "print('from file')"

    def test_reference_caching(self, tmp_path):
        """Reference code is cached after first load."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(code="test"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ref1, cached = load_reference_code(task, task_file=None, cached_reference=None)
        ref2, _ = load_reference_code(task, task_file=None, cached_reference=cached)

        # Same object (cached)
        assert ref1 is ref2

    def test_no_reference(self, tmp_path):
        """Returns None when no reference defined."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        reference, _ = load_reference_code(task, task_file=None, cached_reference=None)

        assert reference is None

    def test_file_reference_not_found(self, tmp_path):
        """Error when reference file doesn't exist."""
        task_file = tmp_path / "task.yaml"
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(file="nonexistent.py"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="Reference file not found"):
            load_reference_code(task, task_file=task_file, cached_reference=None)

    def test_file_reference_without_task_file(self, tmp_path):
        """Error when file reference but no task_file provided."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(file="reference.py"),
        )

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises(ValueError, match="task_file not set"):
            load_reference_code(task, task_file=None, cached_reference=None)


class TestOrchestratorReferenceDirectory:
    """Tests for the directory form of ReferenceSource."""

    def test_load_directory_reference(self, tmp_path):
        """Load a directory-form reference: returns the resolved path, no string content."""
        from coder_eval.orchestration.evaluation import load_reference

        ref_dir = tmp_path / "ref"
        ref_dir.mkdir()
        (ref_dir / "main.py").write_text("print('from dir')")

        task_file = tmp_path / "task.yaml"
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(directory="ref"),
        )

        code, dir_path, cache = load_reference(task, task_file=task_file, cached_reference=None)

        assert code is None
        assert dir_path is not None
        assert dir_path.resolve() == ref_dir.resolve()
        # No string-form cache for directory references.
        assert cache is None

    def test_directory_reference_not_found(self, tmp_path):
        """Error when reference directory doesn't exist."""
        from coder_eval.orchestration.evaluation import load_reference

        task_file = tmp_path / "task.yaml"
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(directory="nonexistent_dir"),
        )

        with pytest.raises(FileNotFoundError, match="Reference directory not found"):
            load_reference(task, task_file=task_file, cached_reference=None)

    def test_directory_reference_without_task_file(self, tmp_path):
        """Error when directory reference but no task_file provided to resolve relative path."""
        from coder_eval.orchestration.evaluation import load_reference

        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(directory="ref"),
        )

        with pytest.raises(ValueError, match="task_file not set"):
            load_reference(task, task_file=None, cached_reference=None)

    def test_load_reference_legacy_two_tuple_alias(self, tmp_path):
        """``load_reference_code`` keeps returning the 2-tuple for back-compat."""
        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="test.py", description="Test")],
            reference=ReferenceSource(code="hello"),
        )

        result = load_reference_code(task, task_file=None, cached_reference=None)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result[0] == "hello"


class TestReferenceSourceExclusivity:
    """Tests for the ReferenceSource exclusivity validator across all three options."""

    def test_rejects_code_and_directory(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Only one of"):
            ReferenceSource(code="x", directory="ref")

    def test_rejects_file_and_directory(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Only one of"):
            ReferenceSource(file="ref.py", directory="ref")

    def test_rejects_all_three(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Only one of"):
            ReferenceSource(code="x", file="ref.py", directory="ref")

    def test_rejects_none(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="One of"):
            ReferenceSource()

    def test_accepts_directory_alone(self):
        # No exception → validator accepts directory-only.
        rs = ReferenceSource(directory="ref")
        assert rs.directory == "ref"
        assert rs.code is None
        assert rs.file is None
