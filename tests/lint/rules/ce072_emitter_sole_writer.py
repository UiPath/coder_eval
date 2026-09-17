"""CE072: an agent adapter writes the event protocol only through ``TurnEmitter``.

The defect class: every harness once carried its own per-turn accumulator — the open
tools, the sequence numbers, the transcript, the reported usage, the inner turn and the
end event — six copies of one state machine. Each copy shipped its own defect, and
five timing rules (now retired; see the runner note) were each written after one of
them: a window built from one clock read, an assistant message with no identity, a
window computed by hand, tool time subtracted in an adapter, a turn bracket off the
turn clock. Those rules guarded the copies. ``TurnEmitter`` removed the copies, so one rule now keeps an
adapter from growing one back: it may not construct the events, the transcript
messages or the reducer itself.

Fires, in files under ``src/coder_eval/agents/``, on a call that constructs
``AgentStartEvent``, ``AgentEndEvent``, ``TurnStartEvent``, ``TurnEndEvent``,
``ToolStartEvent``, ``ToolEndEvent``, ``TextChunkEvent``, ``AssistantMessage`` or
``EventCollector`` — imported from ``coder_eval.streaming`` or one of its submodules,
under any import alias, through a relative import, or as a module attribute.
``CommandTelemetry`` is not banned: an adapter builds tool telemetry and hands it to
the emitter. Importing a class for a type annotation is allowed.

Blind spots: a plugin agent outside this tree (the emitter's runtime guards and
``coder_eval.testing`` are its sensors); ``AssistantMessage.model_validate(...)`` or
``model_copy`` building a message; a re-export through an intermediate module.
"""

import ast

from coder_eval.models import AssistantMessage
from coder_eval.streaming import events
from coder_eval.streaming.collector import EventCollector
from tests.lint.rules._model_ctor import AGENTS_ROOT, bindings_from, constructor_name
from tests.lint.rules.base import BaseRule
from tests.lint.violation import Violation


# Taken from the classes, never spelled here: a rename moves the rule with it.
_BANNED: dict[str, str] = {
    **{
        cls.__name__: "coder_eval.streaming"
        for cls in (
            events.AgentStartEvent,
            events.AgentEndEvent,
            events.TurnStartEvent,
            events.TurnEndEvent,
            events.ToolStartEvent,
            events.ToolEndEvent,
            events.TextChunkEvent,
        )
    },
    AssistantMessage.__name__: "coder_eval.models",
    EventCollector.__name__: "coder_eval.streaming",
}
_FIX = "report it through the turn's TurnEmitter (Agent._open_emitter), the sole writer of the event protocol"


class EmitterSoleWriter(BaseRule):
    id = "CE072"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(AGENTS_ROOT.search(filepath))
        self._names: dict[str, set[str]] = {}

    def check(self, tree: ast.AST) -> list[Violation]:
        if self._in_scope:
            self._names = {name: bindings_from(tree, name, module) for name, module in _BANNED.items()}
        return super().check(tree)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_scope:
            for class_name, names in self._names.items():
                spelled = constructor_name(node.func, names, class_name)
                if spelled is not None:
                    self.violation(node, f"architectural violation: '{spelled}(...)' built in an adapter — {_FIX}")
                    break
        self.generic_visit(node)
