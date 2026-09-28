"""CE066: core may import only the reports package's public WRITERS.

The invariant is not "core must not import reports" — core legitimately *writes* reports.
What must not happen is core reaching into the reports layer for a **metric, a statistic, a
serializer or a formatter**, because that is how a number the evaluation loop needs comes to
live in a rendering module. Those names live in ``result_metrics.py``, ``stats.py`` and
``run_record.py``.

Scope: ``_layers.is_core_path`` (the package, minus ``cli/`` and ``reports/``). The
permitted set is an ALLOWLIST of writers, so a newly added report helper is banned from core
by default. Both the ABSOLUTE and the RELATIVE spelling are checked; the relative form is
the local idiom.

BLIND SPOT: the rule checks the imported NAME, not what is done with it. Importing
``ReportGenerator`` and then reaching through the class for a private helper is invisible.

Rationale: .claude/notes/lint-rules.md § CE066
"""

import ast

from tests.lint.rules._layers import imports_package, is_bare_package_import, is_core_path
from tests.lint.rules.base import BaseRule


# The reports package's public writer entry points — the only names core may import.
ALLOWED_WRITERS = frozenset(
    {
        "ExperimentReportGenerator",
        "ReportGenerator",
        "generate_junit_xml",
        "write_experiment_html",
        "write_junit_xml",
        "write_suite_rollups",
        "write_task_html",
        "write_variant_html",
    }
)

_FIX = (
    "move the metric to result_metrics.py or the statistic to stats.py — "
    "core may import only the reports package's public writers"
)


class NoReportImportsInCore(BaseRule):
    id = "CE066"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_core = is_core_path(filepath)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not self._in_core:
            self.generic_visit(node)
            return
        # `from . import reports`, `from .. import reports`, `from coder_eval import
        # reports`: the package arrives as an alias, so there is no imported NAME to
        # check and every attribute read through it is invisible.
        if is_bare_package_import(node, "reports", self.filepath):
            for alias in node.names:
                if alias.name == "reports":
                    self.violation(
                        node,
                        f"architectural violation: '{alias.name}' (reports layer) imported wholesale "
                        f"into core — import the specific writer instead, or {_FIX}",
                    )
        elif imports_package(node, "reports", self.filepath):
            for alias in node.names:
                if alias.name not in ALLOWED_WRITERS:
                    self.violation(
                        node,
                        f"architectural violation: '{alias.name}' imported from "
                        f"'{'.' * node.level}{node.module}' (reports layer) into core — {_FIX}",
                    )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        # `import coder_eval.reports` binds the whole module, so there is no name
        # to check and every attribute access through it is invisible.
        if self._in_core:
            for alias in node.names:
                if alias.name == "coder_eval.reports" or alias.name.startswith("coder_eval.reports."):
                    self.violation(
                        node,
                        f"architectural violation: '{alias.name}' (reports layer) imported wholesale "
                        f"into core — import the specific writer instead, or {_FIX}",
                    )
        self.generic_visit(node)
