"""Tests for the unified file_check criterion."""

from unittest.mock import MagicMock

import pytest

from coder_eval.criteria.file_check import FileCheckChecker
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import FileCheckCriterion, RegexPattern, SandboxConfig
from coder_eval.sandbox import Sandbox


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


class TestFileCheckModel:
    """Verify FileCheckCriterion model defaults and construction."""

    def test_defaults(self):
        c = FileCheckCriterion(description="d", path="f.py")
        assert c.includes == []
        assert c.excludes == []
        assert c.patterns == []
        assert c.type == "file_check"

    def test_full_construction(self):
        c = FileCheckCriterion(
            description="d",
            path="f.py",
            includes=["a"],
            excludes=["b"],
            patterns=[RegexPattern(pattern=r"\d+")],
        )
        assert c.includes == ["a"]
        assert c.excludes == ["b"]
        assert len(c.patterns) == 1

    def test_regex_pattern_defaults(self):
        p = RegexPattern(pattern=r"\w+")
        assert p.must_match is True
        assert p.flags == 0


# ---------------------------------------------------------------------------
# Unit tests (mocked sandbox)
# ---------------------------------------------------------------------------


class TestFileCheckScoring:
    """Verify scoring logic with mocked sandbox."""

    def _sandbox(self, content: str | None = None) -> MagicMock:
        s = MagicMock(spec=Sandbox)
        if content is None:
            s.file_exists.return_value = False
        else:
            s.file_exists.return_value = True
            s.get_file_content.return_value = content
        return s

    def test_file_not_found(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(description="d", path="x.py", includes=["a"])
        result = checker._check_impl(c, self._sandbox(None))
        assert result.score == 0.0
        assert "does not exist" in result.error

    def test_existence_only(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox("anything"))
        assert result.score == 1.0
        assert "exists" in result.details

    def test_includes_only_not_inflated(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(description="d", path="x.py", includes=["a", "b", "c", "d", "e"])
        result = checker._check_impl(c, self._sandbox("a b c"))
        # 3/5 found = 0.6, not inflated by absent excludes/patterns
        assert result.score == pytest.approx(0.6, abs=0.01)

    def test_excludes_only_not_inflated(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(description="d", path="x.py", excludes=["bad1", "bad2"])
        result = checker._check_impl(c, self._sandbox("has bad1 in it"))
        # 1/2 excluded present → excludes_score = 0.5
        assert result.score == pytest.approx(0.5, abs=0.01)

    def test_patterns_only_not_inflated(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(
            description="d",
            path="x.py",
            patterns=[
                RegexPattern(pattern=r"def \w+"),
                RegexPattern(pattern=r"class \w+"),
            ],
        )
        result = checker._check_impl(c, self._sandbox("def foo():\n    pass"))
        # 1/2 patterns matched = 0.5
        assert result.score == pytest.approx(0.5, abs=0.01)

    def test_all_pass(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(
            description="d",
            path="x.py",
            includes=["hello"],
            excludes=["bad"],
            patterns=[RegexPattern(pattern=r"hello")],
        )
        result = checker._check_impl(c, self._sandbox("hello world"))
        assert result.score == pytest.approx(1.0, abs=0.01)

    def test_combined_partial(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(
            description="d",
            path="x.py",
            includes=["a", "b"],  # 1/2 found = 0.5
            excludes=["bad"],  # absent = 1.0
            patterns=[RegexPattern(pattern=r"hello")],  # matched = 1.0
        )
        result = checker._check_impl(c, self._sandbox("a hello"))
        # average(0.5, 1.0, 1.0) = 0.833...
        assert result.score == pytest.approx(5.0 / 6.0, abs=0.01)

    def test_must_not_match_pass(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(
            description="d",
            path="x.py",
            patterns=[RegexPattern(pattern=r"TODO", must_match=False)],
        )
        result = checker._check_impl(c, self._sandbox("clean code"))
        assert result.score == 1.0

    def test_must_not_match_fail(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(
            description="d",
            path="x.py",
            patterns=[RegexPattern(pattern=r"TODO", must_match=False)],
        )
        result = checker._check_impl(c, self._sandbox("# TODO fix this"))
        assert result.score == 0.0

    def test_invalid_regex(self):
        checker = FileCheckChecker()
        c = FileCheckCriterion(
            description="d",
            path="x.py",
            patterns=[RegexPattern(pattern=r"[invalid(")],
        )
        result = checker._check_impl(c, self._sandbox("some text"))
        assert result.score == 0.0
        assert "Invalid regex" in result.details


# ---------------------------------------------------------------------------
# Integration tests (real sandbox)
# ---------------------------------------------------------------------------


class TestFileCheckIntegration:
    """Integration tests with real sandbox."""

    def test_existence_only(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_fc_exist")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "hello.py").write_text("print('hi')\n")

        checker = SuccessChecker(sandbox)
        result = checker.check(FileCheckCriterion(description="exists", path="hello.py"))

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)

    def test_file_missing(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_fc_miss")
        sandbox.setup()

        checker = SuccessChecker(sandbox)
        result = checker.check(FileCheckCriterion(description="missing", path="nope.py"))

        assert result.score == 0.0
        assert "does not exist" in result.error
        sandbox.cleanup(preserve=False)

    def test_includes_and_patterns(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_fc_combo")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "app.py").write_text("import os\n\ndef main():\n    pass\n")

        criterion = FileCheckCriterion(
            description="combo",
            path="app.py",
            includes=["import os", "def main"],
            patterns=[RegexPattern(pattern=r"def main\(\)")],
        )
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == pytest.approx(1.0, abs=0.01)
        sandbox.cleanup(preserve=False)
