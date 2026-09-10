"""CE018: Don't string-membership-test against ``FinalStatus`` member names.

Comparing a status string against a denylist of ``FinalStatus`` member names
(``s == "SUCCESS"``, ``s in ("FAILURE", "TIMEOUT")``) re-implements the
``FinalStatus.category`` classification by hand. The two drift the moment a new
member is added: the denylist silently misses it (the bug ``_status_badge`` had,
where new budget-exceeded statuses fell through to a ``neutral`` badge). The
``category`` property is the single source of truth — classify via
``FinalStatus(s).category`` instead.

Flagged patterns (must reference ≥1 real FinalStatus member name, so unrelated
string comparisons don't trip):

  * ``s == "SUCCESS"`` / ``!=`` / reversed ``"ERROR" == s``
  * ``s in ("FAILURE", "TIMEOUT")`` / ``not in`` against a tuple/list/set of such names

Add ``# noqa: CE018`` on the offending line for the rare legitimate case
(e.g., asserting against a specific member name in a test).
"""

import ast

from tests.lint.rules.base import BaseRule


# The FinalStatus member NAMES (not the lowercase category values "succeeded" /
# "failed" / "error", which are the correct thing to compare against).
_FINAL_STATUS_NAMES = frozenset(
    {
        "SUCCESS",
        "FAILURE",
        "ERROR",
        "BUILD_FAILED",
        "TIMEOUT",
        "MAX_TURNS_EXHAUSTED",
        "TOKEN_BUDGET_EXCEEDED",
        "COST_BUDGET_EXCEEDED",
        "NOT_GRADED",
    }
)


def _is_string_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


# Cover tuple/list/set literals — `s in ("A",)`, `s in ["A"]`, and `s in {"A"}`
# are all the same denylist re-implementation.
def _is_collection_of_strings(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Tuple | ast.List | ast.Set)
        and bool(node.elts)
        and all(_is_string_literal(e) for e in node.elts)
    )


def _is_final_status_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in _FINAL_STATUS_NAMES


def _collection_has_final_status_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Tuple | ast.List | ast.Set) and any(_is_final_status_name(e) for e in node.elts)


class NoFinalStatusNameDenylist(BaseRule):
    id = "CE018"

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for op, left, right in zip(node.ops, operands[:-1], operands[1:], strict=True):
            self._check_pair(op, left, right, node)
        self.generic_visit(node)

    def _check_pair(self, op: ast.cmpop, left: ast.AST, right: ast.AST, node: ast.AST) -> None:
        eq_form = isinstance(op, ast.Eq | ast.NotEq) and (_is_final_status_name(left) or _is_final_status_name(right))
        in_form = (
            isinstance(op, ast.In | ast.NotIn)
            and _is_collection_of_strings(right)
            and _collection_has_final_status_name(right)
        )
        if eq_form or in_form:
            self._flag(node)

    def _flag(self, node: ast.AST) -> None:
        self.violation(
            node,
            "string-membership test against a FinalStatus member name re-implements "
            "FinalStatus.category — classify via FinalStatus(...).category instead",
        )
