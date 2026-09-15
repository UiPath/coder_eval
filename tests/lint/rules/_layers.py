"""The core-layer predicate, declared once and shared by CE004 and CE066.

Both rules ask the same question — "is this file in the core layer?" — and a
second copy of the answer is how a package added to one regex silently escapes
the other. ``_model_ctor.py`` is the in-tree precedent for a ``_``-prefixed
shared rule helper.

The boundary is an ALLOWLIST of what is *not* core: everything under
``src/coder_eval/`` is core except the ``cli/`` and ``reports/`` packages, so a
new subpackage is core by default rather than exempt until someone notices. The
denylist form is what leaves holes, twice over. An earlier draft named only
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
predicate. ``TestCoreLayerMembership`` pins the residual so nobody reads the
anchoring as a complete fix.
"""

import ast
import re


_PKG = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]")
_NON_CORE = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\](cli|reports)[/\\]")


def is_core_path(filepath: str) -> bool:
    """Whether ``filepath`` belongs to the core layer."""
    return bool(_PKG.search(filepath)) and not _NON_CORE.search(filepath)


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
