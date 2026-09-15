"""CE063: a reducer may not compute its own tool subtraction.

Tool execution comes out of a generation window in exactly ONE place:
``coder_eval.timing.subtract_tool_time``. In ``src/coder_eval/agents/`` this fires on
a ``from`` import of ``busy_ms`` (any alias, relative forms included) and on the
``timing.busy_ms`` spelling. Every timing defect in per-reducer subtraction lived
not in the arithmetic but in the bookkeeping AROUND it: when to reset a span list,
clear a spent start stamp, advance the mark. Separately, a reducer that also
subtracts takes tool time out twice.

HAZARD: do not reuse CE061's ``_imports_the_helper``: inverted into a ban, its
bare-module-import branch flags any reducer calling ``timing.close_window``. The banned name comes from the function object,
so a rename moves the rule.

BLIND SPOT: a reducer that re-implements the union inline, or reaches ``busy_ms``
through a re-export, is invisible. ``tests/test_timing_identity_contract.py`` is the
sensor for the arithmetic.

Rationale: .claude/notes/lint-rules.md § CE063
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
