"""CE004: core layers must not import from coder_eval.cli.

The "core" layer comprises every package that should be usable without the
CLI: criteria/, evaluation/, models/, simulation/, scoring/, streaming/,
errors/, orchestration/, agents/, harbor/, plus top-level orchestrator.py.
Importing from coder_eval.cli creates an upward dependency that breaks
testability in isolation. The membership test lives in ``_layers.is_core_path``
so CE004 and CE066 cannot drift apart about what "core" means.

``harbor/`` joined this list for the same reason ``orchestration/`` is on it:
its reward writer wants to raise a plain exception (``RewardWriteSkippedError``,
or the re-exported ``RegradeError``) and let the CLI wrap it into an exit
code — exactly the ``orchestration/regrade.py`` -> ``evaluate`` shape.

Both the absolute and the RELATIVE spelling are checked — see
``_layers.imports_package`` for why that distinction is load-bearing rather than
pedantic.

Note: this is a single, narrow rule (no upward imports into cli). For a
fully layered import graph (no upward imports between any layers), evaluate
import-linter / grimp — purpose-built for that. CE004 is the cheap version
that catches the one mistake we have actually seen.
"""

import ast

from tests.lint.rules._layers import imports_package, is_bare_package_import, is_core_path
from tests.lint.rules.base import BaseRule


class NoCliImportsInCore(BaseRule):
    id = "CE004"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_core = is_core_path(filepath)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Both spellings: `from coder_eval.cli import x` AND `from ..cli import x`.
        if self._in_core and (imports_package(node, "cli") or is_bare_package_import(node, "cli")):
            named = f"{'.' * node.level}{node.module or 'cli'}"
            self.violation(
                node,
                f"architectural violation: '{named}' (cli layer) imported from core layer",
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self._in_core:
            for alias in node.names:
                if alias.name == "coder_eval.cli" or alias.name.startswith("coder_eval.cli."):
                    self.violation(
                        node,
                        f"architectural violation: '{alias.name}' (cli layer) imported from core layer",
                    )
        self.generic_visit(node)
