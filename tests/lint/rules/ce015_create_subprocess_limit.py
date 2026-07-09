"""CE015: ``asyncio.create_subprocess_exec`` / ``create_subprocess_shell`` must pass ``limit=``.

asyncio's StreamReader caps a single line at 64 KiB (``_DEFAULT_LIMIT``) by
default. Any caller that reads the child's stdout/stderr line-by-line
(``readline`` / ``readuntil`` / ``async for``) raises ``ValueError`` the moment
one line exceeds that cap. For a line-delimited wire protocol (our
``STREAM_EVENT`` NDJSON) a single large event -- e.g. an agent Write carrying a
whole file -- blows past 64 KiB and tears the stream down mid-task. This was a
real incident: docker-driver tasks died with no ``task.json`` on nights the
agent emitted one big Write.

The fix is to pass an explicit ``limit=`` sized for the largest expected line
(see ``docker_runner.STDOUT_LINE_LIMIT_BYTES`` and
``Orchestrator._POST_RUN_STREAM_LIMIT``).

Add ``# noqa: CE015`` on the call if the child's output is consumed only via
``communicate()`` (which reads without the per-line cap) and the default is
genuinely fine.
"""

import ast

from tests.lint.rules.base import BaseRule


def _is_create_subprocess(func: ast.expr) -> bool:
    """Return True for ``asyncio.create_subprocess_exec`` / ``create_subprocess_shell``."""
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"create_subprocess_exec", "create_subprocess_shell"}:
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "asyncio"


class CreateSubprocessExplicitLimit(BaseRule):
    id = "CE015"

    def visit_Call(self, node: ast.Call) -> None:
        if _is_create_subprocess(node.func) and not any(kw.arg == "limit" for kw in node.keywords):
            self.violation(
                node,
                "asyncio.create_subprocess_exec/_shell without limit= uses StreamReader's 64 KiB "
                "per-line cap; a larger line raises ValueError mid-stream and kills the read loop. "
                "Pass an explicit limit= (see docker_runner.STDOUT_LINE_LIMIT_BYTES).",
            )
        self.generic_visit(node)
