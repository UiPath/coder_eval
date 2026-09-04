"""CE052: a process-lethal call must be gated on actually being in the container.

``os._exit`` bypasses ``atexit``, buffered IO, ``finally`` blocks and every
exception handler: the process is simply gone. That is the correct primitive for
exactly one thing in this codebase — reaping the container's own disposable main
process when the host that started it has died — and it is safe there only
because that process is *ours to destroy*. In any other process it is not a
degraded outcome, it is an unattributable one.

The motivating bug: ``run_task_internal_command`` armed its host-heartbeat
watchdog — a daemon thread whose whole authority is ``os._exit(137)`` — as an
unconditional side effect of the command body. A test invoked that command
in-process (legitimately: the command must refuse a malformed ``context.json``,
and asserting that means calling it), and the pytest worker inherited the
thread. Forty seconds later — 20s grace plus the 20s stale window — it found no
heartbeat and exited the worker, mid-way through whatever unrelated test file
that worker had since moved on to.

Every property of that failure is the one this rule exists to prevent:

  * it named the wrong test — a different one on each run, on each platform,
    with no traceback, because there is no exception to raise;
  * it was invisible at low load — with 14 local workers the file finished and
    the run ended before the timer fired, so it reproduced only on CI's 2;
  * and it took the coverage gate with it. A dead worker returns no coverage
    data, so a single killed process reported as "total of 65.13 is less than
    fail-under=80.00" — a failure naming neither the test nor the cause.

Fires on ``os._exit(...)`` anywhere in ``src/coder_eval/`` that is not lexically
inside a branch testing ``CODER_EVAL_IN_CONTAINER``. That env var is the repo's
established in-container predicate (``Sandbox.enforces_permission_windows``,
``orchestration/evaluation.resolve_reference_dir``) and is deliberately NOT
``sandbox.driver`` — ``run_task_internal_command`` rewrites the driver to
``tempdir`` before building the in-container Orchestrator, so a driver-based gate
disables itself on precisely the path that needs it.

The check is lexical (an enclosing ``if``/``elif`` whose test mentions the var),
not a data-flow proof. That is enough to force the guard to be written down at
the site, which is the property that was missing.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_GATE = "CODER_EVAL_IN_CONTAINER"

_MESSAGE = (
    "`os._exit` here is not gated on CODER_EVAL_IN_CONTAINER. It kills the process outright — "
    "no atexit, no finally, no traceback — which is the right primitive ONLY for the container's "
    "own main process. Anywhere else it destroys a host process that merely called this code: an "
    "unconditionally-armed watchdog once exited a pytest worker 40s after the test that armed it, "
    "reporting as a random crash in an unrelated file and as a bogus coverage failure. Gate it on "
    '`os.environ.get("CODER_EVAL_IN_CONTAINER") == "1"`, or add `# noqa: CE052` with a reason.'
)


def _is_os_exit(node: ast.Call) -> bool:
    """``os._exit(...)`` under any module alias (it is imported as ``_os`` here)."""
    return isinstance(node.func, ast.Attribute) and node.func.attr == "_exit"


class ProcessLethalMustBeContainerGated(BaseRule):
    id = "CE052"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        # `(^|sep)` so a repo-relative path is in scope too; see CE047.
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
        return any(_GATE in ast.dump(test) for test in self._guards)
