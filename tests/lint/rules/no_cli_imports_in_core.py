"""CE004: core layers must not import from coder_eval.cli.

The rule's scope is everything under src/coder_eval/ except the cli/ package
itself. Importing from coder_eval.cli creates an upward dependency that breaks
testability in isolation. The package anchor and the cli/ boundary live in
``_layers`` so CE004 and CE066 cannot drift apart about where either is;
re-enumerating the packages here is how that list rots.

``reports/`` is in scope, unlike under CE066. The reports package runs without
the CLI — the orchestrator writes a task report mid-run — so a ``cli`` import
there closes a cli -> orchestration -> reports -> cli cycle. CE004 once borrowed
CE066's core predicate whole and inherited its ``reports/`` exemption; nothing
had imported ``cli`` from there yet, so the hole was latent rather than live.

``harbor/`` is in scope for the same reason ``orchestration/`` is:
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

from tests.lint.rules._layers import imports_package, is_bare_package_import, is_cli_path, is_package_path
from tests.lint.rules.base import BaseRule


class NoCliImportsInCore(BaseRule):
    id = "CE004"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = is_package_path(filepath) and not is_cli_path(filepath)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Both spellings: `from coder_eval.cli import x` AND `from ..cli import x`.
        if self._in_scope and (imports_package(node, "cli") or is_bare_package_import(node, "cli")):
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
