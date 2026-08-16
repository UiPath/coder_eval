"""`copy_with` — the validated replacement for `model_copy(update={...})`.

The hole it closes is not hypothetical: `model_copy(update=)` sets an unknown key as a bare
instance attribute, absent from `model_dump()` entirely, with the field it was meant to set left
at its default and nothing raised. `test_the_hole_this_closes_is_real` reproduces exactly that on
a shipped model, so the rest of this file is testing a fix for a demonstrated defect.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from coder_eval.models import NoiseFloor, copy_with


class _Thing(BaseModel):
    name: str = "a"
    count: int = 0
    tags: list[str] = []


class _Subthing(_Thing):
    extra: str = "x"


def _floor(**overrides) -> NoiseFloor:
    return NoiseFloor(
        suite_id="my-skill-activation",
        variant_id="incumbent",
        model="claude-haiku-4-5-20251001",
        criterion_index=0,
        n_rows=12,
        n_invocations=3,
        confidence=0.95,
        seed=0,
        n_resamples=2000,
        split=None,
        mde=0.08,
        computed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        **overrides,
    )


def test_the_hole_this_closes_is_real() -> None:
    """`model_copy(update=)` accepts a key no field declares, silently. Pinned, not assumed."""
    typo = _floor().model_copy(update={"mde_typo": 9.9})  # noqa: CE048 — this IS the defect under test

    assert typo.mde_typo == 9.9  # type: ignore[attr-defined]  # a bare instance attribute...
    assert "mde_typo" not in typo.model_dump()  # ...that model_dump() has never heard of...
    assert typo.mde == 0.08  # ...while the field it was meant to set is untouched.


def test_a_known_field_is_updated() -> None:
    updated = copy_with(_Thing(), name="b", count=3)
    assert (updated.name, updated.count) == ("b", 3)


def test_an_unknown_field_raises_naming_it_and_the_known_fields() -> None:
    with pytest.raises(ValueError, match=r"has no field\(s\) \['suite_i'\]") as excinfo:
        copy_with(_floor(), suite_i="typo")
    # Both halves: the bad name so the author sees the typo, and the known fields so they see the
    # one they meant. A message with only the first is a riddle on a model with 12 fields.
    assert "suite_id" in str(excinfo.value)


def test_it_does_not_mutate_the_original() -> None:
    original = _Thing(name="a")
    copy_with(original, name="b")
    assert original.name == "a"


def test_it_returns_the_same_type() -> None:
    updated = copy_with(_Subthing(), name="b")
    assert type(updated) is _Subthing
    assert updated.extra == "x"


def test_a_model_with_a_field_named_model_is_updatable() -> None:
    """The positional-only guard, named after the real collision it was found on.

    `NoiseFloor` has a field literally called `model`. With an ordinary first parameter this call
    raises `TypeError: got multiple values for argument 'model'`.
    """
    assert copy_with(_floor(), model="claude-sonnet-5").model == "claude-sonnet-5"


def test_no_updates_is_an_identity_copy() -> None:
    original = _Thing(name="b", count=2, tags=["t"])
    assert copy_with(original).model_dump() == original.model_dump()


def test_a_list_field_stays_aliased_exactly_as_model_copy_left_it() -> None:
    """The detached-list hazard, pinned so a conversion cannot quietly change it.

    `copy_with` inherits `model_copy`'s shallow semantics exactly: the copy's list IS the object
    passed in, so appending to that local afterwards mutates the model too. Callers that build a
    `notes` list by appending must not reorder an append relative to the call.
    """
    notes = ["first"]
    copied = copy_with(_Thing(), tags=notes)
    assert copied.tags is notes

    notes.append("second")
    assert copied.tags == ["first", "second"]


def test_the_error_is_a_valueerror_not_a_typeerror() -> None:
    # Callers catch ValueError around model construction; a TypeError here would escape those.
    with pytest.raises(ValueError):
        copy_with(_Thing(), nope=1)
