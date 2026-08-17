"""A validated replacement for ``model_copy(update={...})``.

**The hole it closes.** ``model_copy(update=)`` does not check the update's KEYS. A mistyped or
renamed key is set as a bare instance attribute: it is absent from ``model_dump()`` entirely, the
field it was meant to set is left at its default, nothing raises, nothing logs, and
``extra="forbid"`` does not help — that governs *validation*, and ``model_copy`` skips validation
by design. Measured on ``NoiseFloor``: ``.model_copy(update={"mde_typo": 9.9})`` returns a model
whose ``mde_typo`` reads back as ``9.9`` as a plain attribute while ``model_dump()`` has never
heard of it.

Both optimize-gate verdicts were written that way — on models whose entire job is to say what a
promotion decision rests on, where a ``mean_dif`` typo renders a block reporting no difference at
all.

**Why keywords rather than a dict, and what that does NOT buy.** Literal keywords put the field
names in the source, where a reader and a grep-driven rename can see them; a dict literal hid them.
They do **not** buy a static check: this signature is ``**updates: object``, so pyright accepts
every keyword and every value type — ``copy_with(floor, mde_typo=1.0, mde="not a float")`` type-
checks clean. The raise below is the whole enforcement. That makes this weaker than CE041's shape,
where literal keywords into a real constructor genuinely ARE checked statically, and the difference
is stated here rather than left to be discovered.

Three things this deliberately does **not** do:

1. **It does not re-validate the VALUE.** ``model_copy`` never has, and this closes the *key* hole
   and nothing else. A caller assigning a ``str`` to an ``int`` field still gets a model that
   fails only at serialization. Re-validating would mean
   ``model_validate(model.model_dump() | updates)``, which re-runs every validator on every field
   on paths (``holm_promote``) that run once per candidate per round.
2. **It does not accept a dict.** A genuine dict update — one built from user YAML, say, or one
   needing ``deep=True`` — keeps ``model_copy(update=)`` under an explicit ``# noqa: CE048``
   naming the reason. There is **no such site in the tree**: ``criteria/agent_judge.py`` was the
   one, and it moved to ``model_validate({**defaults.model_dump(), **user_overrides})`` once its
   field was narrowed to a single model. That is the other shape a dict update can take, and it
   is the better one wherever re-running the validators is affordable — it checks the VALUES too,
   which this function deliberately does not.
3. **It does not admit an extra on an ``extra="allow"`` model.** ``CriterionResult`` declares
   ``extra="allow"`` so its subclasses may carry undeclared fields, and there
   ``model_copy(update=)`` routes an unknown key into ``__pydantic_extra__`` where it DOES survive
   ``model_dump()``. This raises on that key instead. That is the conservative side of the trade —
   the failure is loud and at the call site — but a caller who genuinely means to set an extra has
   to say so with ``model_copy(update=)`` and a ``# noqa: CE048``.
"""

from __future__ import annotations

from pydantic import BaseModel


def copy_with[T: BaseModel](model: T, /, **updates: object) -> T:
    """Return a copy of ``model`` with ``updates`` applied, raising on an unknown field name.

    The first parameter is **positional-only**, and that is not stylistic: ``NoiseFloor`` has a
    field literally named ``model``, so ``copy_with(floor, model="opus")`` raises
    ``TypeError: got multiple values for argument 'model'`` under an ordinary parameter and works
    under a positional-only one. It costs one character and removes the whole class of collision.
    """
    unknown = sorted(set(updates) - set(type(model).model_fields))
    if unknown:
        raise ValueError(
            f"{type(model).__name__} has no field(s) {unknown} — known fields: {sorted(type(model).model_fields)}"
        )
    return model.model_copy(update=updates)
