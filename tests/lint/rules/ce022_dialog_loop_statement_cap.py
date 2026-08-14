"""CE022: functions carrying ``# noqa: PLR0915`` must stay under a measured cap.

``# noqa: PLR0915`` *disables ruff's statement check entirely* for that
function — without a guard it could silently regrow past the size that earned
the suppression and nothing would fail. This rule re-imposes a bound on every
function in ``src/`` known to carry the suppression: it counts each target's
statements (every statement node in the body, recursively) and fires if the
count exceeds its registered cap. Each cap is the measured size at the time it
was registered, plus a small headroom, so ordinary edits don't trip it but a
real regrowth does.

This count is CE022's OWN metric, deliberately not identical to ruff's PLR0915
count (ruff's is higher — it counts some constructs this walk does not), so a
cap below ruff's 80 ceiling does NOT mean the function would now pass ruff and
could drop its suppression. The ruff-equivalent counts are recorded beside each
cap below; re-measure with `ruff check --select PLR0915 --ignore-noqa` before
concluding a suppression is removable.

Registered targets (file, function, cap):

- ``orchestrator.py::_simulation_dialog_loop`` (cap 128; 122 CE022-stmts ≙ 124
  ruff-stmts) — sequential dialog driver, irreducible without a state-object
  rewrite (2026-06-23 decompose-god-functions plan, Phase 5).
- ``antigravity_agent.py::communicate`` (cap 81; 77 CE022-stmts ≙ 82
  ruff-stmts) — the poll-loop-plus-finalize driver; the
  ``step_fetch_timed_out`` post-loop branch (2026-08-14) pushed it over
  ruff's ceiling.

Adding a new ``# noqa: PLR0915`` anywhere in ``src/`` means adding its
``(file, function, cap)`` to ``_TARGETS`` below too. That contract is
**self-enforcing**: an unregistered function carrying the suppression is itself
a CE022 violation (see ``_carries_suppression``), because otherwise its size
would be bounded by nothing — ruff's check suppressed, this rule skipping it —
and ``test_no_violations`` would still report the tree clean.

If a future decomposition legitimately brings a target under ruff's ceiling,
remove its ``# noqa: PLR0915`` AND its entry in ``_TARGETS`` together.
"""

import ast
import re
from pathlib import Path

from tests.lint.rules.base import BaseRule


# Mirrors runner._NOQA_CODES so CE022 reads a suppression exactly the way the
# runner does; kept as its own copy to avoid a rules -> runner import cycle.
_NOQA_CODES = re.compile(r"#\s*noqa:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)")


def _count_statements(func: ast.AsyncFunctionDef | ast.FunctionDef) -> int:
    """Statement nodes in the function body, counted recursively (nested
    compound-statement bodies included). Close to, but not identical with,
    ruff's PLR0915 count — see the module docstring."""
    return sum(1 for stmt in func.body for node in ast.walk(stmt) if isinstance(node, ast.stmt))


class NoqaPlr0915StatementCap(BaseRule):
    id = "CE022"

    # (basename, function name, cap). Exact-basename match (not endswith) so a
    # sibling like ``x_orchestrator.py`` can't accidentally match.
    _TARGETS: tuple[tuple[str, str, int], ...] = (
        ("orchestrator.py", "_simulation_dialog_loop", 128),
        ("antigravity_agent.py", "communicate", 81),
    )

    def _carries_suppression(self, node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
        """True if this function's ``def`` header carries a PLR0915 suppression.

        Comments are absent from the AST, so this reads physical source lines —
        but ONLY ``self.source_lines``, never the file at ``self.filepath``.
        Re-reading the path would scan an unrelated file whenever the tree came
        from somewhere else (the rule's own unit tests pass synthetic source
        with a real-looking path), which is a silent false-positive source and
        makes the result depend on the process's cwd. No source ⇒ no check.

        The scanned range is the ``def`` line through the line before the first
        body statement, so a PLR0915 mention inside the body — a docstring, or
        this rule's own message text — cannot register as a suppression.

        KNOWN GAP: a blanket ``# noqa`` (no codes) also suppresses PLR0915 for
        ruff but is deliberately NOT matched here, because ``runner._is_suppressed``
        would drop this rule's own violation on that same line anyway — the
        marker is unreachable for CE022 either way. A file-level
        ``# ruff: noqa: PLR0915`` is likewise out of range. Both are recorded in
        ``.claude/harness-candidates.md``; this guard closes the ordinary
        per-function ``# noqa: PLR0915`` form, which is the one in use.
        """
        if self.source_lines is None:
            return False
        start = node.lineno - 1
        end = max(node.lineno, node.body[0].lineno - 1)
        for line in self.source_lines[start:end]:
            m = _NOQA_CODES.search(line)
            if m and "PLR0915" in {c.strip() for c in m.group(1).split(",")}:
                return True
        return False

    def _check(self, node: ast.AsyncFunctionDef | ast.FunctionDef) -> None:
        # Both sync and async defs are checked so a future def-kind conversion
        # can't silently drop the guard.
        basename = Path(self.filepath).name
        registered = False
        for target_file, target_func, cap in self._TARGETS:
            if basename == target_file and node.name == target_func:
                registered = True
                count = _count_statements(node)
                if count > cap:
                    self.violation(
                        node,
                        f"{target_func} has {count} statements (cap {cap}). It keeps a "
                        f"# noqa: PLR0915, which disables ruff's statement check entirely, so this CE rule "
                        f"bounds its regrowth. Decompose further, or — if the growth is intentional and "
                        f"reviewed — re-measure and bump its cap in _TARGETS.",
                    )
                break
        # Self-guard: a suppression this table doesn't know about is an
        # UNBOUNDED function that test_no_violations would still call clean —
        # the exact false sense of coverage this rule exists to prevent. Without
        # it the "register your new noqa" contract in the module docstring is
        # documentation only, enforced by nothing.
        if not registered and self._carries_suppression(node):
            self.violation(
                node,
                f"{node.name} carries a # noqa: PLR0915 but is not registered in CE022's _TARGETS, so its "
                f"size is bounded by nothing (ruff's check is suppressed and this rule skips it). Add "
                f"({basename!r}, {node.name!r}, <measured statements + 6>) to _TARGETS, or decompose the "
                f"function and drop the suppression.",
            )
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check(node)
