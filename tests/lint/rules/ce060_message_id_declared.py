"""CE060: an assistant message must declare its identity.

In ``src/coder_eval/agents/``, every ``AssistantMessage(...)`` must pass
``message_id=``, not as a literal ``None``; ``**`` expansion does not count.

The rule derives its constructor set from each module's own ``coder_eval.models``
imports (an absolute or relative ``from`` import of ``AssistantMessage`` under
any alias, and
``<module>.AssistantMessage(...)``); the class name comes from
``AssistantMessage.__name__``. That resolution lives in ``_model_ctor.py``, shared
with CE061: fix a new import spelling there, once.

BLIND SPOT 1: the runtime ``None``. The kwarg must be PRESENT, not statically
non-``None``. A fallback expression (OpenCode ``messageID``, Pi ``responseId``) is a
decision and can still yield ``None``; the golden corpus catches only a later change.

BLIND SPOT 2: a re-export through an intermediate module
(``from .sibling import AssistantMessage``); see ``_model_ctor.py``.

Rationale: .claude/notes/lint-rules.md § CE060
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
