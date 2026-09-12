"""CE061: a generation window must come from the shared helper.

Pi shipped measuring its window from its own ``turn_start`` while four sibling
reducers tiled from a mark, so the wall clock between one turn's end and the
next turn's start — the model time that PRODUCED that turn — fell into no
bucket at all. Nothing failed. ``docs/agents/HARNESS_PARITY.md`` asserted the
four-bucket identity, and the only sensor for it
(``tests/_fixtures/golden_streams/_scrub.py``) checks ONE side: it catches a
bucket claiming more time than the turn contains and says nothing about one
claiming less. Pi's own tests passed because they were written against Pi's
own arithmetic.

That is the shape this rule guards against: not a reducer that computes the
window wrongly, but a reducer that computes it AT ALL instead of asking
``coder_eval.timing.close_window``. A new harness whose author reimplements the
arithmetic inline arrives with a green test suite by construction.

Separate id from CE058, CE059 and CE060 deliberately. Those three are about the
VALUES a message carries — an unknown duration published as a literal, a window
built from one clock read, a missing identity. This one is about PROVENANCE:
where the arithmetic came from. One invariant per id is what makes a ``# noqa``
mean one thing.

BLIND SPOT, and it is the whole weakness of the chosen shape: this proves the
module IMPORTS the helper, never that any particular call used it. The value
passed to ``generation_duration_ms=`` is always a local (``generation_ms``,
``gen_parts[idx]``), so no AST rule can trace it back to a call. The sensors for
the arithmetic itself are ``tests/test_timing_close_window.py`` and the
per-reducer window tests; this rule adds only the cheap structural half that
neither can reach — a sixth harness rolling its own.

It costs exactly one permanent suppression. ``claude_code_agent.py`` computes
its window from a monotonic delta and subtracts tool time ONCE at finalization
across every emission, because a call issued by an earlier emission is still
running when the next window closes. Forcing that into ``close_window`` means a
mode flag on a helper whose whole value is having one shape.

EXEMPT, because both are honest claims that no window was measured: an explicit
``generation_duration_ms=None`` (codex's rollout rebuild, claude-code's
sub-agent synthesis) and the kwarg absent altogether, which defaults to
``None``. Not matched: ``**``-expansion and ``model_copy(update={...})`` — CE058
already covers the ``model_copy`` dict shape for timing literals.

Alias resolution, and its blind spot, live in ``_model_ctor.py``, shared with
CE060. The helper's own name is taken from the function object rather than
written here as a string, so renaming it moves this rule too.
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
                    "window of its own. Every window is the same arithmetic: tile from the mark, "
                    "keep a backwards stamp from inverting the span, bound the calls still open at "
                    "the boundary, subtract the UNION of the tool intervals clipped to the window, "
                    "clamp at zero. Pi got that wrong by measuring from its own turn start, and "
                    "nothing caught it because the four-bucket identity is only asserted as an "
                    f"upper bound. Call {_HELPER} instead. If this harness genuinely cannot use it "
                    "— claude-code subtracts once at finalization across every emission — add "
                    "'# noqa: CE061' with a comment saying why.",
                )
        self.generic_visit(node)
