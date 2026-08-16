"""The harness's ONE markdown pipe-table parser.

Extracted from ``computed_claims.py`` on its SECOND consumer — ``estimator_ledger.py``, which
counts the rows of ``docs/REPORT_SCHEMA.md``'s estimator table — rather than on the first,
which is this repo's stated DRY discipline: no duplicated logic, and no premature abstraction
either. Pure extraction: the bodies moved verbatim.

**Stdlib only, and deliberately so.** ``tests.lint.estimator_ledger`` runs in a CI job that
installs nothing, so nothing on this import path may reach for ``pydantic`` or ``coder_eval``.
"""

from __future__ import annotations

from typing import NamedTuple


class MarkdownTable(NamedTuple):
    """One pipe table, with enough position to name it in a failure message."""

    header: list[str]
    rows: list[list[str]]
    line: int  # 1-based line of the header row


def parse_markdown_tables(text: str) -> list[MarkdownTable]:
    """Every pipe table in ``text``, skipping separator rows and anything inside a ``` fence.

    Reads RAW text rather than ``_normalized`` text, deliberately: a table is a line structure and
    collapsing whitespace destroys it. That is legal because
    ``test_no_sensor_inlines_the_normalization_idiom`` is scoped to ``tests/test_custom_lint.py``
    by its own ``Path(__file__)`` — do not "fix" this to use ``_normalized``.
    """
    tables: list[MarkdownTable] = []
    header: list[str] | None = None
    rows: list[list[str]] = []
    header_line = 0
    fenced = False

    def _flush() -> None:
        nonlocal header, rows
        if header is not None and rows:
            tables.append(MarkdownTable(header=header, rows=rows, line=header_line))
        header, rows = None, []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("```"):
            fenced = not fenced
            _flush()
            continue
        if fenced or not (line.startswith("|") and line.endswith("|")):
            _flush()
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") and c for c in cells):
            continue  # the ---|--- separator row
        if header is None:
            header, header_line = cells, lineno
        else:
            rows.append(cells)
    _flush()
    return tables


def table_signature(table: MarkdownTable) -> str:
    """The joined header cells — what ``covers`` names.

    Stable under a body edit (a new row, a corrected figure) and changes when the table's SHAPE
    changes, which is exactly when a claim written against it needs rewriting.
    """
    return " | ".join(table.header)
