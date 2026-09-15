"""The package-layer predicates, declared once and shared by CE004 and CE066.

Both rules ask where a file sits in ``src/coder_eval/``, and a second copy of
the answer is how a package added to one regex silently escapes the other. So
the package anchor and the ``cli/`` boundary are each spelled once, here.
``_model_ctor.py`` is the in-tree precedent for a ``_``-prefixed shared rule
helper.

The two rules do NOT share an exemption set, because they do not ask the same
question. CE004 bans ``cli`` imports from everything that must run without the
CLI, which is the whole package except ``cli/`` itself. CE066 bans reaching into
the reports layer, which ``reports/`` may obviously do to itself, so its "core"
also excludes ``reports/``. When CE004 borrowed CE066's predicate wholesale it
inherited the ``reports/`` exemption, and a ``cli`` import added inside the
reports package — which the orchestrator imports mid-run, closing a
cli -> orchestration -> reports -> cli cycle — would have passed silently.

Both scopes are ALLOWLISTS of what is exempt, so a new subpackage is in scope by
default rather than exempt until someone notices. The denylist form is what
leaves holes, twice over. An earlier draft named only
``orchestrator.py`` as the top-level core module, which exempted
``result_metrics.py`` — the very module CE066's fix message tells a violator to
move their metric into — along with ``run_record.py``, ``stats.py`` and
``timing.py``. Its successor listed ten core directories and ``isolation/`` was
not one of them, so ``isolation/docker_runner.py`` — the ``driver: docker``
evaluation path, which imports ``models``, ``orchestration`` and ``streaming`` —
could import anything with both rules silent.

The package regex is anchored on ``src/`` because the unanchored form made a
repo-root file core: this project's own checkout directory is named
``coder_eval``, so ``…/coder_eval/conftest.py`` matched the package.

Blind spot: anchoring narrows that trap without closing it. A clone whose parent
directory is literally named ``src`` — ``~/src/coder_eval/conftest.py`` — still
matches, and so now does that clone's ``tests/`` tree. No path substring can
separate the package from a checkout laid out like it; closing it properly means
relativising every rule's path against the repo root. It is unreachable today:
CE004 and CE066 are only ever handed paths under the runner's ``SRC``, never the
repo root, and ``_ALSO_SCAN_TESTS`` is ``{"CE048"}``, which uses neither
predicates. ``TestCoreLayerMembership`` pins the residual so nobody reads the
anchoring as a complete fix.
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


def imports_package(node: ast.ImportFrom, package: str) -> bool:
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
    if not node.module:
        return False
    if node.level:
        return node.module == package or node.module.startswith(f"{package}.")
    full = f"coder_eval.{package}"
    return node.module == full or node.module.startswith(f"{full}.")


def is_bare_package_import(node: ast.ImportFrom, package: str) -> bool:
    """Whether this binds the package itself rather than a name out of it.

    Three spellings do that: ``from . import reports``, ``from .. import reports``
    and ``from coder_eval import reports``. The last is the one both rules missed
    longest — it is neither relative nor a dotted path, so `node.module` is the
    bare ``"coder_eval"`` and the package arrives as an alias.

    Worth catching because the binding is the dangerous half: once `reports` is a
    local name, every attribute read through it is invisible to an import check.
    """
    if node.level:
        return node.module is None and any(a.name == package for a in node.names)
    return node.module == "coder_eval" and any(a.name == package for a in node.names)
