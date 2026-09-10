"""CE049: never coalesce a possibly-unmeasured score to a numeric literal.

``weighted_score is None`` means *nothing measured this row*, and it is a
different fact from ``weighted_score == 0.0``, which means *this row was measured
and scored nothing*. ``score or 0.0`` erases that difference — and it does it
silently, producing a real-looking number that every downstream consumer treats
as a genuine miss.

The motivating bug: ``build_task_event`` published ``Score = float(
result.weighted_score or 0.0)`` on every ``CoderEval.Task.End``. Four shipped
App Insights tiles compute ``avg(todouble(customDimensions.Score))`` with no
status filter, so one ``coder-eval execute`` night dragged every score tile
toward zero, indistinguishable from a genuinely bad night. The hazard was
already documented in prose in ``orchestrator.py`` ("every downstream
`score or 0.0` would launder it into a real-looking failure") — this rule makes
it mechanical.

Fires on ``<name> or <numeric literal>`` where the left operand's trailing name
looks like a score or a rate. The fix is to omit the value, keep it ``None``, or
branch explicitly on ``is None``.

``# noqa: CE049`` for a genuinely aggregate-internal use where a missing value
really is a miss — e.g. summing a variant's scores where an errored row must
count as 0.0 (see ``orchestration/experiment._measured_scores``, which makes that
decision explicitly and states why).
"""

import ast
import re

from tests.lint.rules.base import BaseRule


# Trailing-segment match, so `result.weighted_score`, `v.average_score` and
# `summary.pass_rate` all fire while `sample_rate_limit` does not.
_SCORE_NAME = re.compile(r"^(weighted_score|score|average_score|pass_rate|[a-z_]*_rate)$")


def _scoreish(node: ast.expr) -> str | None:
    """The name of a score/rate-looking operand, or None."""
    if isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Name):
        name = node.id
    else:
        return None
    return name if _SCORE_NAME.match(name) else None


def _numeric_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool)


class NoScoreOrZero(BaseRule):
    id = "CE049"

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if isinstance(node.op, ast.Or) and len(node.values) == 2:
            name = _scoreish(node.values[0])
            if name is not None and _numeric_literal(node.values[1]):
                self.violation(
                    node,
                    f"{name!r} is coalesced to a numeric literal. `None` means the row was never "
                    + "measured; a literal means it was measured and scored that value — and the "
                    + "coalesce publishes the second while meaning the first, which is how an "
                    + "ungraded run reached four avg(Score) dashboards as a real zero. Omit the "
                    + "value, keep None, or branch on `is None`. Use `# noqa: CE049` where a "
                    + "missing value genuinely IS a miss, with a comment saying so.",
                )
        self.generic_visit(node)
