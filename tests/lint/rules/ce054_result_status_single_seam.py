"""CE054: a criterion module compares ``result_status`` in exactly ONE place.

**The bug it is written for.** ``criteria/skill_triggered.py`` decided "did this command
deliver the skill body?" twice, 29 lines apart and inside the same function — an ALLOWLIST on
the ``Skill`` branch (``result_status != "success"``) and a DENYLIST on the file-read branch
(``result_status in ("error", None)``). They agreed on ordinary input and diverged exactly
where it mattered: a turn crashing before a ``Read(SKILL.md)`` result arrives force-closes the
call to ``"unknown"``, which the denylist let through as engagement while the identical crash
on a ``Skill`` call was correctly excluded. That is a false positive on ``f1.yes``, the metric
``optimize_activation.activation_gate`` promotes on. The fix was to write the predicate once, as
``_delivered``, and call it from both branches.

**It counts comparison SITES, not enclosing functions, and that distinction is the whole rule.**
Both halves of the bug lived in one function, so a per-function seam check — the obvious
spelling, and the one this rule was first written as — reports ZERO on the exact file it exists
for. Measured on the pre-fix source before this docstring was written.

**Why not CE018's shape one field over.** Forbidding a membership DENYLIST on ``result_status``
was considered and deliberately not built. ``result_status`` is a closed
``Literal["success", "error", "unknown"] | None``, so CE018's real failure mode (a new enum
member silently falling through a stale denylist) needs a model edit a reviewer sees. And it
would have caught only ONE of the two sites above — the allowlist half is not a denylist, and
it is the DRIFT BETWEEN the two, not the shape of either, that changed the score. The denylist
mirror is recorded in ``.claude/harness-candidates.md`` rather than built.

This is the CE037 / CE040 / CE042 family: one declaration of a rule, enforced structurally, so
a second copy cannot silently disagree with the first.

**What it detects, precisely.** Under ``src/coder_eval/criteria/``, every ``Compare`` node with
an ``<expr>.result_status`` operand. The second and each subsequent one in a module is a
violation, reported at its own line and naming the first.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.**

- Scoped to ``criteria/``. Elsewhere in ``src/`` a status is legitimately read in several
  places for unrelated reasons — ``analysis.py`` buckets counts for a report, ``reports_html``
  picks a CSS class, the agents SET it — and none of those decides a score. A criterion module
  is where a status becomes a NUMBER.
- It counts sites, not truth. Two callers can still disagree by routing through a shared helper
  that returns the wrong thing; what this forbids is the specific shape where the predicate is
  spelled twice and the two spellings drift.
- A non-comparison read (``f(cmd.result_status)``, a dict lookup, an f-string, a ``match``
  subject) is not matched. The rule is about classification, and a value passed on is not
  classified here.
- One legitimate site may still need a second: ``# noqa: CE054`` is the escape hatch, and it is
  the rule working — it forces the author to answer *why does this module decide the same thing
  twice, and are the two answers the same?*
"""

import ast
import re

from tests.lint.rules.base import BaseRule


# Matched against the forward-slash-normalized path (the convention CE009 sets), so a
# backslash alternative here would be dead: `filepath.replace` runs first.
_CRITERIA_PACKAGE = re.compile(r"/coder_eval/criteria/")

_ATTR = "result_status"


def _is_result_status(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == _ATTR


class ResultStatusSingleSeam(BaseRule):
    id = "CE054"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_criteria = bool(_CRITERIA_PACKAGE.search(filepath.replace("\\", "/")))
        self._first_line: int | None = None

    def visit_Compare(self, node: ast.Compare) -> None:
        if self._in_criteria and any(_is_result_status(n) for n in (node.left, *node.comparators)):
            self._record(node)
        self.generic_visit(node)

    def _record(self, node: ast.Compare) -> None:
        if self._first_line is None:
            self._first_line = node.lineno
            return
        self.violation(
            node,
            f"{_ATTR} is compared in more than one place in this module (first at line "
            f"{self._first_line}) — a criterion's delivered/failed predicate must be written "
            "ONCE and called from each branch, or the two spellings drift and the score "
            "changes for identical agent output",
        )
