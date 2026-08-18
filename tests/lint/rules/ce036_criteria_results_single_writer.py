"""CE036: ``success_criteria_results`` is written only through ``_record_criteria``.

Storing a grading pass's results and stamping ``_graded_iteration_count`` are
inseparable: ``_grade_after_forced_kill``'s skip-regrade shortcut keys on
``_graded_iteration_count == len(result.iterations)``. A grading site that
assigned ``success_criteria_results`` without stamping would leave the counter
stale, defeat the shortcut, and silently re-grade -- double-spending every
``llm_judge`` / ``agent_judge`` criterion on a run whose budget is already
blown, and (on the simulation path) potentially deciding the verdict from a
different trajectory than the one recorded.

The pair used to be hand-written at four sites, which is precisely the shape
that invites a forgotten fifth. ``Orchestrator._record_criteria`` is now the
single writer, and this rule keeps it that way: any other
``...success_criteria_results = ...`` assignment in ``src/`` is a violation.

Reads (``if self.result.success_criteria_results:``) are untouched -- only
assignment is guarded.

Scoped to ``orchestrator.py``: the seam being protected is
``Orchestrator._record_criteria``, so telling some future ``reports.py`` site
that builds a synthetic ``EvaluationResult`` to "call self._record_criteria()"
would be nonsense there.
"""

import ast
from pathlib import Path

from tests.lint.rules.base import BaseRule


# The one function allowed to perform the assignment.
_SINGLE_WRITER = "_record_criteria"

# The only module the single-writer seam governs.
_SCOPED_FILE = "orchestrator.py"

_GUARDED_ATTR = "success_criteria_results"


class CriteriaResultsSingleWriter(BaseRule):
    id = "CE036"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._fn_stack: list[str] = []
        self._in_scope = Path(filepath).name == _SCOPED_FILE

    def _visit_fn(self, node: ast.AsyncFunctionDef | ast.FunctionDef) -> None:
        self._fn_stack.append(node.name)
        self.generic_visit(node)
        self._fn_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_fn(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_fn(node)

    def _check_targets(self, node: ast.stmt, targets: list[ast.expr]) -> None:
        if not self._in_scope:
            return
        if self._fn_stack and self._fn_stack[-1] == _SINGLE_WRITER:
            return
        for target in targets:
            # Tuple/list unpacking assigns to each element, so recurse into them.
            if isinstance(target, ast.Tuple | ast.List):
                self._check_targets(node, list(target.elts))
            elif isinstance(target, ast.Attribute) and target.attr == _GUARDED_ATTR:
                self.violation(
                    node,
                    f"assigning {_GUARDED_ATTR} outside {_SINGLE_WRITER}() splits it from the "
                    f"_graded_iteration_count stamp they must always be written together with — a stale "
                    f"counter makes _grade_after_forced_kill re-grade and double-spend every llm_judge / "
                    f"agent_judge criterion. Call self.{_SINGLE_WRITER}(results) instead.",
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_targets(node, list(node.targets))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """An annotated assignment stores just the same -- and slipped the seam."""
        self._check_targets(node, [node.target])
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """`+= [...]` replaces the list contents without stamping the counter."""
        self._check_targets(node, [node.target])
        self.generic_visit(node)
