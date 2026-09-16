"""The package-layer predicates, declared once and shared by CE004 and CE066.

Both rules ask where a file sits in ``src/coder_eval/``, so the package anchor and the
``cli/`` boundary are each spelled once, here. ``_model_ctor.py`` is the precedent for a
``_``-prefixed shared rule helper.

The two do NOT share an exemption set. CE004's scope is the package minus ``cli/``; CE066
also excludes ``reports/``, which may reach into itself. Both scopes are ALLOWLISTS, so a
new subpackage is in scope by default.

A relative import is RESOLVED against the importing file, not pattern-matched:
``from .reports import x`` means ``coder_eval.reports`` at top level and
``coder_eval.orchestration.reports`` inside ``orchestration/``. See ``_absolute_module``.

BLIND SPOT: the package regex is anchored on ``src/``, which narrows but does not close the
trap of a checkout laid out like the package. Unreachable today;
``TestCoreLayerMembership`` pins the residual.

Rationale: .claude/notes/lint-rules.md § _layers
"""

import ast
import re


_PKG = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]")
_CLI = re.compile(_PKG.pattern + r"cli[/\\]")
_REPORTS = re.compile(_PKG.pattern + r"reports[/\\]")


def is_package_path(filepath: str) -> bool:
    """Whether ``filepath`` is anywhere under ``src/coder_eval/``."""
    return bool(_PKG.search(filepath))


def is_cli_path(filepath: str) -> bool:
    """Whether ``filepath`` is inside the ``cli/`` package."""
    return bool(_CLI.search(filepath))


def is_core_path(filepath: str) -> bool:
    """Whether ``filepath`` is in CE066's core: the package minus ``cli/`` and ``reports/``."""
    return is_package_path(filepath) and not is_cli_path(filepath) and not _REPORTS.search(filepath)


def _containing_package(filepath: str) -> list[str] | None:
    """The dotted parts of the package a module lives in, rooted at ``coder_eval``.

    ``reports/html.py`` and ``reports/__init__.py`` both answer
    ``["coder_eval", "reports"]`` — Python resolves a package's ``__init__`` against
    the package itself, not its parent, so dropping the filename is right for both.
    """
    match = _PKG.search(filepath)
    if not match:
        return None
    parts = [p for p in filepath[match.end() :].replace("\\", "/").split("/") if p]
    return ["coder_eval", *parts[:-1]]


def _absolute_module(node: ast.ImportFrom, filepath: str) -> str | None:
    """The fully-qualified module a ``from … import …`` names, or None if it escapes.

    A relative import only means ``coder_eval.<package>`` at ONE depth, and which
    depth depends on where the importing file sits. An earlier form compared
    ``node.module`` against the bare package name whenever ``node.level`` was
    non-zero, which reads ``from .reports import x`` inside ``orchestration/`` —
    i.e. ``coder_eval.orchestration.reports`` — as the reports layer. Nothing in
    the tree is nested that way today, so it was a latent false positive rather
    than a live one; resolving the dots against the file removes the class.
    """
    if not node.level:
        return node.module
    package = _containing_package(filepath)
    if package is None:
        return None
    base = package[: len(package) - (node.level - 1)]
    if not base:
        return None
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def imports_package(node: ast.ImportFrom, package: str, filepath: str) -> bool:
    """Whether a ``from … import …`` names ``coder_eval.<package>``, EITHER spelling.

    This exists because matching ``node.module`` alone is a trap both layering
    rules fell into. A relative import keeps its dots in ``node.level`` and leaves
    the rest in ``node.module``, so ``from ..cli import x`` arrives as
    ``level=2, module="cli"`` — and the relative form is this codebase's dominant
    idiom, so a rule that checks only the absolute path fires on almost nothing
    while its tests pass. CE066 shipped that way for one review cycle; CE004 had
    carried it since it was written.

    ``from . import cli`` and ``from coder_eval import cli`` are NOT matched here —
    they bind the package itself rather than a name out of it, so each rule reports
    them through ``is_bare_package_import`` as its own wholesale case.
    """
    module = _absolute_module(node, filepath)
    if module is None:
        return False
    full = f"coder_eval.{package}"
    return module == full or module.startswith(f"{full}.")


def is_bare_package_import(node: ast.ImportFrom, package: str, filepath: str) -> bool:
    """Whether this binds the package itself rather than a name out of it.

    Three spellings do that: ``from . import reports``, ``from .. import reports``
    and ``from coder_eval import reports``. The last is the one both rules missed
    longest — it is neither relative nor a dotted path, so `node.module` is the
    bare ``"coder_eval"`` and the package arrives as an alias.

    Worth catching because the binding is the dangerous half: once `reports` is a
    local name, every attribute read through it is invisible to an import check.
    """
    return _absolute_module(node, filepath) == "coder_eval" and any(a.name == package for a in node.names)
