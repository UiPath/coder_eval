"""CE035: a criterion checker must not book an IO/config error as score 0.0.

``CriterionResult(score=0.0)`` means *the agent did the work and it was wrong*.
It is gating (``all_criteria_passed`` is a strict AND) and it flows into every
downstream count that consumes criterion scores: ``CriterionAggregate``
mean/median, ``suite_thresholds`` gates on dataset-fanned suites, run and
experiment pass rates, the JUnit report, the evalboard.

An ``except OSError`` around a file the TASK AUTHOR named is not that. The
motivating case: a typo in ``reference_comparison.reference_file`` raised
``FileNotFoundError`` (an ``OSError``), got turned into a gating 0.0, and was
counted against the agent's pass rate — silently zeroing every row of a
dataset-fanned suite while looking like a genuine similarity failure.

Raise ``CheckerMisuseError`` instead. ``criteria/base.py``'s
``_ESCALATING_EXCEPTIONS`` routes it to ``FinalStatus.ERROR``, which is what an
eval-config error is.

Fires only on a ``return CriterionResult(...)`` with a literal ``score=0.0``
lexically inside an ``except`` handler for ``OSError`` / ``FileNotFoundError`` /
``PermissionError`` / ``IsADirectoryError``, in ``coder_eval/criteria/``. A
failure attributable to the AGENT's own output (its file is missing, its JSON is
malformed) is legitimately 0.0 — mark those ``# noqa: CE035`` with a one-line
reason.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_IO_EXCEPTIONS = {"OSError", "IOError", "FileNotFoundError", "PermissionError", "IsADirectoryError"}


def _handles_io(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    if caught is None:
        return False
    candidates = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(isinstance(c, ast.Name) and c.id in _IO_EXCEPTIONS for c in candidates)


def _is_zero_score_result(node: ast.stmt) -> ast.Call | None:
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
        return None
    call = node.value
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if not name.endswith("CriterionResult"):
        return None
    for kw in call.keywords:
        if kw.arg == "score" and isinstance(kw.value, ast.Constant) and kw.value.value == 0.0:
            return call
    return None


class ConfigErrorEscalates(BaseRule):
    id = "CE035"

    _CRITERIA_PATH = re.compile(r"[/\\]coder_eval[/\\]criteria[/\\]")

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(self._CRITERIA_PATH.search(filepath))

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if self._in_scope and _handles_io(node):
            for stmt in ast.walk(node):
                if not isinstance(stmt, ast.stmt):
                    continue
                offender = _is_zero_score_result(stmt)
                if offender is not None:
                    self.violation(
                        stmt,
                        "an IO failure on a path the TASK AUTHOR named is returned as a gating score=0.0, "
                        + "so an eval-config error is booked as an agent failure (and on a dataset-fanned "
                        + "suite, zeroes every row). Raise CheckerMisuseError so it routes to "
                        + "FinalStatus.ERROR; use # noqa: CE035 when the failure really is the agent's",
                    )
        self.generic_visit(node)
