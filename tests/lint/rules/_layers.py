"""The core-layer predicate, declared once and shared by CE004 and CE066.

Both rules ask the same question — "is this file in the core layer?" — and a
second copy of the answer is how a package added to one regex silently escapes
the other. ``_model_ctor.py`` is the in-tree precedent for a ``_``-prefixed
shared rule helper.

The boundary is stated as an ALLOWLIST of what is *not* core, because the
denylist form is what leaves holes: an earlier draft named only
``orchestrator.py`` as the top-level core module, which exempted
``result_metrics.py`` — the very module CE066's fix message tells a violator to
move their metric into — along with ``run_record.py``, ``stats.py`` and
``timing.py``. Every ``.py`` directly under ``src/coder_eval/`` is core; the
non-core layers are the ``cli/`` and ``reports/`` packages, which are named.
"""

import ast
import re


_CORE_DIRS = re.compile(
    r"[/\\](criteria|evaluation|models|simulation|scoring|streaming|errors|orchestration|agents|harbor)[/\\]"
)
# Every module directly under src/coder_eval/ — orchestrator.py, result_metrics.py,
# run_record.py, timing.py and friends. No directory regex can see these.
_TOP_LEVEL_CORE = re.compile(r"[/\\]coder_eval[/\\][A-Za-z_][A-Za-z0-9_]*\.py$")


def is_core_path(filepath: str) -> bool:
    """Whether ``filepath`` belongs to the core layer."""
    return bool(_CORE_DIRS.search(filepath) or _TOP_LEVEL_CORE.search(filepath))


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
