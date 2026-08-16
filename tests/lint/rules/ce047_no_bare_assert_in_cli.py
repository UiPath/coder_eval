"""CE047: no bare ``assert`` under ``src/coder_eval/cli/``.

``python -O`` strips every ``assert`` from the bytecode. An assertion used as an *internal
invariant* survives that fine — the invariant either holds or the program is already broken. An
assertion used as **user-input validation** does not: under ``-O`` the check simply disappears and
the bad input flows on into code written on the assumption it was rejected.

``cli/plan_command.py`` shipped exactly that — a bare ``assert`` as its only argument validation,
removed by a later plan *without* a guard. This is that guard, arriving one plan late.

**What it detects, precisely.** Any ``ast.Assert`` in a file whose normalized path contains
``src/coder_eval/cli/``.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.** Repo-wide there were
**77** ``assert`` statements in ``src/`` when this rule landed, and almost all of them are
legitimate internal invariants
in code a user never reaches through an ``-O`` interpreter. This rule is deliberately scoped to the
one directory where an ``assert`` is most likely to BE input validation, and it does **not**
distinguish the two cases — it cannot, without reading intent. An internal-invariant assertion that
genuinely belongs in ``cli/`` therefore needs a ``# noqa: CE047`` with a reason. The scope is
``src/`` only, because the ``ALL_RULES`` sweep is, so a test fixture whose path contains ``cli/``
is out of reach.

The intended fix: raise ``typer.BadParameter`` (or ``typer.Exit`` with a printed message) for
anything derived from user input, and a real exception — ``ValueError``, ``RuntimeError`` — for an
internal invariant that must survive ``-O``.
"""

import ast

from tests.lint.rules.base import BaseRule


_CLI_PACKAGE = "src/coder_eval/cli/"

_FIX = (
    "a bare `assert` under cli/ is stripped by `python -O`, so an assertion used as argument "
    "validation silently stops validating. Raise typer.BadParameter for user input, or a real "
    "exception for an internal invariant. `# noqa: CE047` with a reason if this really is an "
    "internal invariant that belongs here."
)


class NoBareAssertInCli(BaseRule):
    id = "CE047"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        # Normalize backslashes so the match works on Windows runners, where pathlib hands the
        # rule native-separator strings (the convention CE009's `_SCOPED_PATHS` comment sets).
        self._active = _CLI_PACKAGE in filepath.replace("\\", "/")

    def visit_Assert(self, node: ast.Assert) -> None:
        if self._active:
            self.violation(node, _FIX)
        self.generic_visit(node)
