"""Tests for glob patterns in criterion ``path`` fields.

A criterion can address a file whose exact location the task prompt does not
pin — e.g. a scaffolded wrapper directory the agent was free to name. Path
resolution lives in ``Sandbox.resolve_files``, so every path-based criterion
type inherits the behavior.
"""

from pathlib import Path

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
        _write(sandbox, "m/middle.flow")

        assert sandbox.resolve_files("**/*.flow") == [
            sandbox.sandbox_dir / "a" / "first.flow",
            sandbox.sandbox_dir / "m" / "middle.flow",
            sandbox.sandbox_dir / "z" / "last.flow",
        ]

    def test_no_sandbox_dir(self):
        sb = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="test_glob_unset")

        assert sb.resolve_files("**/*.flow") == []
        assert sb.file_exists("**/*.flow") is False
        assert sb.resolved_path_label("**/*.flow") is None
        with pytest.raises(RuntimeError, match="not set up"):
            sb.get_file_content("**/*.flow")


class TestLiteralPathsWinOverGlobInterpretation:
    """A path that exists is never reinterpreted as a pattern.

    ``Path.glob`` turns ``[...]`` into a character class, so without a
    literal-first probe a plain filename carrying a metacharacter would grade a
    different file — the exact silent-wrong-file failure globbing is meant to
    prevent. Dataset fan-out substitutes ``${row.<field>}`` into criterion
    paths, so such filenames are not only hand-written.
    """

    def test_bracketed_literal_beats_character_class_decoy(self, sandbox):
        _write(sandbox, "report[2024].json", "literal")
        _write(sandbox, "report2.json", "decoy")

        assert sandbox.resolve_files("report[2024].json") == [sandbox.sandbox_dir / "report[2024].json"]
        assert sandbox.get_file_content("report[2024].json") == "literal"

    def test_bracketed_literal_still_exists(self, sandbox):
        _write(sandbox, "logs[1]", "entries")

        assert sandbox.file_exists("logs[1]") is True
        assert sandbox.get_file_content("logs[1]") == "entries"

    def test_glob_still_expands_when_literal_is_absent(self, sandbox):
        _write(sandbox, "runs/report2.json", "matched")

        assert sandbox.get_file_content("runs/report[123].json") == "matched"


class TestIgnoredDirectoriesAreNotGraded:
    """The sandbox root holds harness-created content the agent never authored.

    ``.venv`` is created inside the root for any task with a ``python:`` block,
    and templates copy vendored trees in before the agent runs; ``Path.glob``
    descends into dotdirs. Grading off those files is neither fair (a pass with
    no agent output) nor deterministic (ambiguity that depends on the harness).
    """

    def test_venv_and_node_modules_are_skipped(self, sandbox):
        _write(sandbox, ".venv/lib/python3.13/site-packages/dep/config.json", "vendored")
        _write(sandbox, "node_modules/left-pad/package.json", "vendored")
        _write(sandbox, "src/config.json", "authored")

        assert sandbox.resolve_files("**/*.json") == [sandbox.sandbox_dir / "src" / "config.json"]
        assert sandbox.get_file_content("**/*.json") == "authored"

    def test_ignored_tree_alone_does_not_satisfy_file_exists(self, sandbox):
        _write(sandbox, ".venv/lib/site-packages/dep/__init__.py", "vendored")

        assert sandbox.file_exists("**/*.py") is False

    def test_literally_named_segment_is_an_opt_in(self, sandbox):
        _write(sandbox, "dist/bundle.js", "built")

        assert sandbox.resolve_files("dist/**/*.js") == [sandbox.sandbox_dir / "dist" / "bundle.js"]

    def test_ignore_pattern_negation_un_ignores_a_discovered_segment(self):
        sb = Sandbox(SandboxConfig(driver="tempdir", python=None, ignore_patterns=["!dist"]), task_id="test_glob_neg")
        sb.setup()
        try:
            _write(sb, "dist/bundle.js", "built")

            assert sb.resolve_files("**/*.js") == [sb.sandbox_dir / "dist" / "bundle.js"]
        finally:
            sb.cleanup(preserve=False)


class TestResolvedPathIsReported:
    def test_label_is_none_for_a_literal_path(self, sandbox):
        _write(sandbox, "app.py", "print('hi')")

        assert sandbox.resolved_path_label("app.py") is None

    def test_label_is_none_when_ambiguous(self, sandbox):
        _write(sandbox, "a/one.flow")
        _write(sandbox, "b/two.flow")

        assert sandbox.resolved_path_label("**/*.flow") is None

    def test_label_names_the_single_match(self, sandbox):
        _write(sandbox, "Wrapper/Proj/Proj.flow")

        assert sandbox.resolved_path_label("**/*.flow") == str(Path("Wrapper") / "Proj" / "Proj.flow")

    def test_ambiguity_message_is_capped(self, sandbox):
        for i in range(14):
            _write(sandbox, f"d{i:02d}/f.flow")

        with pytest.raises(ValueError, match=r"\+4 more"):
            sandbox.get_file_content("**/*.flow")


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

    def test_details_name_the_graded_file(self, sandbox):
        _write(sandbox, "WrapperSolution/Proj/Proj.flow")
        expected = str(Path("WrapperSolution") / "Proj" / "Proj.flow")

        exists = SuccessChecker(sandbox).check(FileExistsCriterion(description="flow exists", path="**/*.flow"))
        contains = SuccessChecker(sandbox).check(
            FileContainsCriterion(description="has node", path="**/*.flow", includes=["nodes"])
        )

        assert expected in (exists.details or "")
        assert expected in (contains.details or "")

    def test_missing_glob_scores_zero(self, sandbox):
        result = SuccessChecker(sandbox).check(FileExistsCriterion(description="no flow", path="**/*.flow"))

        assert result.score == 0.0
