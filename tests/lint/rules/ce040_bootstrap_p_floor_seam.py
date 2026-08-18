"""CE040: the bootstrap's p-floor may be derived in exactly one place — ``reports_stats.py``.

The smallest two-sided p :func:`reports_stats.cluster_bootstrap_diff_ci` can return is a
property of its *estimator*, not a fact about arithmetic in general. Under the Phipson & Smyth
``(b+1)/(m+1)`` form it is ``2/(m+1)``; under the naive count it replaced it was ``1/m``. The
value therefore MOVES when the estimator changes, and every consumer that re-derived it kept
reporting the old one.

That is not hypothetical — it is what this rule was extracted from. Four sites in
``optimize/gate.py`` and two field descriptions in ``models/optimize.py`` spelled ``1/n_resamples``
inline; when the estimator changed they all silently understated the floor by 2x, in a verdict
block a user reads to decide whether to spend money on another round. They were found by grep in
review, one at a time. :func:`reports_stats.bootstrap_p_floor` now names the value once, and this
rule is what keeps it named once.

**What it detects, precisely: division by a resample count.** An ``ast.BinOp`` division whose
denominator is ``n_resamples`` — as a bare name, an attribute (``verdict.n_resamples``), or that
name plus a constant (``n_resamples + 1``) — anywhere outside ``reports_stats.py``. That covers
both the old shape and a "helpful" re-derivation of the new one.

It is a shape check and the boundary is worth stating so nobody trusts it further than it goes:
a floor computed from a differently-named local (``m``, ``draws``) is NOT matched, and the scan
covers ``src/`` only. It catches the way the mistake actually arrives — reaching for the count
that is already in scope — not every possible re-derivation.

Add ``# noqa: CE040`` on the line only if a module genuinely must divide by the resample count
for something that is not the p-floor (say, a rate per draw), and say why. The intended fix is to
call ``bootstrap_p_floor(n_resamples)``.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_CANONICAL_MODULE = re.compile(r"[/\\]reports_stats\.py$")
_RESAMPLE_COUNT = "n_resamples"


def _is_resample_count(node: ast.expr) -> bool:
    """True for ``n_resamples`` however it is reached: bare, attribute, or offset by a constant."""
    if isinstance(node, ast.Name):
        return node.id == _RESAMPLE_COUNT
    if isinstance(node, ast.Attribute):
        return node.attr == _RESAMPLE_COUNT
    # `n_resamples + 1` / `n_resamples - 1`: the offset form the current estimator uses.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Sub):
        return _is_resample_count(node.left) or _is_resample_count(node.right)
    return False


class BootstrapPFloorSeam(BaseRule):
    id = "CE040"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._is_canonical = bool(_CANONICAL_MODULE.search(filepath))

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not self._is_canonical and isinstance(node.op, ast.Div) and _is_resample_count(node.right):
            self.violation(
                node,
                "the bootstrap's p-floor is re-derived here. It is a property of "
                "cluster_bootstrap_diff_ci's estimator, not of arithmetic — it was 1/n_resamples "
                "under the naive count and is 2/(n_resamples+1) under Phipson & Smyth — so a copy "
                "keeps reporting the old value after the estimator moves. Call "
                "reports_stats.bootstrap_p_floor(n_resamples) instead.",
            )
        self.generic_visit(node)
