"""CE066: core may import only the reports package's public WRITERS.

The invariant is not "core must not import reports" — core legitimately *writes*
reports: ``orchestrator.py`` writes the per-task HTML and ``orchestration/batch.py``
drives ``ReportGenerator``. What must not happen is core reaching into the reports
layer for a **metric, a statistic, a serializer or a formatter**, because that is
how a number the evaluation loop needs comes to live in a rendering module.

Before the split that was the actual shape of the code: the orchestrator imported
``turn_time_buckets`` and ``visible_turn_count`` from ``reports_stats``, and
``orchestration/batch.py`` imported the run.json row serializer from
``reports_experiment``. Those names now live in ``result_metrics.py``, ``stats.py``
and ``run_record.py``, and this rule is what stops the next one drifting back.

An ALLOWLIST, not a denylist — the CE018 rationale. A newly added report helper is
banned from core by default rather than after someone notices. The list is purely
writers; ``eval_result_to_task_dict`` is deliberately absent, because carrying a
serializer on it would be the rule documenting a wart instead of the wart being
removed.

Both the ABSOLUTE and the RELATIVE spelling are checked. That is not a detail:
the relative form is the local idiom — both surviving edges in the tree are
``from .reports import write_task_html`` (orchestrator.py) and ``from ..reports
import ReportGenerator`` (orchestration/batch.py) — and an earlier draft of this
rule matched only ``node.module``, which for a relative import holds
``"reports"`` with the dots in ``node.level``. It therefore fired on nothing the
codebase actually writes, and its own tests passed because they used the
absolute form. An unrun assertion is documentation, not enforcement.

**Blind spot, stated deliberately:** the rule checks the imported NAME, not what
is done with it. ``from coder_eval.reports import ReportGenerator`` followed by
reaching through the class for a private helper is invisible here. That is the
cheap version, consistent with CE004's own "catches the one mistake we have
actually seen" note.
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
        if is_bare_package_import(node, "reports"):
            for alias in node.names:
                if alias.name == "reports":
                    self.violation(
                        node,
                        f"architectural violation: '{alias.name}' (reports layer) imported wholesale "
                        f"into core — import the specific writer instead, or {_FIX}",
                    )
        elif imports_package(node, "reports"):
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
