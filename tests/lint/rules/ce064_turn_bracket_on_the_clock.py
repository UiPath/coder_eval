"""CE064: a clocked harness must stamp its turn BRACKET off that same clock.

``decompose_turn`` computes the head and the tail by subtracting a generation
window bound from an ``AgentStartEvent`` / ``AgentEndEvent`` timestamp. Those
two stamps therefore have to share a basis, and a reducer that derives its
window bounds from a ``TurnClock`` while letting the bracket fall back to
``StreamEvent.timestamp``'s ``default_factory=datetime.now`` puts a
monotonic-derived stamp and a raw wall stamp inside one subtraction — the exact
split ``timing.TurnClock`` exists to remove, reintroduced at the one seam the
clock does not own.

MEASURED, not hypothetical. Instrumenting ``decompose_turn`` on a live
antigravity turn printed::

    PROBE tail: elapsed=-0.017000ms busy=0.000000ms raw=-0.017000ms
                last_completed = 09:05:22.033099
                agent_end      = 09:05:22.033082

an ``AgentEndEvent`` stamped 17 us BEFORE its own last message finished, which
cannot happen: the event is constructed strictly after the final flush.
``decompose_turn`` then clamps the negative to ``0.0`` and publishes it, which
is "measured, and instant" — the CE058 confusion, arrived at from the other
direction. The published ``harness_teardown_ms`` was ``0.0`` for a harness
whose real tail is ~0.1 ms.

WHY IT ONLY SHOWED ON ONE HARNESS, and why the rule is not scoped to that one:
the drift between the two clocks is tens of microseconds, so it can only flip a
sign where the true interval is itself that small. Antigravity is the only
harness that spawns its process ONCE in ``start()`` and holds it across turns,
so nothing happens between its last flush and its ``AgentEndEvent``; every
other harness books a head of 0.2-6 s and a tail of 7-543 ms, where the drift
is invisible. Invisible is not absent. The fix belongs at every clocked site
because that is what makes the subtraction single-basis rather than
usually-close, and "usually-close" is not a property a millisecond field can
rest on.

SCOPE IS DERIVED, never listed. The rule applies to a module under
``agents/`` that imports ``TurnClock`` — antigravity, pi and claude-code today.
Codex and OpenCode take their spans from the CLI's own epoch stamps and
deliberately have no ``TurnClock`` (see that class's docstring), so a raw
``datetime.now()`` bracket is CONSISTENT with their bounds and the rule must
not fire on them; the noop agent has no windows at all. The day one of them
adopts a clock, this rule starts applying to it with no edit here — which is
the half a hardcoded harness list would get wrong.

Separate id from CE058/CE059/CE060/CE061 for the reason CE060 states: one
invariant per id, so a ``# noqa`` means one thing. CE058 is about publishing a
literal for an unknown duration, CE059 about a window built from a single clock
read, CE060 about identity, CE061 about where a window's arithmetic comes from.
This one is about the turn's OUTER bounds, which no other rule looks at — they
all scope to ``AssistantMessage``, and the bracket is not one.

BLIND SPOT: presence, not correctness. The rule requires ``timestamp=`` to be
passed; it cannot tell ``self.clock.now()`` from ``datetime.now()`` written out
at the call site, because an agent may legitimately reach its clock through any
expression (a local ``clock`` in ``communicate``, ``state.clock`` from the
caller, ``self.clock`` inside the state). Demanding a specific spelling would
make the rule a syntax check on three harnesses' internal structure. What it
removes is the SILENT case — a default nobody chose — which is the one that
shipped.
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
