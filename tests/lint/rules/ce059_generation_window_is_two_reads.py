"""CE059: one clock read cannot measure a window.

In ``src/coder_eval/agents/``, an ``AssistantMessage`` (or
``AssistantMessageTelemetry``) call may not pass the same name for both ``started_at``
and ``completed_at``: that records a zero-length window, whatever
``generation_duration_ms`` says. The check is skipped unless BOTH bounds are a bare
``ast.Name``; attribute and call expressions are not compared.

It does NOT fire when the same call passes ``generation_duration_ms=None``. That call
states that no window was measurable, so it claims none. Keep this exemption; do not
replace it with ``# noqa`` lines at those sites.

BLIND SPOT: two DIFFERENT names that hold the same instant at runtime. No AST rule can
see it; the replay-based ``assert_timing_captured`` golden invariant catches it.

Rationale: .claude/notes/lint-rules.md § CE059
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_AGENTS_ROOT = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]agents[/\\]")

_MESSAGE_CONSTRUCTORS = frozenset({"AssistantMessage", "AssistantMessageTelemetry"})


def _is_none(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


class GenerationWindowIsTwoReads(BaseRule):
    id = "CE059"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(_AGENTS_ROOT.search(filepath))

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_scope:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in _MESSAGE_CONSTRUCTORS:
                kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
                start, end = kwargs.get("started_at"), kwargs.get("completed_at")
                claims_a_window = not _is_none(kwargs.get("generation_duration_ms"))
                if claims_a_window and isinstance(start, ast.Name) and isinstance(end, ast.Name) and start.id == end.id:
                    self.violation(
                        node,
                        f"'started_at' and 'completed_at' are both {start.id!r}, so the generation "
                        "window has zero length while generation_duration_ms claims one beside it. "
                        "Antigravity shipped this on 368 of 368 sampled messages. Read the clock "
                        "twice — mark the end of the previous SDK event, and read again when this "
                        "message arrives — or pass generation_duration_ms=None if no window is "
                        "measurable.",
                    )
        self.generic_visit(node)
