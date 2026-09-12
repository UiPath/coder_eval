"""CE059: one clock read cannot measure a window.

An ``AssistantMessage`` that receives the SAME name for both ``started_at`` and
``completed_at`` records a zero-length generation window — whatever
``generation_duration_ms`` happens to say beside it. The Antigravity reducer
read ``datetime.now()`` once and passed it as both bounds, so
``started_at == completed_at`` on 368 of 368 sampled messages and every
consumer that derives a window from the two stamps saw nothing at all. A window
needs two reads at two moments.

Separate id from CE058 deliberately: this is a different invariant (a
zero-length window, regardless of what the duration field says), and one
invariant per id is what makes a ``# noqa`` mean one thing.

WHAT IT DOES NOT FIRE ON, and why that is the rule rather than a stack of
suppressions: a call that passes ``generation_duration_ms=None`` in the same
breath is not claiming a window — it is saying, in the field built to say it,
that none was measurable. Three sites are legitimately like that (Codex's
rollout rebuild, and both sub-agent syntheses on Codex and Claude: the
generation arrives as a tool result and is never streamed), and collapsing
their bounds to one ``now()`` is then a formatting choice, not a false
measurement. Exempting them here — rather than through four permanent
``# noqa`` lines — keeps the rule pointed at the case that actually misleads:
a duration asserted beside two stamps that cannot support it.

Scoped to ``src/coder_eval/agents/``, the layer that measures. The check is
skipped unless BOTH bounds are a bare ``ast.Name`` — comparing attribute or
call expressions (``self.a`` vs ``self.b``) would be guesswork.

BLIND SPOT: two DIFFERENT names that hold the same instant at runtime. Codex
already produces that shape — ``started = _ms_to_dt(self.open_start_ms)`` and
``completed = _ms_to_dt(self.open_end_ms if ... is not None else
self.open_start_ms)`` collapse to one instant whenever ``open_end_ms`` is
None. No AST rule can see it. The catch for that case is the replay-based
``assert_timing_captured`` golden invariant, which runs the real reducer and
asserts a non-zero window actually came out.
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
