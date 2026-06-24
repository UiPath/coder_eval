"""Committed expected-output snapshots for the report generators.

Characterization safety net for decomposing ``ReportGenerator.generate_markdown``
and ``ExperimentReportGenerator.generate_experiment_report``: each test builds a
representative model, renders it, and asserts the output is byte-identical to a
committed ``.md`` snapshot via ``assert_matches_snapshot``. The decompositions
must keep these byte-identical.
"""

from tests._fixtures.report_snapshots._snapshot import assert_matches_snapshot


__all__ = ["assert_matches_snapshot"]
