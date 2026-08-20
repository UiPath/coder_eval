"""Two classes of pointer that go stale silently when a test moves.

Both were live after the lint monolith was split into `tests/lint_tests/`. Neither had a sensor,
while the decision-log pointers next door are checked in BOTH directions — the asymmetry is what
made these worth closing rather than deferring.

* **`<path>::<Name>` wiring references.** Fifteen of them named `tests/test_custom_lint.py` as the
  home of a class that had moved. Each is the "where is this rule wired" pointer a reader follows
  from a detection body to the test that runs it, so a wrong one sends them to a 95-line file
  holding three runner-level tests and none of the subject.
* **`Path(__file__).parent.parent` inside a SUBDIRECTORY of `tests/`.** Correct for a file directly
  in `tests/` (it reaches the repo root) and one directory short for anything nested. About 25 path
  constants moved into `tests/lint_tests/` carrying it, and the failure was not a red import: it was
  178 anti-vacuity assertions firing at once, each reporting that the tree it scans had vanished.

Neither is expressible as a `BaseRule`: the first needs the whole `tests/` tree to resolve a name,
the second needs the file's DEPTH rather than its AST.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.lint_tests.shared import REPO_ROOT, TESTS_ROOT


pytestmark = pytest.mark.lint

# Where a `tests/...::Name` pointer may appear. Detection bodies and the docs a contributor reads.
_POINTER_SURFACES = (
    *sorted((TESTS_ROOT / "lint").rglob("*.py")),
    *sorted((REPO_ROOT / "docs").rglob("*.md")),
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / ".claude" / "harness-candidates.md",
)
_POINTER = re.compile(r"(tests/[\w/]+\.py)::(\w+)")

# `Path(__file__).parent.parent` — the repo-root reach.
_ROOT_REACH = "Path(__file__).parent.parent"


def _declared_in() -> dict[str, set[str]]:
    """Every top-level class and function name under `tests/`, mapped to the files declaring it."""
    where: dict[str, set[str]] = {}
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                where.setdefault(node.name, set()).add(str(path.relative_to(REPO_ROOT)))
    return where


def test_the_surfaces_and_the_name_index_are_non_empty() -> None:
    """Anti-vacuity: an empty index would clear both checks below."""
    assert len(_POINTER_SURFACES) > 20, f"only {len(_POINTER_SURFACES)} pointer surfaces found"
    declared = _declared_in()
    assert len(declared) > 300, f"only {len(declared)} names indexed under tests/ — has the tree moved?"
    # And the pattern really matches the tree's live pointers.
    found = sum(len(_POINTER.findall(p.read_text(encoding="utf-8"))) for p in _POINTER_SURFACES if p.is_file())
    assert found > 5, f"only {found} `path::Name` pointers found — the pattern no longer matches"


def test_every_wiring_pointer_names_the_file_that_declares_it() -> None:
    declared = _declared_in()
    stale = [
        f"{path.relative_to(REPO_ROOT)}: `{target}::{name}` — really in {sorted(declared[name])}"
        for path in _POINTER_SURFACES
        if path.is_file()
        for target, name in _POINTER.findall(path.read_text(encoding="utf-8"))
        # A name the tree does not declare is out of scope: it may be an illustrative example.
        if name in declared and target not in declared[name]
    ]
    assert not stale, (
        "these `<path>::<Name>` pointers name a file that does not declare the name:\n  "
        + "\n  ".join(stale)
        + "\nA reader follows these to find the test that runs a rule."
    )


def test_no_nested_test_module_reaches_the_repo_root_through_parent_parent() -> None:
    """Depth-aware, which is the whole point: the same expression is right one level up.

    For `tests/x.py`, `Path(__file__).parent.parent` IS the repo root. For `tests/lint_tests/x.py` it
    is `tests/`, and every path built from it silently points into the wrong tree. Import a declared
    root instead — `tests/lint_tests/shared.py` exports `REPO_ROOT` and `TESTS_ROOT`.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        # Directly in `tests/` the expression is correct; only nesting breaks it. This file is
        # exempt because it must name the pattern to forbid it — the same carve-out CE050 makes for
        # its own machinery, and it is why the scan skips comment lines too.
        if path.parent != TESTS_ROOT and path.name != Path(__file__).name
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _ROOT_REACH in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"these files are nested under tests/ and reach for the repo root with "
        f"`{_ROOT_REACH}`, which lands on `tests/` instead:\n  " + "\n  ".join(offenders)
    )
