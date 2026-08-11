"""Tests for evaluator reference-directory support."""

import pytest

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    ReferenceComparisonCriterion,
    SandboxConfig,
)
from coder_eval.sandbox import Sandbox


@pytest.fixture
def sandbox():
    config = SandboxConfig(driver="tempdir", python=None)
    sb = Sandbox(config, "test")
    sb.setup()
    yield sb
    sb.cleanup(preserve=False)


def _reference_dir(tmp_path, **files: str):
    ref = tmp_path / "ref"
    ref.mkdir(exist_ok=True)
    for name, content in files.items():
        (ref / name.replace("__", "/")).write_text(content, encoding="utf-8")
    return ref


class TestSuccessCheckerReference:
    """Tests for SuccessChecker with a reference directory."""

    def test_check_all_accepts_reference_dir(self, sandbox, tmp_path):
        checker = SuccessChecker(sandbox)
        results = checker.check_all([], reference_dir=_reference_dir(tmp_path))
        assert results == []

    @pytest.mark.asyncio
    async def test_check_all_async_persists_reference_dir(self, sandbox, tmp_path):
        """check_all_async — the orchestrator's actual entry point — must persist
        reference_dir the same way the sync check_all does, so a later
        check()/check_all() call on the same checker without an explicit
        reference still sees it."""
        checker = SuccessChecker(sandbox)
        reference_dir = _reference_dir(tmp_path)

        results = await checker.check_all_async([], reference_dir=reference_dir)
        assert results == []
        assert checker._reference_dir == reference_dir

    def test_reference_comparison_without_reference_dir(self, sandbox):
        """reference_comparison scores 0 when no reference directory is set."""
        (sandbox.sandbox_dir / "solution.py").write_text("def foo(): pass")

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="solution.py",
        )

        result = SuccessChecker(sandbox).check(criterion)

        assert result.score == 0.0
        assert "No reference directory provided" in result.error

    def test_reference_comparison_with_reference(self, sandbox, tmp_path):
        code = "def hello():\n    return 'world'"
        (sandbox.sandbox_dir / "solution.py").write_text(code)

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="solution.py",
            comparison_method="token",
        )

        result = SuccessChecker(sandbox).check(
            criterion, reference_dir=_reference_dir(tmp_path, **{"solution.py": code})
        )

        # Identical code should score 1.0
        assert result.score == 1.0
        assert result.error is None

    def test_reference_comparison_reads_a_nested_reference_file(self, sandbox, tmp_path):
        """reference_file is a path INSIDE the reference dir, not just a basename."""
        code = "def hello():\n    return 'world'"
        (sandbox.sandbox_dir / "solution.py").write_text(code)
        ref = _reference_dir(tmp_path)
        (ref / "src").mkdir()
        (ref / "src" / "solution.py").write_text(code, encoding="utf-8")

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="src/solution.py",
            comparison_method="token",
        )

        result = SuccessChecker(sandbox).check(criterion, reference_dir=ref)
        assert result.score == 1.0

    def test_reference_comparison_rejects_traversal_out_of_reference_dir(self, sandbox, tmp_path):
        """`reference_file` names a file of the solution; escaping is always a bug."""
        (sandbox.sandbox_dir / "solution.py").write_text("def foo(): pass")
        (tmp_path / "outside.py").write_text("def foo(): pass", encoding="utf-8")

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="../outside.py",
        )

        result = SuccessChecker(sandbox).check(criterion, reference_dir=_reference_dir(tmp_path))

        assert result.score == 0.0
        assert "escapes the reference directory" in result.error

    def test_reference_comparison_missing_reference_file(self, sandbox, tmp_path):
        (sandbox.sandbox_dir / "solution.py").write_text("def foo(): pass")

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="absent.py",
        )

        result = SuccessChecker(sandbox).check(criterion, reference_dir=_reference_dir(tmp_path))

        assert result.score == 0.0
        assert "Failed to read reference file" in result.error

    def test_reference_comparison_agent_file_missing(self, sandbox, tmp_path):
        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="nonexistent.py",
            reference_file="solution.py",
        )

        result = SuccessChecker(sandbox).check(
            criterion, reference_dir=_reference_dir(tmp_path, **{"solution.py": "def foo(): pass"})
        )

        assert result.score == 0.0
        assert "Agent file not found" in result.error

    def test_reference_comparison_ast_method(self, sandbox, tmp_path):
        code = "def foo():\n    return 42"
        (sandbox.sandbox_dir / "solution.py").write_text(code)

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            reference_file="solution.py",
            comparison_method="ast",
        )

        result = SuccessChecker(sandbox).check(
            criterion, reference_dir=_reference_dir(tmp_path, **{"solution.py": code})
        )

        assert result.score > 0.8
        assert "ast" in result.details
