"""CE004: core layers must not import from coder_eval.cli.

The "core" layer comprises every package that should be usable without the
CLI: criteria/, evaluation/, models/, simulation/, scoring/, streaming/,
errors/, orchestration/, agents/. Importing from coder_eval.cli
creates an upward dependency that breaks testability in isolation.

Note: this is a single, narrow rule (no upward imports into cli). For a
fully layered import graph (no upward imports between any layers), evaluate
import-linter / grimp — purpose-built for that. CE004 is the cheap version
that catches the one mistake we have actually seen.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_CORE_DIRS = re.compile(
    r"[/\\](criteria|evaluation|models|simulation|scoring|streaming|errors|orchestration|agents)[/\\]"
)
_BANNED = re.compile(r"^coder_eval\.cli")


class NoCliImportsInCore(BaseRule):
    id = "CE004"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_core = bool(_CORE_DIRS.search(filepath))

    def check_import(self, node: ast.ImportFrom, module: str | None) -> None:
        # `module` arrives with `node.level` already resolved (see `BaseRule.check_import`). CE004
        # was the one rule whose exposure was inconclusive in the spike, and it IS affected: a core
        # module reaching sideways with `from ..cli.console import console` read as `"cli.console"`
        # and never matched.
        if self._in_core and module and _BANNED.match(module):
            self.violation(
                node,
                f"architectural violation: '{module}' (cli layer) imported from core layer",
            )

    def visit_Import(self, node: ast.Import) -> None:
        if self._in_core:
            for alias in node.names:
                if _BANNED.match(alias.name):
                    self.violation(
                        node,
                        f"architectural violation: '{alias.name}' (cli layer) imported from core layer",
                    )
        self.generic_visit(node)
