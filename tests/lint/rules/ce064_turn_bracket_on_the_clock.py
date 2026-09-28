"""CE064: a clocked harness must stamp its turn BRACKET off that same clock.

In ``src/coder_eval/agents/``, a module that imports ``TurnClock`` must pass an
explicit ``timestamp=`` to every ``AgentStartEvent`` and ``AgentEndEvent``.
``decompose_turn`` subtracts a generation-window bound from a bracket stamp, so
both must share a basis; ``StreamEvent.timestamp`` defaults to a raw
``datetime.now()``.

SCOPE IS DERIVED, never a harness list: a module is in scope because it imports
``TurnClock``, so a harness that adopts a clock comes into scope with no edit
here. Which harnesses have a clock: ``docs/agents/HARNESS_PARITY.md``.

BLIND SPOT: presence, not correctness. The rule cannot tell
``self.clock.now()`` from ``datetime.now()`` written at the call site. The guard
for the source is behavioural: ``tests/_bracket_clock.py``.

Rationale: .claude/notes/lint-rules.md § CE064
"""

import ast

from coder_eval.streaming.events import AgentEndEvent, AgentStartEvent
from coder_eval.timing import TurnClock
from tests.lint.rules._model_ctor import AGENTS_ROOT, bindings_from, constructor_name, keywords_of
from tests.lint.rules.base import BaseRule
from tests.lint.violation import Violation


_EVENTS_MODULE = "coder_eval.streaming.events"
_TIMING_MODULE = "coder_eval.timing"

# Taken from the classes themselves, never spelled here: a rename moves the
# rule with them, the way CE056 imports IN_CONTAINER_ENV.
_BRACKETS = (AgentStartEvent.__name__, AgentEndEvent.__name__)
_CLOCK = TurnClock.__name__


class TurnBracketOnTheClock(BaseRule):
    id = "CE064"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(AGENTS_ROOT.search(filepath))
        self._clocked = False
        self._names: dict[str, set[str]] = {}

    def check(self, tree: ast.AST) -> list[Violation]:
        if not self._in_scope:
            return []
        self._clocked = bool(bindings_from(tree, _CLOCK, _TIMING_MODULE))
        if not self._clocked:
            return []
        self._names = {name: bindings_from(tree, name, _EVENTS_MODULE) for name in _BRACKETS}
        return super().check(tree)

    def visit_Call(self, node: ast.Call) -> None:
        for bracket in _BRACKETS:
            name = constructor_name(node.func, self._names[bracket], bracket)
            if name is None:
                continue
            if "timestamp" not in keywords_of(node):
                self.violation(
                    node,
                    f"{name}(...) leaves 'timestamp' to StreamEvent's default_factory "
                    "(a raw datetime.now()), but this harness derives its generation-window "
                    f"bounds from a {_CLOCK}. `timing.decompose_turn` subtracts one from the "
                    "other to get harness_startup_ms / harness_teardown_ms, so the two bases "
                    "meet inside one subtraction — measured at -0.017 ms on antigravity, an "
                    "agent end stamped BEFORE its own last message finished, which "
                    "decompose_turn then clamped to the 0.0 that means 'measured, and "
                    "instant' (CE058). Pass timestamp=<this turn's clock>.now().",
                )
        self.generic_visit(node)
