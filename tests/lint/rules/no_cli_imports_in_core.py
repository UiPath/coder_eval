"""CE004: core layers must not import from ``coder_eval.cli``.

An upward dependency on ``coder_eval.cli`` breaks testability in isolation. Scope is
everything under ``src/coder_eval/`` except ``cli/`` itself, via ``_layers`` so CE004 and
CE066 cannot drift apart about where the boundary is.

``reports/`` is in scope, unlike under CE066: it runs without the CLI, so a ``cli`` import
there closes a ``cli -> orchestration -> reports -> cli`` cycle. ``harbor/`` is in scope for
the same reason ``orchestration/`` is. Both the absolute and the RELATIVE spelling are
checked; see ``_layers.imports_package``.

BLIND SPOT: one narrow rule, not a layered import graph. For that, evaluate import-linter /
grimp.

Rationale: .claude/notes/lint-rules.md § CE004
"""

import ast

from tests.lint.rules._layers import imports_package, is_bare_package_import, is_cli_path, is_package_path
from tests.lint.rules.base import BaseRule


class NoCliImportsInCore(BaseRule):
    id = "CE004"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = is_package_path(filepath) and not is_cli_path(filepath)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Both spellings: `from coder_eval.cli import x` AND `from ..cli import x`.
        if self._in_scope and (
            imports_package(node, "cli", self.filepath) or is_bare_package_import(node, "cli", self.filepath)
        ):
            named = f"{'.' * node.level}{node.module or 'cli'}"
            self.violation(
                node,
                f"architectural violation: '{named}' (cli layer) imported from core layer",
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self._in_scope:
            for alias in node.names:
                if alias.name == "coder_eval.cli" or alias.name.startswith("coder_eval.cli."):
                    self.violation(
                        node,
                        f"architectural violation: '{alias.name}' (cli layer) imported from core layer",
                    )
        self.generic_visit(node)
