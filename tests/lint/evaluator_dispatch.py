"""CE044 — the restricted evaluator's operator WHITELIST and its DISPATCH must agree.

``tests/lint/computed_claims.py`` evaluates the optimize surfaces' cost-table expressions with a
restricted ``ast`` walk: ``_ALLOWED_OPS`` says which operators may appear, and
``evaluate_expression``'s ``match`` statements say what each one computes. They are two halves of
one decision, and nothing made them agree — the shipped dispatch ended in

.. code-block:: python

    case _:
        return lhs / rhs

so widening ``_ALLOWED_OPS`` by one line would have made the evaluator compute a cost-table cell
with **division** and report the result as true. That is the failure in the one sensor class whose
entire purpose is catching arithmetic that lies: a wrong number, arrived at silently, presented as
a recomputation.

**What it detects, precisely — two shapes:**

1. An operator named in ``_ALLOWED_OPS`` with no matching ``case ast.<Name>`` anywhere inside
   ``evaluate_expression``. Patterns are collected across **every nested** ``match`` in the
   function, not just the operator dispatch — ``USub`` is matched by the outer
   ``case ast.UnaryOp(op=ast.USub(), ...)`` arm, so a scanner scoped to the inner ``match op:``
   would report a false gap on it from day one.
2. A bare wildcard (``case _:``) inside ``evaluate_expression`` whose body ``return``\\ s. A
   wildcard that *raises* is the intended shape; one that returns is the defect above.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.** It reads the tuple
named ``_ALLOWED_OPS`` and the function named ``evaluate_expression`` **by name**. Renaming either
would leave the rule with nothing to check, so both absences are reported as gaps rather than
passing vacuously — the same failure mode CE026 guards with
``test_action_input_names_reads_the_real_action``. It compares NAMES, never behaviour: a ``case
ast.Div()`` arm that computes ``lhs * rhs`` satisfies this rule completely, and only CE039's own
recomputed claims would catch that.

The intended fix is to widen both halves together: add the operator to ``_ALLOWED_OPS`` **and** a
``case`` that computes it, leaving the wildcard raising.

Wired as ``tests/test_custom_lint.py::TestCE044EvaluatorDispatch`` rather than as a ``BaseRule``,
because its subject is a file under ``tests/`` and the ``ALL_RULES`` sweep runs over ``src/`` only.
"""

from __future__ import annotations

import ast
from pathlib import Path


WHITELIST_NAME = "_ALLOWED_OPS"
EVALUATOR_NAME = "evaluate_expression"


def _attribute_name(node: ast.expr) -> str | None:
    """``ast.Add`` -> ``"Add"``; a bare ``Add`` -> ``"Add"``; anything else -> ``None``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def whitelisted_ops(tree: ast.Module) -> list[str]:
    """The operator class names of the module's top-level ``_ALLOWED_OPS`` tuple, in order."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == WHITELIST_NAME for t in node.targets):
            continue
        if not isinstance(node.value, ast.Tuple):
            return []
        return [name for element in node.value.elts if (name := _attribute_name(element))]
    return []


def _evaluator(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == EVALUATOR_NAME:
            return node
    return None


def handled_ops(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every class name a ``case ast.<Name>(...)`` pattern matches, across all nested ``match``es.

    Nested patterns count: ``case ast.UnaryOp(op=ast.USub(), ...)`` handles ``USub`` as much as a
    top-level ``case ast.USub()`` would, and ``ast.walk`` reaches it because ``MatchClass`` carries
    its sub-patterns as ordinary children.
    """
    return {name for node in ast.walk(fn) if isinstance(node, ast.MatchClass) and (name := _attribute_name(node.cls))}


def returning_wildcards(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    """Lines of every bare ``case _:`` inside ``fn`` whose body returns.

    Any ``return`` counts, bare one included: the intended shape is a wildcard that RAISES, and a
    bare ``return`` hands an unhandled operator back as ``None`` — a wrong answer that merely fails
    somewhere else rather than here.
    """
    lines: list[int] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.match_case):
            continue
        pattern = node.pattern
        if not (isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None):
            continue
        if any(isinstance(stmt, ast.Return) for stmt in ast.walk(node)):
            lines.append(pattern.lineno)
    return lines


def dispatch_gaps(module: Path) -> list[str]:
    """Human-readable gaps between ``_ALLOWED_OPS`` and ``evaluate_expression``; ``[]`` is clean."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    gaps: list[str] = []

    allowed = whitelisted_ops(tree)
    if not allowed:
        gaps.append(
            f"{module.name} declares no top-level `{WHITELIST_NAME}` tuple — the rule reads it by "
            "name, so a rename leaves nothing to check. Restore the name or update CE044."
        )
    fn = _evaluator(tree)
    if fn is None:
        gaps.append(
            f"{module.name} declares no `{EVALUATOR_NAME}` function — the rule reads it by name, "
            "so a rename leaves nothing to check. Restore the name or update CE044."
        )
        return gaps

    handled = handled_ops(fn)
    for name in allowed:
        if name not in handled:
            gaps.append(
                f"{module.name}: `{WHITELIST_NAME}` admits ast.{name} but {EVALUATOR_NAME} has no "
                f"`case ast.{name}` — the whitelist and the dispatch are two halves of one decision "
                "and must be widened together, or the operator is computed as something else"
            )
    for line in returning_wildcards(fn):
        gaps.append(
            f"{module.name}:{line}: a bare `case _:` inside {EVALUATOR_NAME} RETURNS a value. An "
            "unhandled operator would be silently computed as whatever that arm does — make the "
            "wildcard raise and add an explicit `case` for every operator in the whitelist"
        )
    return gaps
