"""CE052: a process-lethal call must be gated on actually being in the container.

Fires on ``os._exit(...)`` (any module alias) anywhere in ``src/coder_eval/`` that is
not lexically inside the body of an ``if``/``elif`` whose test mentions
``CODER_EVAL_IN_CONTAINER`` or ``IN_CONTAINER_ENV``. An ``else`` arm is not gated.
``os._exit`` skips ``atexit``, ``finally`` and every handler, so it is correct only for
reaping the container's own disposable main process.

HAZARD: do not gate on ``sandbox.driver``. ``DockerRunner._stage_inputs`` stages the
in-container task with ``driver: tempdir``, so a driver gate disables itself on the one
path that needs it.

The check is lexical, not a data-flow proof: it forces the guard to be written at the
site. Add ``# noqa: CE052`` with a reason for an intentional exception.

Rationale: .claude/notes/lint-rules.md § CE052
"""

import ast
import re

from tests.lint.rules.base import BaseRule


# Both spellings of the same gate. The env var NAME is the canonical one, but it
# is now reached through `models.container_paths.IN_CONTAINER_ENV` so the string
# has a single definition — and a rule that recognised only the literal would
# read the constant-based gate as NO gate at all, then instruct the author to
# paste the literal back. That is the rule arguing against the SSOT it should be
# reinforcing, so it accepts the constant's name too.
_GATES = ("CODER_EVAL_IN_CONTAINER", "IN_CONTAINER_ENV")

_MESSAGE = (
    "`os._exit` here is not gated on CODER_EVAL_IN_CONTAINER. It kills the process outright — "
    "no atexit, no finally, no traceback — which is the right primitive ONLY for the container's "
    "own main process. Anywhere else it destroys a host process that merely called this code: an "
    "unconditionally-armed watchdog once exited a pytest worker 40s after the test that armed it, "
    "reporting as a random crash in an unrelated file and as a bogus coverage failure. Gate it on "
    '`os.environ.get(IN_CONTAINER_ENV) == "1"` (from coder_eval.models), or add `# noqa: CE052` '
    "with a reason."
)


def _is_os_exit(node: ast.Call) -> bool:
    """``os._exit(...)`` under any module alias (it is imported as ``_os`` here)."""
    return isinstance(node.func, ast.Attribute) and node.func.attr == "_exit"


class ProcessLethalMustBeContainerGated(BaseRule):
    id = "CE052"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        # `(^|sep)` so a repo-relative path is in scope too; see CE054.
        self._in_scope = bool(re.search(r"(?:^|[/\\])src[/\\]coder_eval[/\\]", filepath))
        # Tests of enclosing `if`/`elif` statements, innermost last.
        self._guards: list[ast.expr] = []

    def visit_If(self, node: ast.If) -> None:
        # Only the body is guarded — the `else` arm is the ungated branch, which
        # is exactly where an inverted guard would put the lethal call.
        self._guards.append(node.test)
        for child in node.body:
            self.visit(child)
        self._guards.pop()
        for child in node.orelse:
            self.visit(child)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_scope and _is_os_exit(node) and not self._container_gated():
            self.violation(node, _MESSAGE)
        self.generic_visit(node)

    def _container_gated(self) -> bool:
        return any(gate in ast.dump(test) for test in self._guards for gate in _GATES)
