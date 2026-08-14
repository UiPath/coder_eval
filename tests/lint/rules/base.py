import ast
from abc import ABC

from tests.lint.violation import Violation


class BaseRule(ast.NodeVisitor, ABC):
    """Base class for all lint rules. Each rule is an AST visitor."""

    id: str = ""

    #: Physical source lines of the tree being checked. ``runner.check_file``
    #: assigns this after construction (a CLASS attribute rather than an
    #: ``__init__`` parameter so the many rules that override ``__init__``
    #: keep working unchanged). A rule needing raw text -- comments are absent
    #: from the AST, so ``# noqa`` markers are only visible here -- must read
    #: this instead of re-opening ``filepath``: a rule fed a synthetic tree
    #: with a real-looking path would otherwise scan an unrelated file, a
    #: silent false positive that also depends on the process's cwd. ``None``
    #: means the caller supplied only a path; degrade to doing nothing.
    source_lines: list[str] | None = None

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
