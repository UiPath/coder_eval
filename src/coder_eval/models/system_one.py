"""Rubric question models for the System One judge.

A System One model (TypeSafe's ``jev``) generates no text: it reads a state and
answers a map of typed questions with calibrated probability distributions. These
models are the task-authored half of that contract — one class per primitive
(``noul`` / ``choice`` / ``score``), each carrying the wire fields the API needs
plus the ``values`` a rubric uses to turn an answer back into a 0..1 score.

Pure data. The request payload and the answer reduction live in
``evaluation/system_one_scoring.py``.

Rationale: .claude/notes/contracts.md § System One rubric scoring
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


SYSTEM_ONE_MAX_CHOICE_OPTIONS = 255
SYSTEM_ONE_MIN_SCORE_LEVELS = 2
SYSTEM_ONE_MAX_SCORE_LEVELS = 10
SYSTEM_ONE_MAX_QUESTION_WEIGHT = 1_000_000.0


class BaseSystemOneQuestion(BaseModel):
    """Fields every rubric question shares.

    ``extra='forbid'`` propagates to each concrete subclass, so a typo in a
    question body is a load-time error rather than a silently dropped knob.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    instructions: str = Field(
        description=(
            "What this question asks of the state. Written for a model that answers it in "
            "isolation — name the artifact and the property, not 'the above'."
        )
    )
    weight: float = Field(
        default=1.0,
        gt=0.0,
        le=SYSTEM_ONE_MAX_QUESTION_WEIGHT,
        allow_inf_nan=False,
        description=(
            "Relative weight of this question in the criterion's weighted-mean score. "
            "Weights need not sum to 1.0 — they are normalized across the rubric. "
            "Bounded because an infinite or overflowing weight makes the weighted mean "
            "NaN, which would grade as full credit."
        ),
    )


class NoulQuestion(BaseSystemOneQuestion):
    """A yes/no question. The model answers with P(yes), not a hard boolean.

    Scores 1.0 when the returned probability agrees fully with ``expected``.
    """

    type: Literal["noul"] = "noul"  # pyright: ignore[reportIncompatibleVariableOverride]

    criteria: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional clarification of what yes and no mean, as a map with 'true' and/or "
            "'false' keys. Omit when ``instructions`` is already unambiguous."
        ),
    )
    expected: bool = Field(
        default=True,
        description=(
            "The answer that scores 1.0. With the default (true), the question's value is "
            "P(yes); set false to invert it so a 'no' is what the rubric rewards."
        ),
    )


class ChoiceQuestion(BaseSystemOneQuestion):
    """A pick-one question over author-defined options.

    Either ``expected`` (one option scores 1.0, the rest 0.0) or ``values``
    (a per-option partial credit map) must be set — a choice with no declared
    values has no way to become a score.
    """

    type: Literal["choice"] = "choice"  # pyright: ignore[reportIncompatibleVariableOverride]

    criteria: dict[str, str | None] = Field(
        description=(
            "The options, as a map of option key to a description of when it applies. "
            "A null description is allowed when the key speaks for itself, so a bare "
            "list of option names — criteria: [a, b, c] — is accepted and widens to "
            "that map."
        )
    )
    expected: str | None = Field(
        default=None,
        description="The single option that scores 1.0. Mutually exclusive with ``values``.",
    )
    values: dict[str, float] | None = Field(
        default=None,
        description=(
            "Partial credit per option, each in [0.0, 1.0]. Options omitted here score 0.0. "
            "Mutually exclusive with ``expected``."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _widen_bare_option_list(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        options = data.get("criteria")
        if not isinstance(options, list):
            return data
        # str() on anything would turn a stray `- ` into an option named 'None',
        # and YAML's bare `yes`/`no` into 'True'/'False'. Make that a load error.
        bad = [option for option in options if not isinstance(option, str)]
        if bad:
            raise ValueError(f"choice options must be strings; got {bad!r} (quote them if they are YAML keywords)")
        keys = list(options)
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"choice question lists duplicate options: {', '.join(duplicates)}")
        return {**data, "criteria": dict.fromkeys(keys)}

    @model_validator(mode="after")
    def _check_options(self) -> Self:
        if not self.criteria:
            raise ValueError("choice question needs at least one option in 'criteria'")
        if len(self.criteria) > SYSTEM_ONE_MAX_CHOICE_OPTIONS:
            raise ValueError(f"choice question has {len(self.criteria)} options; max {SYSTEM_ONE_MAX_CHOICE_OPTIONS}")
        if (self.expected is None) == (self.values is None):
            raise ValueError("choice question needs exactly one of 'expected' or 'values'")
        if self.expected is not None and self.expected not in self.criteria:
            raise ValueError(f"choice question's expected={self.expected!r} is not one of its options")
        for option, value in (self.values or {}).items():
            if option not in self.criteria:
                raise ValueError(f"choice question's values[{option!r}] is not one of its options")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"choice question's values[{option!r}]={value} is outside [0.0, 1.0]")
        return self


class ScoreQuestion(BaseSystemOneQuestion):
    """A rate-on-a-spectrum question over ordered levels.

    Levels are ordered worst-to-best by default: level *i* of *n* is worth
    ``i / (n - 1)``, so the last level scores 1.0. Set ``values`` to override
    that — an inverted or non-linear spectrum needs it.
    """

    type: Literal["score"] = "score"  # pyright: ignore[reportIncompatibleVariableOverride]

    criteria: list[str] = Field(
        description="Ordered level descriptions, worst first. Between 2 and 10 entries.",
    )
    values: list[float] | None = Field(
        default=None,
        description=(
            "Per-level credit, each in [0.0, 1.0], in the same order as ``criteria``. "
            "Defaults to an even ramp from 0.0 at the first level to 1.0 at the last."
        ),
    )

    @model_validator(mode="after")
    def _check_levels(self) -> Self:
        count = len(self.criteria)
        if not SYSTEM_ONE_MIN_SCORE_LEVELS <= count <= SYSTEM_ONE_MAX_SCORE_LEVELS:
            allowed = f"{SYSTEM_ONE_MIN_SCORE_LEVELS}-{SYSTEM_ONE_MAX_SCORE_LEVELS}"
            raise ValueError(f"score question has {count} levels; the API accepts {allowed}")
        if self.values is None:
            return self
        if len(self.values) != count:
            raise ValueError(f"score question has {count} levels but {len(self.values)} values")
        for i, value in enumerate(self.values):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"score question's values[{i}]={value} is outside [0.0, 1.0]")
        return self

    def level_values(self) -> list[float]:
        """Per-level credit, defaulting to an even ramp across the levels."""
        if self.values is not None:
            return list(self.values)
        last = len(self.criteria) - 1
        return [i / last for i in range(len(self.criteria))]


SystemOneQuestion = Annotated[
    NoulQuestion | ChoiceQuestion | ScoreQuestion,
    Field(discriminator="type"),
]
