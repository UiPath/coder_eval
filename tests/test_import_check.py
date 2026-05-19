"""Tests for the import_check criterion."""

import json
import shlex
from unittest.mock import MagicMock

import pytest

from coder_eval.criteria import import_check as import_check_mod
from coder_eval.criteria.import_check import ImportCheckChecker, _python_exe, extract_imports
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import ImportCheckCriterion, SandboxConfig
from coder_eval.sandbox import Sandbox


# ---------------------------------------------------------------------------
# extract_imports unit tests
# ---------------------------------------------------------------------------


class TestExtractImports:
    """Unit tests for the extract_imports helper."""

    def test_basic_import(self):
        assert extract_imports("import os") == ["os"]

    def test_dotted_import(self):
        assert extract_imports("import os.path") == ["os.path"]

    def test_from_import(self):
        assert extract_imports("from collections import OrderedDict") == ["collections"]

    def test_from_dotted_import(self):
        assert extract_imports("from os.path import join") == ["os.path"]

    def test_multiple_imports(self):
        source = "import os\nimport sys\nfrom json import dumps"
        result = extract_imports(source)
        assert set(result) == {"os", "sys", "json"}

    def test_nested_in_function(self):
        source = "def foo():\n    import os\n    from sys import argv"
        result = extract_imports(source)
        assert set(result) == {"os", "sys"}

    def test_nested_in_try_except(self):
        source = "try:\n    import uipath\nexcept ImportError:\n    import fallback"
        result = extract_imports(source)
        assert set(result) == {"uipath", "fallback"}

    def test_relative_imports_all_skipped(self):
        # All relative imports are skipped regardless of whether they have a module name.
        # find_spec cannot resolve them without the package hierarchy.
        source = "from . import foo\nfrom .bar import baz\nfrom ..utils import helper"
        assert extract_imports(source) == []

    def test_no_imports(self):
        assert extract_imports("x = 1\nprint(x)") == []

    def test_syntax_error_raises(self):
        with pytest.raises(SyntaxError):
            extract_imports("def (broken syntax")

    def test_multi_name_import(self):
        result = extract_imports("import os, sys")
        assert set(result) == {"os", "sys"}

    def test_duplicates_deduplicated(self):
        source = "import os\nimport os\nfrom os import path"
        assert extract_imports(source) == ["os"]


# ---------------------------------------------------------------------------
# _python_exe helper tests
# ---------------------------------------------------------------------------


class TestPythonExe:
    """Verify the sandbox python invocation is portable + properly quoted."""

    def test_posix_quoting_via_shlex(self, monkeypatch):
        fake_path = "/usr/local/bin/python 3.13"
        monkeypatch.setattr(import_check_mod.os, "name", "posix")
        monkeypatch.setattr(import_check_mod.sys, "executable", fake_path)
        result = _python_exe()
        # Round-trip through shlex.split to prove the quoting survives.
        assert shlex.split(result) == [fake_path]

    def test_windows_quoting_wraps_in_double_quotes(self, monkeypatch):
        fake_path = r"C:\Program Files\Python313\python.exe"
        monkeypatch.setattr(import_check_mod.os, "name", "nt")
        monkeypatch.setattr(import_check_mod.sys, "executable", fake_path)
        result = _python_exe()
        assert result == f'"{fake_path}"'

    def test_fallback_when_sys_executable_empty(self, monkeypatch):
        monkeypatch.setattr(import_check_mod.sys, "executable", "")
        assert _python_exe() == "python"


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


class TestImportCheckModel:
    """Verify ImportCheckCriterion model defaults and construction."""

    def test_defaults(self):
        c = ImportCheckCriterion(description="d", path="f.py")
        assert c.timeout == 30
        assert c.type == "import_check"

    def test_custom_timeout(self):
        c = ImportCheckCriterion(description="d", path="f.py", timeout=60)
        assert c.timeout == 60


# ---------------------------------------------------------------------------
# Checker unit tests (mocked sandbox)
# ---------------------------------------------------------------------------


