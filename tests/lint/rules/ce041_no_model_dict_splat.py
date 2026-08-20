"""CE041: never ``**<dict>``-splat into a ``coder_eval.models`` constructor.

A model built as ``ActivationGateVerdict(**verdict_kwargs)`` accepts whatever the dict happens to
hold. Rename a field, or mistype one of its string keys, and the value lands in no field at all:
with pydantic's default ``extra="ignore"`` the model is constructed successfully with that field
at its default — usually ``None`` — and every other number intact. Nothing raises, nothing logs,
and the type checker cannot see inside the dict to help.

That is not hypothetical. Both of the optimize gate's verdict models were built that way, on
models whose entire job is to say what a promotion decision rests on: a ``mean_dif`` typo would
have rendered a block reporting no difference at all, under a headline the reader trusts. The fix
is two-sided — ``extra="forbid"`` catches it at RUNTIME, literal keywords catch it STATICALLY —
and this rule is what keeps the static half from coming back.

**What it detects, precisely.** A ``Call`` whose callee name was imported from ``coder_eval.models``
(or a ``coder_eval.models.*`` submodule) in the same file, and whose arguments include **any** ``**``
splat. The operand shape is deliberately not narrowed: ``Model(**base)`` and
``Model(**{**base, **overrides})`` are the two spellings this repo actually had, but
``Model(**other.model_dump())`` is the commonest spelling in the wild and ``**cfg["v"]``,
``**dict(base, x=1)``, ``**(base | overrides)`` and a dict comprehension are all the same hole. A
rule that matched only a bare name and a dict display would name a boundary its users would read
as "splats are covered".

It is a shape check, and the boundary is worth stating so nobody trusts it further than it goes:

- A constructor reached through an ALIAS (``M = ActivationGateVerdict; M(**d)``), through a
  factory, or through a variable holding the class is NOT matched — the callee name has to be the
  imported one.
- A model imported some other way is not matched either: a plain ``import coder_eval.models`` plus
  attribute access, a star import (``from coder_eval.models import *``, which ruff's F403 rejects
  anyway), or — because ``_model_names`` fills in visit order — an import that appears *after* the
  call site in the file. This repo's own rule is that models come from ``coder_eval.models``, which
  CE002 enforces separately.
- ``model_copy(update={...})`` is a DIFFERENT hole and this rule does not cover it: pydantic does
  not validate ``update=`` keys even under ``extra="forbid"``, so a typo there is set as a bare
  instance attribute and dropped from ``model_dump()`` entirely. That one is now **CLOSED** by
  ``models.copy_with`` and **CE048**, which apply this same two-sided fix — a runtime raise plus
  literal keywords — to the update half. It mattered here because ``promoted`` and ``holm_alpha``,
  the two fields a promotion decision IS, were written only that way.
- The scan covers ``src/`` only. Tests legitimately build models from dicts — that is how a
  round-trip test is written.
- It matches on the NAME being imported from ``coder_eval.models``, so ``copy_with`` — a function
  exported from there, not a model — is matched too. Splatting a dict into it is the same defect
  one verb over and should be flagged; only the remediation sentence below reads oddly for it
  (there is no ``copy_with.model_validate``). Write the keywords out instead.

Add ``# noqa: CE041`` with a reason only where a dict really is the input (re-validating a payload,
say), and prefer ``Model.model_validate(payload)`` there, which validates rather than splats.
"""

import ast

from tests.lint.rules.base import BaseRule


_MODELS_MODULE = "coder_eval.models"


def _imports_models(module: str | None) -> bool:
    """True for ``from coder_eval.models import ...`` and its submodules, relative form included.

    Takes the module ALREADY RESOLVED, which :meth:`~tests.lint.rules.base.BaseRule.check_import`
    hands every rule. It used to resolve for itself, and before that it read ``node.module`` alone —
    which for ``from ..models import X`` is the string ``"models"``, so the guard was False and the
    rule saw nothing. That was not a corner case: it is how most of ``src/`` imports models, and
    CE041 consequently reported **0 violations against 8 real model-constructor splats** and had
    never fired.

    ``None`` — an import the resolver could not place — degrades to "not a models import" rather
    than guessing.
    """
    return bool(module) and (module == _MODELS_MODULE or module.startswith(f"{_MODELS_MODULE}."))


class NoModelDictSplat(BaseRule):
    id = "CE041"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._model_names: set[str] = set()

    def check_import(self, node: ast.ImportFrom, module: str | None) -> None:
        if _imports_models(module):
            # The bound name, so `as` renames are still tracked to the call site that uses them.
            self._model_names |= {alias.asname or alias.name for alias in node.names}

    def visit_Call(self, node: ast.Call) -> None:
        callee = node.func.id if isinstance(node.func, ast.Name) else None
        # `keyword.arg is None` IS the `**` splat, whatever it splats. Narrowing the operand to a
        # Name or a Dict display would miss `**other.model_dump()`, which is how this defect
        # usually arrives.
        if callee in self._model_names and any(keyword.arg is None for keyword in node.keywords):
            self.violation(
                node,
                f"{callee} is constructed by splatting a dict. A mistyped or renamed key then lands "
                "in no field at all and the model is built with that field at its default — "
                "silently, since pydantic ignores extras unless the model forbids them, and no type "
                "checker can see inside the dict. Pass literal keywords instead (or "
                f"{callee}.model_validate(payload) where a dict really is the input).",
            )
        self.generic_visit(node)
