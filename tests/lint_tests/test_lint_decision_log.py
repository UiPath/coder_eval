"""The decision log's two mechanical invariants, and the prose ratchet that keeps it used.

`.claude/decisions/README.md` states the three registers — contract in the docstring,
why-not-the-alternative at the decision site, defect history in a dated file — and says outright
that "is this sentence a contract or a defect history" is a judgement no rule can make. What IS
checkable is mechanical, and it is what this file checks:

* **No orphaned decision file.** A decision nobody can reach from the code is one nobody will read,
  and it will be re-litigated. Every file must be referenced from `src/`.
* **The ratchet does not slip.** Counted, in one place, so the next 40-line essay is a visible
  decision rather than a drift. Deliberately a CEILING and not an equality: trimming further is
  always allowed, and a docstring that resists trimming because every line is contract should stay —
  the README says so.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.lint_tests.shared import REPO_ROOT


pytestmark = pytest.mark.lint

DECISIONS = REPO_ROOT / ".claude" / "decisions"
SRC = REPO_ROOT / "src"

# The surfaces the register applies to: the optimize decision family, its presentation half, and the
# models that carry the verdicts. Measured before Phase 9 as 25 over-long docstrings and 9 over-long
# field descriptions.
PROSE_SURFACES = [*sorted((SRC / "coder_eval" / "optimize").glob("*.py")), SRC / "coder_eval" / "reports_optimize.py"]
FIELD_SURFACE = SRC / "coder_eval" / "models" / "optimize.py"

DOCSTRING_LINE_LIMIT = 25
MAX_LONG_DOCSTRINGS = 8

FIELD_DESCRIPTION_LINE_LIMIT = 8
MAX_LONG_FIELD_DESCRIPTIONS = 4


def _long_docstrings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # `clean=False`: the ratchet counts the lines an author actually reads in the file, and
        # `cleandoc` would re-wrap them.
        doc = ast.get_docstring(node, clean=False)
        if doc and len(doc.splitlines()) > DOCSTRING_LINE_LIMIT:
            found.append((len(doc.splitlines()), f"{path.name}::{getattr(node, 'name', '<module>')}"))
    return found


def _long_field_descriptions(path: Path) -> list[tuple[int, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_field = (isinstance(func, ast.Name) and func.id == "Field") or (
            isinstance(func, ast.Attribute) and func.attr == "Field"
        )
        if not is_field:
            continue
        for keyword in node.keywords:
            if keyword.arg != "description":
                continue
            span = (keyword.value.end_lineno or keyword.value.lineno) - keyword.value.lineno + 1
            if span > FIELD_DESCRIPTION_LINE_LIMIT:
                found.append((span, node.lineno))
    return found


def test_the_surfaces_this_ratchet_measures_exist() -> None:
    """Anti-vacuity. A moved file would make every count below zero, which reads as success."""
    assert len(PROSE_SURFACES) >= 7, f"only {len(PROSE_SURFACES)} prose surfaces found — has optimize/ moved?"
    assert all(p.is_file() for p in PROSE_SURFACES), [str(p) for p in PROSE_SURFACES if not p.is_file()]
    assert FIELD_SURFACE.is_file()
    # And they really do hold docstrings, so a parse that silently returned nothing is caught.
    assert any(ast.get_docstring(ast.parse(p.read_text(encoding="utf-8"))) for p in PROSE_SURFACES)


def test_the_decision_log_states_its_convention() -> None:
    readme = (DECISIONS / "README.md").read_text(encoding="utf-8")
    for register in ("Contract", "Defect history", "harness-candidates"):
        assert register in readme, f"the README no longer states the {register!r} register"


def test_no_decision_file_is_orphaned() -> None:
    """Every decision must be reachable from the code it explains.

    The one part of the convention a rule CAN check, and the part that decides whether the log is
    read at all: a reader arrives at history from a pointer in a docstring, never by browsing
    `.claude/`. An unreferenced file is a decision that will be made again.
    """
    files = sorted(p for p in DECISIONS.glob("*.md") if p.name != "README.md")
    assert files, "GAP: no decision files — this test would pass vacuously"

    src_text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    orphans = [p.name for p in files if p.name not in src_text]
    assert not orphans, (
        "these decision files are referenced from nowhere in src/:\n  "
        + "\n  ".join(orphans)
        + "\nAdd a `# See .claude/decisions/<slug>.md` pointer at the code they explain, or delete them."
    )


def test_every_pointer_resolves_to_a_real_decision_file() -> None:
    """The other direction: a pointer to a file that does not exist is worse than none."""
    pointer = re.compile(r"\.claude/decisions/([\w.-]+\.md)")
    referenced = {m for p in SRC.rglob("*.py") for m in pointer.findall(p.read_text(encoding="utf-8"))}
    assert referenced, "GAP: src/ contains no decision pointers at all"
    missing = sorted(name for name in referenced if not (DECISIONS / name).is_file())
    assert not missing, f"src/ points at decision files that do not exist: {missing}"


def test_over_long_docstrings_do_not_grow() -> None:
    long = sorted((n for path in PROSE_SURFACES for n in _long_docstrings(path)), reverse=True)
    assert len(long) <= MAX_LONG_DOCSTRINGS, (
        f"{len(long)} docstrings in the optimize family exceed {DOCSTRING_LINE_LIMIT} lines, above "
        f"the agreed {MAX_LONG_DOCSTRINGS} (was 25 before the registers were split). Move the defect "
        "history to .claude/decisions/ and leave a one-line pointer — or, if every line really is "
        "contract, say so and raise this number deliberately:\n  "
        + "\n  ".join(f"{n} lines  {name}" for n, name in long)
    )


def test_over_long_field_descriptions_do_not_grow() -> None:
    long = _long_field_descriptions(FIELD_SURFACE)
    assert len(long) <= MAX_LONG_FIELD_DESCRIPTIONS, (
        f"{len(long)} `Field(description=…)` blocks in {FIELD_SURFACE.name} exceed "
        f"{FIELD_DESCRIPTION_LINE_LIMIT} lines, above the agreed {MAX_LONG_FIELD_DESCRIPTIONS} "
        "(was 9). A field description is USER-VISIBLE in the JSON schema, so trim it to the "
        f"contract rather than deleting it:\n  {long}"
    )
