"""Shared snapshot loader/asserter for committed report-output characterization tests.

Mirrors the golden-master ``GOLDEN_REGEN`` flow (``tests/_fixtures/golden_streams``)
but for plain-string report output: ``generate_markdown`` /
``generate_experiment_report`` are pure functions of their input models, so their
rendered text is byte-stable and snapshotted verbatim with no scrubbing.

Regenerate the fixtures after an INTENTIONAL report-content change with::

    REPORT_SNAPSHOT_REGEN=1 uv run pytest tests/test_reports.py tests/test_experiment_reports.py

and review the resulting ``.md`` diff before committing.
"""

from __future__ import annotations

import os
from pathlib import Path


_SNAPSHOT_DIR = Path(__file__).parent
_REGEN = os.environ.get("REPORT_SNAPSHOT_REGEN", "").strip().lower() in {"1", "true", "yes", "on"}


def assert_matches_snapshot(actual: str, name: str) -> None:
    """Compare ``actual`` against the committed ``<name>`` snapshot, or regenerate it.

    When ``REPORT_SNAPSHOT_REGEN`` is set, writes ``actual`` to the fixture instead
    of comparing — the one-time bootstrap / intentional-change flow.
    """
    path = _SNAPSHOT_DIR / name
    if _REGEN:
        # Write a trailing newline so the committed fixture satisfies the POSIX
        # end-of-file convention (and the end-of-file-fixer pre-commit hook leaves
        # it untouched). The report output itself is a `"\n".join(...)` with no
        # trailing newline, so the single conventional newline is normalised away
        # on compare below.
        path.write_text(actual + "\n", encoding="utf-8")
        return

    assert path.exists(), (
        f"Missing report snapshot {path.name}. Generate it once with "
        f"`REPORT_SNAPSHOT_REGEN=1 uv run pytest tests/test_reports.py tests/test_experiment_reports.py` "
        f"and commit it."
    )
    expected = path.read_text(encoding="utf-8").removesuffix("\n")
    assert actual == expected, (
        f"Report snapshot drift for {name!r}. The report output changed.\n"
        f"If this change is intentional, regenerate with REPORT_SNAPSHOT_REGEN=1 and review the diff."
    )
