"""CE060: an assistant message must declare its identity.

``AssistantMessage.message_id`` is what lets a consumer tell two generations
apart. Antigravity simply omitted the kwarg, so the field defaulted to ``None``
on every message it ever recorded, and the evalboard — which groups assistant
emissions by ``message_id`` and falls back to a wall-clock ``SAME_EMISSION_GAP_MS``
threshold when either side lacks one — folded a whole turn's generations into a
single timeline row once the harness's windows became contiguous. Nothing
failed: the consumer sums a group, so every total came out right, and the
golden snapshots had ratified the ``null`` the day they were written. It was
not confined to the timeline either — a grouped emission is one API call to the
thinking-cost simulator, so its whole cache cascade was computed from one call
per turn. The mechanism and the blast radius live in
``docs/agents/HARNESS_PARITY.md`` § Timing capture; neither is restated here.

Separate id from CE058 and CE059 deliberately: those two are about *timing*
(an unknown duration published as a literal, a window built from one clock
read), this one is about *identity*. One invariant per id is what makes a
``# noqa`` mean one thing.

WHY IT RESOLVES ALIASES where CE058 and CE059 hardcode constructor names:
CE058's own docstring already concedes that spelling-based matching dies on a
rename, and the weakness is live — ``claude_code_agent.py`` binds *only*
``AssistantMessage as AssistantMessageTelemetry`` and never the bare name, so a
name list guards that file's two construction sites purely because somebody
wrote the current alias into a different rule. CE060 instead derives its
constructor set from each module's own ``coder_eval.models`` imports, which
removes the gap rather than documenting it and catches an arbitrary
``AssistantMessage as Msg`` besides. Widening the other two rules the same way
is recorded in ``.claude/harness-candidates.md``; it is a change to two shipped
rules and needs its own mutation checks.

What it removes is the *local binding* spelling, not every rename: the class's
own name still has to be known, so it is taken from the model itself
(``AssistantMessage.__name__``) rather than written here as a string, the way
CE056 imports ``IN_CONTAINER_ENV`` and CE057 derives its target set from
``SIDECAR_MODULES``. Renaming the model therefore moves this rule with it.

That resolution lives in ``_model_ctor.py`` and is shared with CE061, which
needs the identical answer to a different question. Keeping two copies would
mean a new import spelling needs two fixes in two rules.

BLIND SPOT 1: the runtime ``None``. The rule requires the kwarg to be
*present*, not non-``None`` when it runs. ``opencode_agent.py`` passes
``str(part.get("messageID") or "") or None`` and ``pi_agent.py`` the same shape
for ``responseId``, so either records ``None`` whenever the id is missing from
the payload (`pi_a_single_text_turn.json` is a snapshot of that shape, though
its null comes from a fixture that emits no ``responseId`` rather than from a
live CLI omission). No AST rule can see it, and demanding a statically
non-``None`` value would be wrong: passing a fallback expression *is* deciding
what the id is. The sensor for that case is the golden corpus, and only
partially — a snapshot is written from whatever the code currently does, so it
catches a later change, never an initial omission.

BLIND SPOT 2: a binding the resolver cannot follow. It reads one module's own
imports, so it sees the direct forms — absolute or relative ``from ... import
AssistantMessage``, under any alias — and the attribute spelling
``<module>.AssistantMessage(...)``. What remains invisible is a re-export
through an intermediate module (``from .sibling import AssistantMessage``); see
``_model_ctor.py``.

A ``**``-expanded call fires: such a call has not declared the field at the
site. There is no carve-out because no site in ``src/coder_eval/agents/`` uses
``**`` expansion for these constructors; if one is ever added, pass
``message_id=`` explicitly beside it.
"""

import ast

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


class MessageIdDeclared(BaseRule):
    id = "CE060"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(AGENTS_ROOT.search(filepath))
        self._names: set[str] = set()

    def check(self, tree: ast.AST) -> list[Violation]:
        self._names = local_bindings(tree, ASSISTANT_MESSAGE)
        return super().check(tree)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_scope:
            name = constructor_name(node.func, self._names, ASSISTANT_MESSAGE)
            if name is not None:
                kwargs = keywords_of(node)
                if "message_id" not in kwargs or is_none(kwargs["message_id"]):
                    self.violation(
                        node,
                        f"{name}(...) leaves 'message_id' undeclared — absent, or an explicit None — "
                        "so every message it builds shares one empty identity. Pass the field "
                        "with a real value: the CLI's own "
                        "id where the stream carries one, else synthesize it the way Codex does "
                        "(f'{turn_id}-msg-{gen_index}'). Antigravity shipped without it: the "
                        "evalboard then falls back to its SAME_EMISSION_GAP_MS wall-clock gap to "
                        "group emissions, and a harness whose generation windows are contiguous "
                        "has every one of a turn's generations collapse into one row. See "
                        "docs/agents/HARNESS_PARITY.md.",
                    )
        self.generic_visit(node)
