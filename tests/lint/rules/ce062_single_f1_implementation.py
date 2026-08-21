"""CE062: F1 may be computed in exactly one place — ``criteria/_classification_aggregate.py``.

``classification_metrics()`` owns the harmonic-mean arithmetic *and its div-by-zero
convention* (precision / recall / F1 are ``0.0``, never ``NaN``, when the denominator is 0;
:meth:`ClassificationMetrics.metric` extends that to an absent metric name). Every number a
run reports — ``suite.json``'s ``metrics["f1.yes"]``, the suite gate, the optimize gate's
cluster bootstrap — comes from there.

A second implementation is the failure this rule exists to prevent: it looks correct, agrees
with the first on ordinary input, and diverges exactly where a class has no predictions or no
true instances. A gate computing its own F1 would then disagree with the numbers the run
itself reported, silently, in the direction nobody checks.

**What it detects, precisely: the harmonic-mean shape.** An ``ast.BinOp`` division whose
numerator multiplies the literal ``2`` by two distinct operands and whose denominator adds
those same two operands. An operand may be a name, an attribute or a subscript
(``precision``, ``self.precision``, ``m["precision"]``), so the usual ways of spelling the
same formula are covered. The ternary guard that normally wraps the idiom is deliberately NOT
required — a copy that drops the guard is still a second implementation, and the more
dangerous one, since it raises where the canonical form applies the 0.0 convention.

It is a shape check, not a semantic one, and the boundary is worth stating so nobody trusts
it further than it goes: algebraically equivalent rewrites (``tp / (tp + 0.5 * (fp + fn))``,
``2 / (1 / p + 1 / r)``) are NOT matched, and the scan covers ``src/`` only. It catches the
copy-paste, which is the way a second implementation actually arrives; it is not a proof that
no other code computes F1.

Add ``# noqa: CE062`` on the offending line only if a module genuinely must compute its own
(and say why) — the intended fix is to call ``classification_metrics``. A legitimate harmonic
mean of two non-classification quantities is the other reason to suppress.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_CANONICAL_MODULE = re.compile(r"[/\\]criteria[/\\]_classification_aggregate\.py$")
_OPERAND_TYPES = (ast.Name, ast.Attribute, ast.Subscript)


def _key(node: ast.expr) -> str | None:
    """A comparable identity for an operand, so `p` in the numerator and `p` in the
    denominator are recognized as the same value however they are spelled."""
    return ast.dump(node) if isinstance(node, _OPERAND_TYPES) else None


def _mult_operands(node: ast.expr) -> list[ast.expr]:
    """Flatten a left-nested chain of ``*`` into its operands."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _mult_operands(node.left) + _mult_operands(node.right)
    return [node]


def _harmonic_numerator_keys(node: ast.expr) -> set[str] | None:
    """The two operand keys in ``2 * <a> * <b>`` (any nesting/order), else ``None``."""
    operands = _mult_operands(node)
    if len(operands) != 3:
        return None
    twos = [o for o in operands if isinstance(o, ast.Constant) and o.value == 2]
    keys = [k for o in operands if (k := _key(o)) is not None]
    if len(twos) != 1 or len(keys) != 2 or keys[0] == keys[1]:
        return None
    return set(keys)


def _sum_keys(node: ast.expr) -> set[str] | None:
    """The two operand keys in ``<a> + <b>``, else ``None``."""
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
        return None
    left, right = _key(node.left), _key(node.right)
    if left is None or right is None or left == right:
        return None
    return {left, right}


class SingleF1Implementation(BaseRule):
    id = "CE062"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._is_canonical = bool(_CANONICAL_MODULE.search(filepath))

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not self._is_canonical and isinstance(node.op, ast.Div):
            numerator = _harmonic_numerator_keys(node.left)
            if numerator is not None and numerator == _sum_keys(node.right):
                self.violation(
                    node,
                    "second F1 implementation: `2 * p * r / (p + r)` is computed here as well as in "
                    "criteria/_classification_aggregate.py::classification_metrics, which owns the "
                    "arithmetic AND its 0.0 div-by-zero convention. Call classification_metrics(pairs) "
                    "instead — a second copy diverges exactly where a class has no predictions, and "
                    "the gate then disagrees with the numbers the run reported.",
                )
        self.generic_visit(node)
