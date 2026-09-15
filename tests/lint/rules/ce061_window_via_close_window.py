"""CE061: a generation window must come from the shared helper.

In ``src/coder_eval/agents/``, a module that passes a non-``None``
``generation_duration_ms=`` to an ``AssistantMessage`` must import
``coder_eval.timing.close_window`` (a from-import under any alias, or the
``timing`` module itself). The invariant is PROVENANCE of the window arithmetic;
tool-time subtraction is CE063's.

EXEMPT, as honest claims that no window was measured: an explicit
``generation_duration_ms=None`` and the kwarg absent. Not matched:
``**``-expansion and ``model_copy(update={...})`` (CE058 covers that dict shape).

BLIND SPOT: this proves the module IMPORTS the helper, never that a particular
call used it; the published value is always a local. The arithmetic's sensors
are ``tests/test_timing_close_window.py`` and the per-reducer window tests.

``TestCE061WindowViaCloseWindow::test_the_rule_is_now_exemption_free`` pins the
suppression set EMPTY, so a new exemption must be argued for. Alias resolution,
and its blind spot, live in ``_model_ctor.py``, shared with CE060.

Rationale: .claude/notes/lint-rules.md § CE061
"""

import ast

from coder_eval.timing import close_window
from tests.lint.rules._model_ctor import (
    AGENTS_ROOT,
    ASSISTANT_MESSAGE,
    constructor_name,
    is_none,
    keywords_of,
    local_bindings,
)
from tests.lint.rules.base import BaseRule
from tests.lint.violation import Violation


_TIMING_MODULE = "coder_eval.timing"
_TIMING_TAIL = _TIMING_MODULE.rpartition(".")[2]

# Taken from the function, never spelled here: a rename then moves the rule too.
_HELPER = close_window.__name__


def _imports_the_helper(tree: ast.AST) -> bool:
    """True if this module can reach `close_window` under any spelling.

    Both the `from`-import (under any alias) and the module import that makes
    `timing.close_window(...)` possible count — a rule that recognized only the
    first would tell an author to change a working call site.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            reaches = module.startswith(_TIMING_MODULE) or (
                bool(node.level) and (module == _TIMING_TAIL or module.startswith(f"{_TIMING_TAIL}."))
            )
            if reaches and any(a.name == _HELPER for a in node.names):
                return True
            # `from coder_eval import timing` / `from .. import timing`. The
            # package is checked too: `from anywhere import timing` is not this
            # module, and accepting it would let an unrelated name disarm the
            # rule for a whole file.
            package = module == _TIMING_MODULE.rpartition(".")[0] or (bool(node.level) and not module)
            if package and any(a.name == _TIMING_TAIL for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name == _TIMING_MODULE for a in node.names):
                return True
    return False


class WindowViaCloseWindow(BaseRule):
    id = "CE061"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(AGENTS_ROOT.search(filepath))
        self._names: set[str] = set()
        self._has_helper = False

    def check(self, tree: ast.AST) -> list[Violation]:
        self._names = local_bindings(tree, ASSISTANT_MESSAGE)
        self._has_helper = _imports_the_helper(tree)
        return super().check(tree)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_scope and not self._has_helper:
            name = constructor_name(node.func, self._names, ASSISTANT_MESSAGE)
            duration = keywords_of(node).get("generation_duration_ms")
            if name is not None and duration is not None and not is_none(duration):
                self.violation(
                    node,
                    f"{name}(...) publishes a measured 'generation_duration_ms' but this module "
                    f"never imports {_TIMING_MODULE}.{_HELPER} — so it is computing a generation "
                    "window of its own. Every window is the same geometry: tile from the mark, and "
                    "keep a backwards item stamp from inverting the span. Publish that RAW span; do "
                    "NOT subtract tool time here — coder_eval.timing.subtract_tool_time does it once, "
                    "for every harness, and doing it in the reducer too takes it out twice (CE063 "
                    "guards that half). Pi got the mark wrong by measuring from its own turn start, "
                    "and nothing caught it because the golden identity check is one-sided; "
                    "tests/test_timing_identity_contract.py is the two-sided one. "
                    f"Call {_HELPER} instead.",
                )
        self.generic_visit(node)
