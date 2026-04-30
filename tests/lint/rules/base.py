import ast
from abc import ABC

from tests.lint.violation import Violation


class BaseRule(ast.NodeVisitor, ABC):
    """Base class for all lint rules. Each rule is an AST visitor."""

    id: str = ""

    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.violations: list[Violation] = []

    def violation(self, node: ast.AST, message: str) -> None:
        self.violations.append(
            Violation(
                rule_id=self.id,
                file=self.filepath,
                line=getattr(node, "lineno", 0),
                col=getattr(node, "col_offset", 0),
                message=message,
                end_line=getattr(node, "end_lineno", 0) or 0,
            )
        )

    def check(self, tree: ast.AST) -> list[Violation]:
        self.visit(tree)
        return self.violations
