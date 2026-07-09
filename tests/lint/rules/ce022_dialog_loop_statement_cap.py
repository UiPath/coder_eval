"""CE022: ``Orchestrator._simulation_dialog_loop`` must stay under its statement cap.

``_simulation_dialog_loop`` is the one function in the tree that keeps a
``# noqa: PLR0915``: it is a sequential dialog driver whose residual length is
irreducible without a state-object rewrite (see the 2026-06-23
decompose-god-functions plan, Phase 5). But ``# noqa: PLR0915`` *disables ruff's
statement check entirely* — without a guard the function could silently regrow
back toward its pre-decomposition size and nothing would fail.

This rule re-imposes a bound: it counts the statements in
``_simulation_dialog_loop`` (every statement node in the body, recursively — the
same notion ruff PLR0915 bounds) and fires if the count exceeds ``_CAP``. The cap
is the measured post-decomposition size plus a small headroom, so ordinary edits
don't trip it but a real regrowth does. Deliberately narrow: it targets the one
named function in ``orchestrator.py``, not a general size rule (ruff's 80/25
ceiling already covers every other function).

If a future decomposition legitimately brings the function under ruff's ceiling,
remove the ``# noqa: PLR0915`` AND this rule together.
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
