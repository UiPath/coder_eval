"""CE045 — a test may not skip itself on a path this repository does not track.

``tests/test_optimize_measurements.py`` opened with

.. code-block:: python

    history = REPO_ROOT / ".optimize-skill" / "ci" / "history.json"
    if not history.exists():
        pytest.skip("the worked ci ledger is not present in this checkout")

and ``.optimize-skill/`` is in ``.gitignore``. The file exists in the author's working tree and in
**no** clone, so the test passed locally and skipped in CI — every time, silently. What it was
taking with it was not the ledger assertions it was written for but a whole-package AST scan
proving no code path names ``history.json``: a load-bearing guard that had not run in CI since the
day it landed, reported as a skip nobody reads.

**What it detects, precisely.** For every ``pytest.skip(...)`` call, the innermost enclosing ``if``
is located and the filesystem path its condition tests is reconstructed. When the first segment of
that path is a plain-literal ``.gitignore`` entry, the guard is reported.

Reconstructing the path is the whole difficulty, and the reason the naive version of this rule is
blind to its own only subject: the guard above is ``not history.exists()`` and carries **zero**
string constants. A scanner reading only the ``if`` test finds nothing, the real-tree assertion
passes vacuously, and no suppression is ever needed — a sensor that looks green because it cannot
see. So a ``Name`` in the condition is resolved back through a *direct local assignment* of a
``/``-chain earlier in the same function.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof:**

- ``.gitignore`` semantics are NOT reimplemented. ``gitignored_prefixes`` reads plain top-level
  literal entries only — no ``*``, ``?``, ``[``, ``!`` and no nested path — which covers
  ``.optimize-skill/``, the entry that produced the defect. A skip guarded on a globbed or negated
  ignore pattern is not matched.
- Only a **direct local assignment** of a ``/``-chain is followed. A path built in a helper, a
  fixture, a comprehension or a class attribute is invisible.
- A ``pytest.skip`` with no enclosing ``if`` (module level, or inside ``try/except ImportError``)
  is skipped rather than guessed at.
- The path is reconstructed from its STRING segments only, so a ``/``-chain rooted at a variable
  reads as starting at its first literal: ``tmp_path / "runs" / "run.json"`` looks like the
  gitignored top-level ``runs/``. There is no such guard today, and the rule errs toward noise
  rather than silence, but a future one would need a ``# noqa: CE045`` for the wrong reason.
- Any ``<expr>.skip(...)`` attribute call counts, not only ``pytest.skip`` — deliberately broad,
  since aliasing the import is the obvious evasion, but it means a ``self.skip(...)`` would be
  reported in a message that says pytest.
- Suppression runs through the shared ``runner._is_suppressed``, which honours a **bare**
  ``# noqa`` as well as ``# noqa: CE045``. A bare marker added for ruff on any line the ``if``
  spans silently suppresses this rule too.
- It pins the SHAPE of the guard, never what the skip hides. A skip on a tracked path that is
  nonetheless absent in CI is outside it.

The intended fix is to move whatever is load-bearing out from behind the skip so it runs
unconditionally, and to leave ``# noqa: CE045`` plus a comment on the guard that legitimately
remains. That suppression is the rule working: it forces the author to answer *"is anything
load-bearing behind this skip?"* every time one is written.

Wired as ``tests/test_custom_lint.py::TestCE045SkipOnIgnoredPath`` rather than as a ``BaseRule``,
because its subject is ``tests/`` and the ``ALL_RULES`` sweep runs over ``src/`` only.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.lint.runner import _is_suppressed
from tests.lint.violation import Violation


RULE_ID = "CE045"

_GLOB_CHARS = "*?[!"


def gitignored_prefixes(gitignore: Path) -> set[str]:
    """Plain-literal top-level entries only. Globs, negations and nested paths are out of scope."""
    out: set[str] = set()
    for line in gitignore.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or any(c in entry for c in _GLOB_CHARS):
            continue
        entry = entry.strip("/")
        if entry and "/" not in entry:
            out.add(entry)
    return out


def _segments(node: ast.AST) -> list[str]:
    """String constants of an ``A / "b" / "c"`` chain, in order."""
    segs: list[str] = []

    def walk(n: ast.AST) -> None:
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            segs.append(n.value)

    walk(node)
    return segs


def _first_chain(node: ast.AST) -> list[str]:
    """Segments of the outermost ``/``-chain anywhere under ``node``, or ``[]``.

    ``ast.walk`` is breadth-first, so the first ``BinOp(Div)`` it yields is the outermost one and
    its segments are the whole path rather than a tail of it.
    """
    for candidate in ast.walk(node):
        if isinstance(candidate, ast.BinOp) and isinstance(candidate.op, ast.Div):
            return _segments(candidate)
    return []


def _skip_calls(node: ast.AST) -> list[ast.Call]:
    """Any ``<expr>.skip(...)`` under ``node``. Broader than ``pytest.skip`` on purpose — aliasing
    the import is the obvious evasion — which the module docstring's boundary states."""
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "skip"
    ]


def _innermost_ifs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.If]:
    """Each ``if`` that directly guards a ``pytest.skip`` — innermost wins, in source order."""
    guards: dict[int, ast.If] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        for call in _skip_calls(node):
            previous = guards.get(id(call))
            if previous is None or node.lineno > previous.lineno:
                guards[id(call)] = node
    return sorted({id(g): g for g in guards.values()}.values(), key=lambda g: g.lineno)


def _guarded_path(fn: ast.FunctionDef | ast.AsyncFunctionDef, guard: ast.If) -> list[str]:
    """The path segments the guard's condition tests, following one level of local assignment."""
    if segments := _first_chain(guard.test):
        return segments
    assigns: dict[str, list[str]] = {}
    for stmt in ast.walk(fn):
        if not (isinstance(stmt, ast.Assign) and stmt.lineno < guard.lineno):
            continue
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue
        if segments := _first_chain(stmt.value):
            assigns[stmt.targets[0].id] = segments
    for node in ast.walk(guard.test):
        if isinstance(node, ast.Name) and node.id in assigns:
            return assigns[node.id]
    return []


def _matched_prefix(segments: list[str], prefixes: set[str]) -> str | None:
    """The ignored prefix this path sits under, matched on the SEGMENT and never on a substring."""
    if not segments:
        return None
    head = segments[0]
    for prefix in prefixes:
        if head == prefix or head.startswith(f"{prefix}/"):
            return prefix
    return None


def find_ignored_skip_guards(paths: list[Path], prefixes: set[str]) -> list[str]:
    """Every unsuppressed ``pytest.skip`` guarded on a gitignored path, as readable strings."""
    hits: list[str] = []
    for path in sorted(paths):
        source = path.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for guard in _innermost_ifs(fn):
                prefix = _matched_prefix(_guarded_path(fn, guard), prefixes)
                if prefix is None:
                    continue
                violation = Violation(
                    rule_id=RULE_ID,
                    file=str(path),
                    line=guard.lineno,
                    col=guard.col_offset,
                    message="",
                    end_line=guard.end_lineno or guard.lineno,
                )
                if _is_suppressed(source_lines, violation):
                    continue
                hits.append(
                    f"{path.name}:{guard.lineno} {fn.name} skips itself when {prefix!r} is absent, "
                    "and .gitignore means it is absent in every clone — so everything below this "
                    "guard has never run in CI. Move anything load-bearing out from behind it, "
                    f"then mark the remainder `# noqa: {RULE_ID}` with a comment saying what is left."
                )
    return hits
