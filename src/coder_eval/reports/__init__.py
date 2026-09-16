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
    UNGRADED_SCORE_TEXT,
    collect_variant_series,
    describe_prompt_config,
    format_score,
    is_env_table_key,
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
    write_suite_rollups,
)


__all__ = [
    "UNGRADED_SCORE_TEXT",
    "ExperimentReportGenerator",
    "HTMLReportGenerator",
    "ReportGenerator",
    "collect_agent_settings_rows",
    "collect_variant_series",
    "describe_prompt_config",
    "format_score",
    "generate_junit_xml",
    "is_env_table_key",
    "safe_write",
    "write_experiment_html",
    "write_junit_xml",
    "write_suite_rollups",
    "write_task_html",
    "write_variant_html",
]
