"""Layering and packaging invariants for the `coder_eval.reports` package.

These are the things the Phase 4 split is *for*, and each is otherwise
unobservable: the package's public surface, and the two dependencies it must
not have.
"""

import subprocess
import sys

import pytest

import coder_eval.reports as pkg


class TestPublicSurface:
    def test_every_name_in_all_is_bound(self):
        """An `__all__` entry with no matching import makes `from ... import *`
        raise. (A submodule RENAME fails earlier, at this module's own import.)"""
        missing = sorted(n for n in pkg.__all__ if not hasattr(pkg, n))
        assert not missing, f"declared in __all__ but not bound on the package: {missing}"

    def test_every_writer_ce066_allowlists_is_public_here(self):
        """The two lists are one contract seen from opposite sides: CE066 permits
        core to import exactly these, so each must be part of the package's
        declared surface, not merely reachable through a submodule."""
        from tests.lint.rules.ce066_no_report_imports_in_core import ALLOWED_WRITERS

        assert set(pkg.__all__) >= ALLOWED_WRITERS, sorted(ALLOWED_WRITERS - set(pkg.__all__))

    def test_an_internal_rendering_constant_is_not_reachable(self):
        """SLOW_PARAMS_PREVIEW_CHARS is a private layout decision; `.html` imports
        it from `.markdown` directly. Asserted with hasattr rather than against
        `__all__`, so a stray re-export or star-import is caught too."""
        assert not hasattr(pkg, "SLOW_PARAMS_PREVIEW_CHARS")


class TestTheLeafHasNoUnwantedDependencies:
    """Assertions run in a FRESH interpreter: this process has already imported
    half the package, so `sys.modules` here would prove nothing.
    """

    @staticmethod
    def _imports(imported: str, module: str) -> bool:
        code = f"import {imported}, sys; print({module!r} in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        return out.stdout.strip() == "True"

    def test_durations_is_free_of_the_agent_sdk(self):
        """This is what moving `format_ms` out of `formatting.py` buys.

        NOT the same as `coder_eval.reports` being SDK-free — it cannot be:
        `coder_eval.models.agent_config` imports `ClaudeAgentOptions`, and every
        report module needs `models`. What the split achieves is that the
        FORMATTER is usable without the SDK, and that the reports package no
        longer reaches through the SDK-shaped `formatting` module for it.
        """
        assert not self._imports("coder_eval.durations", "claude_agent_sdk")

    def test_reports_does_not_import_the_sdk_shaped_formatting_module(self):
        assert not self._imports("coder_eval.reports", "coder_eval.formatting")

    def test_reports_does_not_import_criteria(self):
        """`markdown.py`'s criteria import must stay function-local — it runs
        pkgutil auto-discovery with registry side effects."""
        assert not self._imports("coder_eval.reports", "coder_eval.criteria")


@pytest.mark.parametrize("submodule", ["markdown", "html", "experiment", "junit", "helpers"])
def test_no_submodule_imports_the_package_by_name(submodule):
    """`from . import X` / `from coder_eval.reports import X` inside a submodule
    re-enters __init__ mid-initialization. Every intra-package import must name
    a sibling module directly."""
    import ast
    from pathlib import Path

    path = Path(__file__).parent.parent / "src" / "coder_eval" / "reports" / f"{submodule}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from . import X` is level=1 with module None; `from coder_eval.reports import X` is absolute.
            assert not (node.level == 1 and node.module is None), f"{submodule}.py re-enters the package __init__"
            assert node.module != "coder_eval.reports", f"{submodule}.py re-enters the package __init__"
