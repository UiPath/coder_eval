"""CE016: never pass ``input_tokens=`` / ``total_tokens=`` to ``TokenUsage(...)``.

After the uncached/derived split, ``TokenUsage.input_tokens`` and
``total_tokens`` are read-only (a ``@computed_field`` / ``@property`` summing
``uncached_input_tokens`` + the cache buckets). Passing either as a constructor
kwarg is SILENTLY DROPPED by Pydantic — the value never lands, and the object is
built with ``uncached_input_tokens = 0``. This already bit a test
(``TokenUsage(input_tokens=1000, ...)`` constructed zero input and only passed
because it asserted something else), and a real caller doing the same would
under-count input with no error.

The stored, billable slice is ``uncached_input_tokens`` — pass that instead.

This rule is scoped to the ``TokenUsage`` callee specifically: per-message models
(``AssistantMessage`` / ``ReconciliationMessage``) DO have a real, settable
``input_tokens`` field (the uncached slice), so ``input_tokens=`` is valid there.
"""

import ast

from tests.lint.rules.base import BaseRule


_FORBIDDEN_KWARGS = {"input_tokens", "total_tokens"}


def _is_token_usage(func: ast.expr) -> bool:
    """Return True for a ``TokenUsage(...)`` call (bare name or attribute access)."""
    if isinstance(func, ast.Name):
        return func.id == "TokenUsage"
    if isinstance(func, ast.Attribute):
        return func.attr == "TokenUsage"
    return False


class NoComputedTokenUsageKwargs(BaseRule):
    id = "CE016"

    def visit_Call(self, node: ast.Call) -> None:
        if _is_token_usage(node.func):
            for kw in node.keywords:
                if kw.arg in _FORBIDDEN_KWARGS:
                    self.violation(
                        node,
                        f"TokenUsage({kw.arg}=...) is silently dropped — {kw.arg} is a derived "
                        "computed field, not a settable input. Pass uncached_input_tokens= "
                        "(the billable slice) instead.",
                    )
        self.generic_visit(node)
