"""CE012: Don't dispatch on ``type(x).__name__`` equality against a string literal.

String-name dispatch silently breaks the moment a subclass is introduced:
``isinstance(msg, SystemMessage)`` matches a ``TaskStartedMessage`` subclass,
but ``type(msg).__name__ == "SystemMessage"`` does not. Library authors
routinely extend public message classes with subclasses, documenting them as
drop-in replacements — name-equality defeats that contract and the breakage is
invisible until a runtime error surfaces (or, worse, until downstream parsers
get garbled input).

Flagged patterns:

  * ``type(x).__name__ == "Foo"`` / ``!=`` / membership-in tuples of literals
  * Reversed: ``"Foo" == type(x).__name__``

Use ``isinstance(x, Foo)`` instead.

Add ``# noqa: CE012`` on the offending line for the rare legitimate case
(e.g., comparing against a string read from data, or in a debug log path
where subclass dispatch is unwanted).
"""

import ast

from tests.lint.rules.base import BaseRule


def _is_type_dot_name(node: ast.AST) -> bool:
    """True for the AST shape ``type(<expr>).__name__``."""
    if not isinstance(node, ast.Attribute) or node.attr != "__name__":
        return False
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    if not isinstance(call.func, ast.Name) or call.func.id != "type":
        return False
    return len(call.args) == 1 and not call.keywords


def _is_string_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_tuple_of_strings(node: ast.AST) -> bool:
    return isinstance(node, ast.Tuple) and bool(node.elts) and all(_is_string_literal(e) for e in node.elts)


class NoTypeNameStringDispatch(BaseRule):
    id = "CE012"

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        # Pair each operator with its (left, right) operands.
        for op, left, right in zip(node.ops, operands[:-1], operands[1:], strict=True):
            self._check_pair(op, left, right, node)
        self.generic_visit(node)

    def _check_pair(self, op: ast.cmpop, left: ast.AST, right: ast.AST, node: ast.AST) -> None:
        eq_form = isinstance(op, ast.Eq | ast.NotEq) and (
            (_is_type_dot_name(left) and _is_string_literal(right))
            or (_is_string_literal(left) and _is_type_dot_name(right))
        )
        in_form = isinstance(op, ast.In | ast.NotIn) and _is_type_dot_name(left) and _is_tuple_of_strings(right)
        if eq_form or in_form:
            self._flag(node)

    def _flag(self, node: ast.AST) -> None:
        self.violation(
            node,
            "dispatching on type(x).__name__ against a string literal is "
            "subclass-blind — use isinstance(x, Foo) instead",
        )
