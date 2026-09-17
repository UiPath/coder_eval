"""CE073: ``asyncio.create_subprocess_exec`` / ``create_subprocess_shell`` must pass ``stdin=``.

The defect: ``pi`` and ``opencode`` both read a non-TTY stdin TO EOF before they emit
anything. Neither adapter passed ``stdin=``, so the CLI inherited the parent's stdin, and a
``coder-eval run`` whose own stdin was a pipe that stayed open (a backgrounded or
tool-spawned batch) stalled every turn with zero events until the 300 s ``turn_timeout``.
Reproduced end to end on 2026-09-16: stdin held open → ``ERROR`` after the timeout with 0
commands; stdin on ``/dev/null`` → ``SUCCESS`` in 10 s. The same inheritance reached the
task's ``pre_run``/``post_run`` shell commands, where an authored ``read`` hangs the task.

Fires, anywhere under ``src/coder_eval/``, on an ``asyncio.create_subprocess_exec`` /
``create_subprocess_shell`` call (attribute form or a bare imported name) with no
``stdin=`` keyword. It requires a DECISION, not ``DEVNULL``: ``stdin=PIPE`` for a caller
that writes to the child passes. Sibling of CE015 (``limit=``); one invariant per id.

Blind spots: synchronous ``subprocess.run`` / ``Popen`` (which includes
``Sandbox.run_command``, running task-authored shell commands), ``loop.subprocess_exec`` /
``subprocess_shell``, ``anyio.open_process`` / ``run_process``, an explicit ``stdin=None``
(which still inherits), and a call through ``**kwargs``.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_SRC_ROOT = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]")
_SPAWNERS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})


def _spawner_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute) and func.attr in _SPAWNERS:
        return func.attr
    if isinstance(func, ast.Name) and func.id in _SPAWNERS:
        return func.id
    return None


class CreateSubprocessExplicitStdin(BaseRule):
    id = "CE073"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(_SRC_ROOT.search(filepath))

    def visit_Call(self, node: ast.Call) -> None:
        name = _spawner_name(node.func)
        if self._in_scope and name is not None and not any(kw.arg == "stdin" for kw in node.keywords):
            self.violation(
                node,
                f"{name} without stdin= inherits this process's stdin; a child that reads it to EOF stalls "
                + "while the parent's stdin stays open. Pass stdin= explicitly (asyncio.subprocess.DEVNULL "
                + "unless you write to the child).",
            )
        self.generic_visit(node)
