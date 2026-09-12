"""CE063: a reducer may not compute its own tool subtraction.

Tool execution comes out of a generation window in exactly ONE place:
``coder_eval.timing.subtract_tool_time``. Before that, five
reducers each did it themselves — four through ``close_window`` as they
flushed, claude-code once at finalization — while the head and the tail were
already computed centrally at the collector seam. That asymmetry is where every
timing defect on this branch actually lived, and none of them was in the
arithmetic: they were in the bookkeeping AROUND it. When to reset a per-step
span list (clearing it at ``step_start`` wiped a span before the flush could
subtract it — a 100% overstatement of that window). When to clear a spent start
stamp (a second flush with no intervening start republished the previous span —
3000 ms of generation for a 2000 ms turn). When to advance the mark.

A sixth harness whose author reaches for ``busy_ms`` is rebuilding exactly that
bookkeeping, and its tool time would then be subtracted TWICE: once by the
reducer and once by the collector, which subtracts from every window it is
handed. The result is a silently under-reported generation figure on one
harness only — the shape that takes a corpus comparison to notice.

Separate id from CE061 deliberately, and CE061 is NOT rebodied into this.
CE061 asks where a window's ARITHMETIC came from, and four reducers still call
``close_window``, so its property is still live and still worth guarding — it
is not superseded. This one asks a different question: whether a reducer
subtracts tool time at all. One invariant per id is what makes a ``# noqa``
mean one thing. (Phase 5 did make CE061 exemption-free: claude-code now calls
the shrunken ``close_window`` like the other four, so its one permanent
suppression is gone.)

WHY NOT ``_imports_the_helper``, which CE061 uses. That function deliberately
returns True for a bare module import (``from coder_eval import timing``), so
that ``timing.close_window(...)`` counts as reaching the helper — its own
comment says a rule that missed it "would tell an author to change a working
call site." Inverted into a BAN that branch flags any reducer importing the
module and calling ``timing.close_window(...)``, which after Phase 5 is four of
them. So this rule keys on the ``busy_ms`` NAME binding plus an
``ast.Attribute`` match for the ``timing.busy_ms`` spelling, and leaves the
module import alone.

The name is taken from the function object rather than written here as a
string, the way CE061 takes ``close_window``: renaming it moves this rule too.

BLIND SPOT: a reducer that re-implements the union inline, without importing
anything, is invisible — as is one reaching ``busy_ms`` through a re-export.
The sensor for the arithmetic itself is
``tests/test_timing_identity_contract.py``, which drives every harness off a
scripted clock and asserts the four buckets tile the turn to the millisecond;
this rule adds only the cheap structural half that a static check can reach.
"""

import ast

from coder_eval.timing import busy_ms
from tests.lint.rules._model_ctor import AGENTS_ROOT
from tests.lint.rules.base import BaseRule


_TIMING_MODULE = "coder_eval.timing"
_TIMING_TAIL = _TIMING_MODULE.rpartition(".")[2]

# Taken from the function, never spelled here: a rename then moves the rule too.
_BANNED = busy_ms.__name__

_MESSAGE = (
    f"imports '{_BANNED}', but a reducer does not subtract tool time any more — "
    "coder_eval.timing.subtract_tool_time does it once, for every harness, "
    "at the single capture seam. Publish the RAW window (close_window gives you its bounds "
    "and span) and let the collector clip the tool union out of it. Subtracting here too "
    "takes it out twice and silently under-reports generation on this harness alone."
)


class NoBusyMsInAgents(BaseRule):
    id = "CE063"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(AGENTS_ROOT.search(filepath))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """`from coder_eval.timing import busy_ms`, under any alias.

        Relative forms (`from ..timing import busy_ms`) count too: the module
        is the same one whatever the path to it looks like.
        """
        if not self._in_scope:
            return
        module = node.module or ""
        reaches = module.startswith(_TIMING_MODULE) or (
            bool(node.level) and (module == _TIMING_TAIL or module.startswith(f"{_TIMING_TAIL}."))
        )
        if reaches and any(alias.name == _BANNED for alias in node.names):
            self.violation(node, _MESSAGE)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """The `timing.busy_ms` spelling.

        Defensive: no reducer uses it today (all five import plain names), but
        a name-binding check alone would let it through, and it is one arm.
        """
        if not self._in_scope:
            return
        if node.attr == _BANNED and isinstance(node.value, ast.Name) and node.value.id == _TIMING_TAIL:
            self.violation(node, _MESSAGE)
        self.generic_visit(node)
