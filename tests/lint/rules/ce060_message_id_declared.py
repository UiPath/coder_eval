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

BLIND SPOT 2: a binding this file cannot resolve. ``check()`` reads one
module's own imports, so it sees the direct forms — absolute or relative
``from ... import AssistantMessage``, under any alias — and the attribute
spelling ``<module>.AssistantMessage(...)``, which is matched on the attribute
alone precisely because the module binding it comes through (``import
coder_eval.models as models``, ``from coder_eval import models``) is the part a
class-binding walk misses. What remains invisible is a re-export through an
intermediate module (``from .sibling import AssistantMessage``): resolving that
means following imports across files, which no rule in this package does.

A ``**``-expanded call fires: such a call has not declared the field at the
site. There is no carve-out because no site in ``src/coder_eval/agents/`` uses
``**`` expansion for these constructors; if one is ever added, pass
``message_id=`` explicitly beside it.
"""

import ast
import re

from coder_eval.models import AssistantMessage
from tests.lint.rules.base import BaseRule
from tests.lint.violation import Violation


_AGENTS_ROOT = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]agents[/\\]")

_MODELS_MODULE = "coder_eval.models"
_MODELS_TAIL = _MODELS_MODULE.rpartition(".")[2]

# Taken from the model, never spelled here: a rename then moves the rule too.
_CLASS = AssistantMessage.__name__


def _binds_the_model(node: ast.ImportFrom) -> bool:
    """True if this `from ... import` reaches `coder_eval.models`.

    A relative import inside `agents/` (`from ..models import ...`) carries only
    the tail in `node.module`, so testing the absolute path alone would leave the
    rule silently blind for a whole file — and `agents/` does use relative
    imports.
    """
    module = node.module or ""
    if module.startswith(_MODELS_MODULE):
        return True
    return bool(node.level) and (module == _MODELS_TAIL or module.startswith(f"{_MODELS_TAIL}."))


def _is_none(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


class MessageIdDeclared(BaseRule):
    id = "CE060"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(_AGENTS_ROOT.search(filepath))
        # Local bindings of coder_eval.models.AssistantMessage in THIS module.
        # Built per file in check(): caching it across files would leak one
        # module's alias into another's matching.
        self._names: set[str] = set()

    def check(self, tree: ast.AST) -> list[Violation]:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and _binds_the_model(node):
                self._names.update(a.asname or a.name for a in node.names if a.name == _CLASS)
        return super().check(tree)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_scope:
            func = node.func
            # A bare name has to be bound in this module to be ours; the
            # attribute spelling is matched on the attribute alone, since the
            # module binding it arrives through is what an import walk over one
            # file's class bindings cannot see (see BLIND SPOT 2).
            name = (
                func.id
                if isinstance(func, ast.Name) and func.id in self._names
                else func.attr
                if isinstance(func, ast.Attribute) and func.attr == _CLASS
                else None
            )
            if name is not None:
                kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
                if "message_id" not in kwargs or _is_none(kwargs["message_id"]):
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
