"""The estimator-change protocol: a PR that moves a statistic must say so in the ledger.

A rendered statistic can step for **identical data** when an estimator or a resample count
changes, and nothing in a run artifact distinguishes that from a real change in the thing being
measured. It has happened: ``b306a99`` set ``BOOTSTRAP_RESAMPLES = 2000`` as the one resample count
and moved **two** CI upper bounds in the experiment report — ``[0.850, 0.933]`` becoming
``[0.850, 0.950]`` and ``[0.600, 0.683]`` becoming ``[0.600, 0.700]`` in a snapshot fixture. A
consumer recomputing intervals from ``per_replicate_scores`` — which ``docs/REPORT_SCHEMA.md``
tells them to do — has no way to attribute that after the fact unless someone wrote it down.

So: when a PR's diff touches a **watched constant's assignment** or a **rendered-number snapshot
fixture**, the ``## Estimator changes`` table in ``docs/REPORT_SCHEMA.md`` must gain a row.

**Why a row COUNT rather than "the file was touched".** The ledger lives in a busy page, so a
touched-file test would be satisfied by a typo fix three sections away. Comparing the table's data
row count between the merge base and the working copy is what makes it a real gate.

**The boundary, stated so a green job is not mistaken for a proof.**

* It watches constant ASSIGNMENTS, not estimator FORMS. Changing the expression inside
  ``reports_stats.bootstrap_p_floor`` — which has already happened once, ``1/m`` becoming
  ``2/(m+1)`` — is not matched **directly**. The fixture half is what backstops it: that floor is
  rendered into ``tests/_fixtures/optimize_renders/``, so the change lands there and IS caught.
  A form change that reaches no pinned fixture is genuinely invisible.
* The diff match is ``^[+-]\\s*<NAME>\\s*[:=]``, so a comment reflow beside a constant does not
  fire, and a value changed through an expression on another line is not caught. The ``:``
  alternative is what admits an annotated re-declaration (``NAME: Final[int] = …``); a test pins
  separately that every watched constant's REAL source line still matches, because the regex is a
  shape check and not an understanding of Python.
* Only ``.md`` / ``.json`` fixtures count, and only MODIFIED ones. A brand-new fixture has no
  "before" and therefore no step; the directories' ``__init__.py`` / helper modules carry no
  rendered numbers.
* A row EDITED rather than added does not raise the count, so a pure-correction PR fails. That is
  intended for a PR that also changes an estimator; the failure message names the escape hatch.
* It pins that a row EXISTS, never that the row is true.

**This check cannot run in ``make verify``** — it is diff-based, and a working tree has no base
ref. It runs as the ``pull_request``-only ``estimator-protocol`` job. Everything above
``__main__`` is pure and unit-tested (``tests/lint_tests/test_lint_computed_claims.py::TestEstimatorLedger``); only
the ``_git`` helpers and ``main`` touch git, and ``main`` is covered by a real fixture repository.

**Stdlib only.** The CI job installs nothing, so this module and everything it imports must load
without ``pydantic`` or ``coder_eval`` — which is why the ``WATCHED_CONSTANTS`` parity assertion
(every ``(module, name)`` pair still resolves) lives in the pytest class rather than here. Without
it a rename would make the diff scan match nothing and the job pass **silently**, which is the
single most important thing that can go wrong with this rule. The same reasoning applies to
``SNAPSHOT_DIRS``: ``git diff --name-only`` reports only a rename's POST-image path, so a moved
fixture directory would disable that half forever with every unit test still green.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from tests.lint.markdown_tables import parse_markdown_tables, table_signature


# (repo-relative module path, constant name). Not bare names: `reports_stats.py` is not the only
# home. Every one of `optimize/gate.py`'s four gate constants steps a rendered gate number exactly
# the way `BOOTSTRAP_RESAMPLES` stepped a rendered CI.
WATCHED_CONSTANTS: tuple[tuple[str, str], ...] = (
    ("src/coder_eval/reports_stats.py", "BOOTSTRAP_RESAMPLES"),
    ("src/coder_eval/reports_stats.py", "DEFAULT_ALPHA"),
    ("src/coder_eval/optimize/gate.py", "MATERIALITY_FLOOR"),
    ("src/coder_eval/optimize/gate.py", "GATE_P_PRECISION"),
    ("src/coder_eval/optimize/gate.py", "GATE_MAX_FAMILY"),
    ("src/coder_eval/optimize/gate.py", "GATE_RESAMPLES"),
    # These two are not resample counts, but they move rendered output just as directly:
    # FLOOR_RESOLUTION decides whether an MDE counts as measurable at all (and therefore whether
    # the execution gate REFUSES), and NEAR_FLOOR_MULTIPLE gates the "p is at or near this
    # bootstrap's resolution floor" note.
    ("src/coder_eval/optimize/gate.py", "FLOOR_RESOLUTION"),
    ("src/coder_eval/optimize/activation.py", "NEAR_FLOOR_MULTIPLE"),
)

# BOTH halves of the pinned-render tree. `report_snapshots/` alone would watch the reports and
# miss the gates — and `optimize_renders/` is where a `bootstrap_p_floor` FORM change surfaces,
# which is the blind spot this module's boundary names.
SNAPSHOT_DIRS: tuple[str, ...] = (
    "tests/_fixtures/report_snapshots/",
    "tests/_fixtures/optimize_renders/",
    "tests/_fixtures/optimize_verdicts/",
)
SNAPSHOT_SUFFIXES: tuple[str, ...] = (".md", ".json")

LEDGER_DOC = "docs/REPORT_SCHEMA.md"
LEDGER_HEADING = "## Estimator changes"
LEDGER_SIGNATURE = "Date | Change | Constant / fixture | Observed step | PR / commit"

_REMEDY = (
    f"Add a row to `{LEDGER_HEADING}` in {LEDGER_DOC} recording the step: date, what changed, the "
    "constant or fixture, the observed before -> after, and the PR. If this PR moves a fixture "
    "with no estimator change behind it, add a row saying the step was zero and why — an edited row "
    "does not raise the count, so a pure correction needs one too."
)


def _assignment_pattern(name: str) -> re.Pattern[str]:
    """A changed diff line that ASSIGNS ``name`` — not one that merely mentions it."""
    return re.compile(rf"^[+-]\s*{re.escape(name)}\s*[:=]", re.MULTILINE)


def is_watched_snapshot(path: str) -> bool:
    """A pinned RENDER, as opposed to the helper modules that live beside them."""
    return path.startswith(SNAPSHOT_DIRS) and path.endswith(SNAPSHOT_SUFFIXES)


def estimator_rows(markdown: str) -> int:
    """The number of data rows in the ledger table; 0 when there is no such table.

    Located by HEADER SIGNATURE rather than by position, so a second table added inside the
    section cannot silently retarget the count.
    """
    if LEDGER_HEADING not in markdown:
        return 0
    section = markdown.split(LEDGER_HEADING, 1)[1].split("\n## ", 1)[0]
    for table in parse_markdown_tables(section):
        if table_signature(table) == LEDGER_SIGNATURE:
            return len(table.rows)
    return 0


def reasons_ledger_is_required(changed: list[str], diffs: dict[str, str]) -> list[str]:
    """Why this diff needs a ledger row, if it does. Pure: ``diffs`` is a path -> diff-text map."""
    reasons: list[str] = []
    changed_set = set(changed)

    for path in sorted(p for p in changed_set if is_watched_snapshot(p)):
        reasons.append(
            f"{path} is a pinned rendered-number fixture and this PR modifies it — a fixture's "
            "numbers moving is exactly the event a consumer recomputing statistics needs recorded"
        )
    for module, name in WATCHED_CONSTANTS:
        if module in changed_set and _assignment_pattern(name).search(diffs.get(module, "")):
            reasons.append(
                f"{module} changes the assignment of {name}, a watched statistical constant — "
                "every rendered figure derived from it steps for identical data"
            )
    return reasons


def check(changed: list[str], diffs: dict[str, str], base_doc: str, head_doc: str) -> list[str]:
    """The reasons a ledger row is required, unless the estimator table gained one."""
    reasons = reasons_ledger_is_required(changed, diffs)
    if not reasons:
        return []
    if estimator_rows(head_doc) > estimator_rows(base_doc):
        return []
    return [*reasons, _REMEDY]


def _git(*args: str) -> str:
    """Run git, surfacing stderr on failure — a bare exit code in a CI log explains nothing."""
    result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def main(base: str) -> int:
    """Compare ``base`` against HEAD and report; the only part of this module that drives git.

    Both sides of the row-count comparison are read at the **merge base**, never at the base
    branch's tip: a base that advanced after this PR branched would otherwise be credited with a
    row this PR did not add, failing the author for a row they already wrote.
    """
    merge_base = _git("merge-base", base, "HEAD").strip()
    # `--diff-filter=M` on the fixture side: a brand-new fixture has no "before" and so no step.
    modified = {line for line in _git("diff", "--name-only", "--diff-filter=M", f"{merge_base}..HEAD").splitlines()}
    all_changed = [line for line in _git("diff", "--name-only", f"{merge_base}..HEAD").splitlines() if line]
    changed = [p for p in all_changed if not is_watched_snapshot(p) or p in modified]

    watched_modules = {module for module, _name in WATCHED_CONSTANTS}
    diffs = {
        module: _git("diff", "-U0", f"{merge_base}..HEAD", "--", module)
        for module in sorted(watched_modules & set(changed))
    }
    reasons = reasons_ledger_is_required(changed, diffs)
    if not reasons:
        print(f"estimator ledger: {len(changed)} changed path(s), none of them watched.")
        return 0

    base_doc = _git("show", f"{merge_base}:{LEDGER_DOC}")
    head_doc = Path(LEDGER_DOC).read_text(encoding="utf-8")
    failures = check(changed, diffs, base_doc, head_doc)
    if not failures:
        print(f"estimator ledger: {len(reasons)} watched change(s), and the ledger gained a row.")
        return 0
    for failure in failures:
        print(failure)
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: python -m {__spec__.name if __spec__ else 'tests.lint.estimator_ledger'} <base-ref>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
