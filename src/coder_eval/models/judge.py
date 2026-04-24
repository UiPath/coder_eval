"""Typed verdict contract for judge-style criteria (llm_judge, agent_judge)."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field, field_validator


class JudgeVerdict(BaseModel):
    """Typed contract for a judge's JSON verdict.

    Score is clamped to [0.0, 1.0] by a validator (not a Field constraint) so
    out-of-range values from the model become 0.0/1.0 rather than parse errors —
    matching the legacy ``extract_score`` clamp. Non-finite values (NaN, +/-Infinity)
    are rejected explicitly because ``max(0.0, min(1.0, nan))`` silently yields 1.0.
    Booleans are rejected because Python treats them as ints.
    """

    model_config = {"extra": "ignore"}

    score: float = Field(description="Verdict score, clamped to [0.0, 1.0]")
    rationale: str = Field(default="", description="1-2 sentence summary")

    @field_validator("score", mode="before")
    @classmethod
    def _clamp_score(cls, v: Any) -> float:
        if isinstance(v, bool):
            raise ValueError(f"score is not a number: {v!r}")
        try:
            f = float(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"score is not a number: {v!r}") from e
        if not math.isfinite(f):
            raise ValueError(f"score is not finite: {v!r}")
        return max(0.0, min(1.0, f))

    @field_validator("rationale", mode="before")
    @classmethod
    def _coerce_rationale(cls, v: Any) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError(f"rationale must be a string, got {type(v).__name__}")
        return v.strip()
