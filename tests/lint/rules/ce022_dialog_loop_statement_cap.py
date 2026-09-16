"""CE022: ``Orchestrator._simulation_dialog_loop`` must stay under its statement cap.

The rule counts every statement node in ``_simulation_dialog_loop`` in
``orchestrator.py`` (recursively, the notion ruff PLR0915 bounds; ``def`` or
``async def``) and fires when the count exceeds ``_CAP``. It targets only that one
function; ruff's ceiling covers every other function.

HAZARD: the function keeps ``# noqa: PLR0915``, which disables ruff's statement
check for it, so this rule is its only bound. Bump ``_CAP`` only with a reviewed
change to the dialog driver. If a decomposition brings the function under ruff's
ceiling, remove the ``# noqa: PLR0915`` AND this rule together.

Rationale: .claude/notes/lint-rules.md § CE022
"""

import ast
from pathlib import Path

from tests.lint.rules.base import BaseRule


def _count_statements(func: ast.AsyncFunctionDef | ast.FunctionDef) -> int:
    """Statement nodes in the function body, counted recursively (nested
    compound-statement bodies included) — the same notion ruff PLR0915 bounds."""
    return sum(1 for stmt in func.body for node in ast.walk(stmt) if isinstance(node, ast.stmt))


class SimulationDialogLoopStatementCap(BaseRule):
    id = "CE022"

    _TARGET_FILE = "orchestrator.py"
    _TARGET_FUNC = "_simulation_dialog_loop"
    # Measured post-decomposition count (122) + 6 headroom. Re-measure and bump
    # _CAP only alongside an intentional, reviewed change to the dialog driver.
    _CAP = 128

    def _check(self, node: ast.AsyncFunctionDef | ast.FunctionDef) -> None:
        # Exact-basename match (not endswith) so a sibling like ``x_orchestrator.py``
        # can't accidentally match; both sync and async defs are checked so a future
        # ``async def`` → ``def`` conversion can't silently drop the guard.
        if Path(self.filepath).name == self._TARGET_FILE and node.name == self._TARGET_FUNC:
            count = _count_statements(node)
            if count > self._CAP:
                self.violation(
                    node,
                    f"{self._TARGET_FUNC} has {count} statements (cap {self._CAP}). It keeps a "
                    f"# noqa: PLR0915, which disables ruff's statement check entirely, so this CE rule "
                    f"bounds its regrowth. Decompose further, or — if the growth is intentional and "
                    f"reviewed — re-measure and bump _CAP in this rule.",
                )
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check(node)
