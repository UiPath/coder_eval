"""CE053: no bare run-record filename literal outside ``path_utils``.

``path_utils`` defines ``TASK_JSON_FILENAME`` / ``PRE_GRADE_JSON_FILENAME`` and
its comment states why: "~12 sites name them — including three that ``rglob`` for
the first — and two half-copies of the same string in different packages is how a
rename becomes a silent no-op on the sites it missed."

The constant shipped with that rationale and the twelve pre-existing literals
were not converted, so it created exactly the second source of truth it argues
against and delivered zero rename safety: the new modules used the constant, and
``orchestrator.py``, ``batch.py``, ``docker_runner.py``, ``reports.py``,
``reports_junit.py``, ``reports_stats.py`` and ``report_command.py`` kept the
string — the three ``rglob("task.json")`` calls the comment specifically cites
among them.

A rationale that only a human remembers is not a rule. Fires on any string
constant in ``src/coder_eval/`` (outside ``path_utils.py``) that equals one of
those filenames, or embeds it as a trailing path segment (``"*/task.json"``).
Import the constant instead; ``# noqa: CE053`` for a genuinely unrelated string.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


def _run_record_filenames() -> set[str]:
    """The filenames from ``path_utils``, read from the module rather than retyped."""
    from coder_eval.path_utils import PRE_GRADE_JSON_FILENAME, TASK_JSON_FILENAME

    return {TASK_JSON_FILENAME, PRE_GRADE_JSON_FILENAME}


class NoRunRecordFilenameLiteral(BaseRule):
    id = "CE053"

    # `(^|sep)` so a repo-relative path is in scope too; see CE047.
    _SRC_PATH = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]")
    # The module that DEFINES them, and the container-path module that mirrors
    # the in-container layout as its own vocabulary.
    _EXEMPT = re.compile(r"[/\\]path_utils\.py$")
    _names: set[str] | None = None

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(self._SRC_PATH.search(filepath)) and not self._EXEMPT.search(filepath)
        if self._in_scope and NoRunRecordFilenameLiteral._names is None:
            NoRunRecordFilenameLiteral._names = _run_record_filenames()

    def visit_Constant(self, node: ast.Constant) -> None:
        if self._in_scope and isinstance(node.value, str):
            self._check(node, node.value)
        self.generic_visit(node)

    def _check(self, node: ast.Constant, value: str) -> None:
        for name in NoRunRecordFilenameLiteral._names or set():
            # Exact, or a glob/path whose LAST segment is the filename. Not a
            # bare `in`: that would fire on prose in a docstring or an error
            # message, where naming the file is the point.
            if value == name or (("/" in value) and value.rsplit("/", 1)[-1] == name):
                self.violation(
                    node,
                    f"{value!r} names a run record by literal. path_utils exports a constant for it "
                    "precisely so a rename cannot leave half the tree behind — and the constant "
                    "shipped while twelve sites kept the string, which is the second source of "
                    "truth it was added to prevent. Import TASK_JSON_FILENAME / "
                    "PRE_GRADE_JSON_FILENAME, or add `# noqa: CE053` if the string is unrelated.",
                )
                return
