"""CE036: a module-level private helper in ``src/`` must have a caller.

A ``def _helper(...)`` that nothing in ``src/`` references is not merely dead
weight — it actively misleads. The bug that motivated this rule shipped an
``_rmtree_restrictive`` whose docstring explained, correctly and in detail, why
``rmtree(..., ignore_errors=True)`` orphans a mode-000 reference tree… while
both live cleanup sites went on calling exactly that. A reader auditing the
cleanup path found a function asserting the shipped code was broken, and a test
that called the helper directly made the real path read as covered.

The rule turns "helper written, never wired" into a ``make lint`` failure at the
commit that introduces it, which is the only moment anyone knows where it was
supposed to be called from.

Scope is deliberately narrow so it stays a bug detector rather than a style
nag:

* module-level ``def`` / ``async def`` only (methods are found via ``self``,
  which this cannot see),
* names starting with a single underscore only (public API has out-of-tree
  callers, dunders are protocol),
* decorated functions are skipped (a decorator is a registration —
  ``@register_criterion``, ``@field_validator``, ``@app.command`` — so the
  reference is the decorator, not a call),
* a name re-exported in ``__all__`` is skipped.

Use ``# noqa: CE036`` for a deliberate SPI hook that genuinely has no in-tree
caller, with a comment naming who calls it.
"""

import ast
import re
from pathlib import Path

from tests.lint.rules.base import BaseRule


_SRC_ROOT = Path("src/coder_eval")


def _all_source_text() -> str:
    """Concatenated text of every module under ``src/coder_eval``.

    A whole-tree grep rather than an import graph: a helper referenced anywhere
    — called, passed as a callback, aliased — counts as wired. False negatives
    (a name that merely appears in a docstring) are the right trade for a rule
    that must never block a legitimate refactor.
    """
    parts: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except OSError:  # pragma: no cover - unreadable file in src is not our problem
            continue
    return "\n".join(parts)


class NoDeadPrivateHelper(BaseRule):
    id = "CE036"

    _SRC_PATH = re.compile(r"[/\\]src[/\\]coder_eval[/\\]")
    _corpus: str | None = None

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(self._SRC_PATH.search(filepath))
        self._module_level: set[str] = set()
        if self._in_scope and NoDeadPrivateHelper._corpus is None:
            NoDeadPrivateHelper._corpus = _all_source_text()

    def visit_Module(self, node: ast.Module) -> None:
        # Record which defs are module-level BEFORE descending, so nested
        # functions (closures — referenced only inside their parent) are exempt.
        self._module_level = {
            child.name for child in node.body if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self._exported = _dunder_all(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check(node)
        self.generic_visit(node)

    def _check(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = node.name
        if not self._in_scope or name not in self._module_level:
            return
        if not name.startswith("_") or name.startswith("__"):
            return
        if node.decorator_list or name in self._exported:
            return
        corpus = NoDeadPrivateHelper._corpus or ""
        # One occurrence is the definition itself; anything more is a reference.
        if len(re.findall(rf"\b{re.escape(name)}\b", corpus)) > 1:
            return
        self.violation(
            node,
            f"private helper '{name}' has no caller anywhere in src/coder_eval — either wire it into the "
            + "code path its docstring describes, or delete it. A helper that documents a bug the shipped "
            + "code still has is worse than no helper (see CE036's docstring for the motivating case)",
        )


def _dunder_all(module: ast.Module) -> set[str]:
    for stmt in module.body:
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.List):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
            continue
        return {el.value for el in stmt.value.elts if isinstance(el, ast.Constant) and isinstance(el.value, str)}
    return set()
