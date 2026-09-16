"""CE037: a module-level private helper in ``src/`` must have a caller.

Fires when the name of such a helper occurs only once (its definition) in the
concatenated text of ``src/coder_eval``. Any other occurrence counts as a caller.
Scope:

* module-level ``def`` / ``async def`` only (not methods, not closures),
* a single leading underscore only (not public names, not dunders),
* undecorated only (a decorator is a registration),
* not re-exported in ``__all__``.

Motivating case: ``_rmtree_restrictive`` documented why
``rmtree(..., ignore_errors=True)`` orphans a mode-000 reference tree, while both
live cleanup sites still called exactly that.

Use ``# noqa: CE037`` for a deliberate SPI hook that genuinely has no in-tree
caller, with a comment naming who calls it.

Rationale: .claude/notes/lint-rules.md § CE037
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
    id = "CE037"

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
            + "code still has is worse than no helper (see CE037's docstring for the motivating case)",
        )


def _dunder_all(module: ast.Module) -> set[str]:
    for stmt in module.body:
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.List):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
            continue
        return {el.value for el in stmt.value.elts if isinstance(el, ast.Constant) and isinstance(el.value, str)}
    return set()
