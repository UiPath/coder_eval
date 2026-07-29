"""Tests for glob patterns in criterion ``path`` fields.

A criterion can address a file whose exact location the task prompt does not
pin — e.g. a scaffolded wrapper directory the agent was free to name. Path
resolution lives in ``Sandbox.resolve_files``, so every path-based criterion
type inherits the behavior.
"""

import pytest

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    FileCheckCriterion,
    FileContainsCriterion,
    FileExistsCriterion,
    SandboxConfig,
)
from coder_eval.sandbox import Sandbox


FLOW_BODY = '{"nodes": [{"type": "uipath.human-in-the-loop.quick-form"}]}'


@pytest.fixture
def sandbox():
    sb = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="test_glob_paths")
    sb.setup()
    yield sb
    sb.cleanup(preserve=False)


def _write(sandbox, relpath: str, body: str = FLOW_BODY):
    target = sandbox.sandbox_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


class TestResolveFiles:
    def test_plain_path_unchanged(self, sandbox):
        _write(sandbox, "app.py", "print('hi')")

        assert sandbox.resolve_files("app.py") == [sandbox.sandbox_dir / "app.py"]
        assert sandbox.file_exists("app.py") is True

    def test_plain_path_missing(self, sandbox):
        assert sandbox.resolve_files("nope.py") == []
        assert sandbox.file_exists("nope.py") is False

    def test_glob_finds_file_under_unpinned_directory(self, sandbox):
        _write(sandbox, "InvoiceApprovalSolution/InvoiceApproval/InvoiceApproval.flow")

        assert sandbox.file_exists("**/*.flow") is True
        assert sandbox.get_file_content("**/*.flow") == FLOW_BODY
        assert sandbox.file_exists("InvoiceApproval/InvoiceApproval/InvoiceApproval.flow") is False

    def test_glob_no_match(self, sandbox):
        assert sandbox.resolve_files("**/*.flow") == []
        assert sandbox.file_exists("**/*.flow") is False
        with pytest.raises(FileNotFoundError, match="No file matches"):
            sandbox.get_file_content("**/*.flow")

    def test_glob_skips_directories(self, sandbox):
        (sandbox.sandbox_dir / "build.flow").mkdir()
        _write(sandbox, "proj/real.flow")

        assert sandbox.resolve_files("**/*.flow") == [sandbox.sandbox_dir / "proj" / "real.flow"]

    def test_ambiguous_glob_refuses_to_guess(self, sandbox):
        _write(sandbox, "a/one.flow")
        _write(sandbox, "b/two.flow")

        assert sandbox.file_exists("**/*.flow") is True
        with pytest.raises(ValueError, match="refusing to guess"):
            sandbox.get_file_content("**/*.flow")

    def test_matches_are_sorted(self, sandbox):
        _write(sandbox, "z/last.flow")
        _write(sandbox, "a/first.flow")

        assert sandbox.resolve_files("**/*.flow") == sorted(sandbox.resolve_files("**/*.flow"))

    def test_no_sandbox_dir(self):
        sb = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="test_glob_unset")

        assert sb.resolve_files("**/*.flow") == []
        assert sb.file_exists("**/*.flow") is False
        with pytest.raises(RuntimeError, match="not set up"):
            sb.get_file_content("**/*.flow")


class TestGlobThroughCriteria:
    def test_file_exists_criterion(self, sandbox):
        _write(sandbox, "WrapperSolution/Proj/Proj.flow")

        result = SuccessChecker(sandbox).check(FileExistsCriterion(description="flow exists", path="**/*.flow"))

        assert result.score == 1.0

    def test_file_contains_criterion(self, sandbox):
        _write(sandbox, "WrapperSolution/Proj/Proj.flow")

        result = SuccessChecker(sandbox).check(
            FileContainsCriterion(
                description="has HITL node",
                path="**/*.flow",
                includes=['"uipath.human-in-the-loop.quick-form"'],
            )
        )

        assert result.score == 1.0

    def test_file_check_criterion(self, sandbox):
        _write(sandbox, "WrapperSolution/Proj/Proj.flow")

        result = SuccessChecker(sandbox).check(
            FileCheckCriterion(
                description="has HITL node, no manual trigger",
                path="**/*.flow",
                includes=['"uipath.human-in-the-loop.quick-form"'],
                excludes=['"core.trigger.manual"'],
            )
        )

        assert result.score == 1.0

    def test_ambiguous_glob_scores_zero_with_message(self, sandbox):
        _write(sandbox, "a/one.flow")
        _write(sandbox, "b/two.flow")

        result = SuccessChecker(sandbox).check(
            FileContainsCriterion(description="ambiguous", path="**/*.flow", includes=["nodes"])
        )

        assert result.score == 0.0
        assert "refusing to guess" in (result.error or "")

    def test_missing_glob_scores_zero(self, sandbox):
        result = SuccessChecker(sandbox).check(FileExistsCriterion(description="no flow", path="**/*.flow"))

        assert result.score == 0.0
