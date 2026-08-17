"""CE048: ``model_copy(update={...})`` is replaced by ``models.copy_with`` everywhere in ``src/``.

``model_copy(update=)`` does not check the update's KEYS. A mistyped or renamed key is set as a
bare instance attribute: absent from ``model_dump()`` entirely, the field it was meant to set left
at its default, nothing raised and nothing logged. ``extra="forbid"`` does not help — that governs
*validation*, and ``model_copy`` skips validation by design.

Both optimize-gate verdicts were written that way, on models whose entire job is to say what a
promotion decision rests on. A ``mean_dif`` typo there renders a block reporting no difference at
all, and every reader of that block believes it.

**What it detects, precisely.** Any ``<expr>.model_copy(...)`` call carrying an ``update=``
keyword, dict literal or not, anywhere under ``src/`` except the canonical module
``models/copy_with.py`` — which is the one place that call may be made, because it is the thing
that checks the keys first.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.** It forbids the CALL
SHAPE and nothing more. It does not resolve the receiver's model type, so it cannot tell a valid
key from an invalid one — it routes every author to a helper that can. A ``copy_with`` reached
through an alias or a factory is not matched, and neither is a plain ``model_copy(deep=True)``
with no update. The scan is ``src/`` only, where the ``ALL_RULES`` sweep runs: a round-trip test
legitimately builds a model from a dumped dict.

This is CE041's shape one verb over, but **weaker on the static side, and that is worth stating**.
``copy_with``'s signature is ``**updates: object``, so pyright accepts any keyword: the runtime
raise is the whole enforcement, and what literal keywords buy is that the field names are in the
source where a reader and a rename can see them. CE041's own docstring named *update* as the hole
it left open; this closes it at runtime and keeps the call shape from coming back.

The intended fix is ``copy_with(model, field=value)``. A genuine dict update — one built from user
YAML, or one needing ``deep=True`` — may keep ``model_copy(update=)`` under a ``# noqa: CE048``
naming the reason, but **no such site remains**: ``criteria/agent_judge.py`` was the one, and it
took the other available shape, ``Model.model_validate({**defaults.model_dump(), **overrides})``,
once its field was narrowed from a union to a single model. That form re-runs the validators, so
it checks the values as well as the keys — prefer it wherever the cost is affordable, and reach
for a suppression only where it is not.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


# Matched against the forward-slash-normalized path (the convention CE009 sets), so a
# backslash alternative here would be dead: `filepath.replace` runs first.
_CANONICAL_MODULE = re.compile(r"/models/copy_with\.py$")

_FIX = (
    "model_copy(update={...}) does not check the update's KEYS: a mistyped or renamed one lands as "
    "a bare instance attribute, absent from model_dump() entirely, with the intended field left at "
    "its default and nothing raised. Use coder_eval.models.copy_with(model, field=value) — literal "
    "keywords so a reader and a rename can see the names, and a runtime raise on an unknown one. "
    "`# noqa: CE048` with a reason if the update really is a dict variable or needs deep=True."
)


class NoBareModelCopyUpdate(BaseRule):
    id = "CE048"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._is_canonical = bool(_CANONICAL_MODULE.search(filepath.replace("\\", "/")))

    def visit_Call(self, node: ast.Call) -> None:
        if (
            not self._is_canonical
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "model_copy"
            and any(keyword.arg == "update" for keyword in node.keywords)
        ):
            self.violation(node, _FIX)
        self.generic_visit(node)
