"""Report rendering: markdown, HTML, JUnit XML, and cross-variant experiment reports.

**This package is a LEAF.** It may import from anywhere in ``coder_eval``; the
core layers may import only its public *writer* entry points. A metric, a
statistic, a serializer or a formatter reached out of here by a core module is a
layering violation — that is what **CE066** enforces, and why
``result_metrics.py``, ``stats.py`` and ``run_record.py`` live outside it.

Intra-package imports name a sibling module directly (``from .markdown import
X``). A submodule must never do ``from . import X`` or ``from coder_eval.reports
import X``: that re-enters this ``__init__`` mid-initialization.

``__all__`` is the public surface. Private names are deliberately absent — tests
that need one import it from its submodule, so the package's API is not a
function of its test suite.
"""

from .experiment import ExperimentReportGenerator
from .helpers import (
    ENV_TABLE_EXCLUDE,
    UNGRADED_SCORE_TEXT,
    PairedComparison,
    VariantSeries,
    collect_variant_series,
    describe_prompt_config,
    format_score,
    is_env_table_key,
    load_variant_eval_results,
    paired_comparison,
)
from .html import (
    HTMLReportGenerator,
    safe_write,
    write_experiment_html,
    write_task_html,
    write_variant_html,
)
from .junit import generate_junit_xml, write_junit_xml
from .markdown import (
    ReportGenerator,
    collect_agent_settings_rows,
    count_partials_by_outcome,
    early_stop_gate_note,
    group_consecutive_by_iteration,
    resolve_agent_settings,
    write_suite_rollups,
)


__all__ = [
    "ENV_TABLE_EXCLUDE",
    "UNGRADED_SCORE_TEXT",
    "ExperimentReportGenerator",
    "HTMLReportGenerator",
    "PairedComparison",
    "ReportGenerator",
    "VariantSeries",
    "collect_agent_settings_rows",
    "collect_variant_series",
    "count_partials_by_outcome",
    "describe_prompt_config",
    "early_stop_gate_note",
    "format_score",
    "generate_junit_xml",
    "group_consecutive_by_iteration",
    "is_env_table_key",
    "load_variant_eval_results",
    "paired_comparison",
    "resolve_agent_settings",
    "safe_write",
    "write_experiment_html",
    "write_junit_xml",
    "write_suite_rollups",
    "write_task_html",
    "write_variant_html",
]
