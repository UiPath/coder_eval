import ast
from abc import ABC

from tests.lint.import_resolution import resolved_module
from tests.lint.violation import Violation


class BaseRule(ast.NodeVisitor, ABC):
    """Base class for all lint rules. Each rule is an AST visitor.

    **Override :meth:`check_import`, never ``visit_ImportFrom``.** The base visitor resolves
    ``node.level`` and hands the result down, which makes the correct path the DEFAULT path: a rule
    matching on ``node.module`` alone is silent on ``from ..models import X`` — the spelling most of
    ``src/`` uses — and it fails OPEN, reporting nothing, which is indistinguishable from a clean
    tree. Five rules were broken that way at once while ``make lint`` stayed green. CE051 keeps a
    sixth from joining them and now also forbids a rule under ``tests/lint/rules/`` from defining
    its own ``visit_ImportFrom``.
    """

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

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Resolve the import once, for every rule, then descend.

        ``generic_visit`` is required rather than tidy: ``ast.NodeVisitor`` stops descending as soon
        as a ``visit_X`` is defined, and rules that also inspect nodes inside an import's subtree
        would silently stop seeing them.
        """
        self.check_import(node, resolved_module(node, self.filepath))
        self.generic_visit(node)

    def check_import(self, node: ast.ImportFrom, module: str | None) -> None:
        """Override to inspect an import.

        ``module`` is the ABSOLUTE dotted module with ``node.level`` already resolved. ``None``
        means it could not be resolved — the file sits outside a ``coder_eval/`` package root, or
        the import walks above it — and a rule must NOT fire then: an import the resolver cannot
        place is one whose module string would be fiction, and a false violation on correct code is
        worse than a missed one. See :mod:`tests.lint.import_resolution`.
        """

    def check(self, tree: ast.AST) -> list[Violation]:
        self.visit(tree)
        return self.violations