class TestImportCheckChecker:
    """Verify checker logic with mocked sandbox."""

    def _sandbox(self, content: str | None = None, cmd_result: tuple[int, str, str] | None = None) -> MagicMock:
        s = MagicMock(spec=Sandbox)
        if content is None:
            s.file_exists.return_value = False
        else:
            s.file_exists.return_value = True
            s.get_file_content.return_value = content
        if cmd_result is not None:
            s.run_command.return_value = cmd_result
        return s

    def test_file_not_found(self):
        checker = ImportCheckChecker()
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(None))
        assert result.score == 0.0
        assert "does not exist" in result.error

    def test_syntax_error(self):
        checker = ImportCheckChecker()
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox("def (broken"))
        assert result.score == 0.0
        assert "SyntaxError" in result.error

    def test_all_imports_valid(self):
        checker = ImportCheckChecker()
        source = "import os\nimport sys"
        results_json = json.dumps({"os": True, "sys": True})
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(source, (0, results_json, "")))
        assert result.score == 1.0
        assert "2/2" in result.details

    def test_partial_imports_valid(self):
        checker = ImportCheckChecker()
        source = "import os\nimport sys\nimport nonexistent_pkg"
        results_json = json.dumps({"os": True, "sys": True, "nonexistent_pkg": False})
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(source, (0, results_json, "")))
        assert result.score == pytest.approx(2.0 / 3.0, abs=0.01)
        assert "nonexistent_pkg" in result.details

    def test_all_imports_fail(self):
        checker = ImportCheckChecker()
        source = "import fake_a\nimport fake_b"
        results_json = json.dumps({"fake_a": False, "fake_b": False})
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(source, (0, results_json, "")))
        assert result.score == 0.0

    def test_no_imports(self):
        checker = ImportCheckChecker()
        source = "x = 1\nprint(x)"
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(source))
        assert result.score == 1.0
        assert "No imports to check" in result.details

    def test_sandbox_command_failure(self):
        checker = ImportCheckChecker()
        source = "import os"
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(source, (1, "", "error")))
        assert result.score == 0.0

    def test_sandbox_command_invalid_json(self):
        checker = ImportCheckChecker()
        source = "import os"
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(source, (0, "not json", "")))
        assert result.score == 0.0

    def test_from_import_checks_module(self):
        """from foo.bar import baz checks foo.bar, not baz."""
        checker = ImportCheckChecker()
        source = "from os.path import join"
        results_json = json.dumps({"os.path": True})
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(source, (0, results_json, "")))
        assert result.score == 1.0

    def test_dotted_import_missing_parent_fractional(self):
        """find_spec crash on dotted import with missing parent should not zero all scores."""
        checker = ImportCheckChecker()
        source = "import os\nfrom nonexistent_parent.child import thing"
        # Simulate what the fixed sandbox script returns: os resolves, nonexistent_parent.child doesn't
        results_json = json.dumps({"os": True, "nonexistent_parent.child": False})
        c = ImportCheckCriterion(description="d", path="x.py")
        result = checker._check_impl(c, self._sandbox(source, (0, results_json, "")))
        assert result.score == pytest.approx(0.5, abs=0.01)
        assert "nonexistent_parent.child" in result.details


# ---------------------------------------------------------------------------
# Integration tests (real sandbox)
# ---------------------------------------------------------------------------


class TestImportCheckIntegration:
    """Integration tests with real sandbox."""

    def test_valid_stdlib_imports(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_ic_valid")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "app.py").write_text("import os\nimport sys\nimport json\n")

        checker = SuccessChecker(sandbox)
        result = checker.check(ImportCheckCriterion(description="stdlib", path="app.py"))

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)

    def test_invalid_import(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_ic_invalid")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "app.py").write_text("import nonexistent_package_xyz_12345\n")

        checker = SuccessChecker(sandbox)
        result = checker.check(ImportCheckCriterion(description="bad import", path="app.py"))

        assert result.score == 0.0
        assert "nonexistent_package_xyz_12345" in result.details
        sandbox.cleanup(preserve=False)

    def test_syntax_error_file(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_ic_syntax")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "broken.py").write_text("def (\n")

        checker = SuccessChecker(sandbox)
        result = checker.check(ImportCheckCriterion(description="broken", path="broken.py"))

        assert result.score == 0.0
        assert "SyntaxError" in result.error
        sandbox.cleanup(preserve=False)

    def test_mixed_valid_invalid(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_ic_mixed")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "app.py").write_text("import os\nimport nonexistent_xyz_999\n")

        checker = SuccessChecker(sandbox)
        result = checker.check(ImportCheckCriterion(description="mixed", path="app.py"))

        assert result.score == pytest.approx(0.5, abs=0.01)
        sandbox.cleanup(preserve=False)

    def test_dotted_import_missing_parent_preserves_fractional_score(self):
        """find_spec on dotted import with missing parent must not crash and zero all scores."""
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_ic_dotted")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "app.py").write_text("import os\nfrom nonexistent_parent.child import thing\n")

        checker = SuccessChecker(sandbox)
        result = checker.check(ImportCheckCriterion(description="dotted", path="app.py"))

        assert result.score == pytest.approx(0.5, abs=0.01), (
            f"Expected 0.5 (1 valid / 2 total) but got {result.score}. "
            "find_spec likely crashed on dotted import, zeroing all scores."
        )
        assert "nonexistent_parent.child" in result.details
        sandbox.cleanup(preserve=False)

    def test_nested_bad_import_caught(self):
        """Import inside try/except is caught (unlike dynamic import)."""
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_ic_nested")
        sandbox_dir = sandbox.setup()
        source = "try:\n    import nonexistent_hidden_pkg\nexcept ImportError:\n    pass\n"
        (sandbox_dir / "app.py").write_text(source)

        checker = SuccessChecker(sandbox)
        result = checker.check(ImportCheckCriterion(description="nested", path="app.py"))

        assert result.score == 0.0
        assert "nonexistent_hidden_pkg" in result.details
        sandbox.cleanup(preserve=False)
