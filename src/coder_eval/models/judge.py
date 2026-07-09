"""Typed verdict contract for judge-style criteria (llm_judge, agent_judge)."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field, field_validator


class JudgeVerdict(BaseModel):
    """Typed contract for a judge's verdict.

    Used both as the tool-input schema for the ``submit_verdict`` call (across
    SDK MCP and Anthropic-native ``tools``) and as the validation target for the
    extractors in ``coder_eval.evaluation.verdict_tool``.

    Score is clamped to [0.0, 1.0] by a validator (not a Field constraint) so
    out-of-range values from the model become 0.0/1.0 rather than tool-input
    validation errors. Non-finite values (NaN, +/-Infinity) are rejected
    explicitly because ``max(0.0, min(1.0, nan))`` silently yields 1.0.
    Booleans are rejected because Python treats them as ints.

    ``findings`` carries the audit trail — the system prompt always asks
    the judge to emit it. The field stays optional on the wire so a model
    that drops it still parses (the parser never falls back to score=0.0
    for missing findings), but the empty-findings case should be rare.
    """

    model_config = {"extra": "ignore"}

    score: float = Field(description="Verdict score, clamped to [0.0, 1.0]")
    rationale: str = Field(default="", description="1-2 sentence headline summary")
    findings: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete observations the judge cited from the artifacts (e.g. 'main.py:12 missing return'). "
            "Each entry is a short bullet. Empty when the judge omitted them."
        ),
    )

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
        # Collapse internal whitespace (newlines, tabs, runs of spaces) to single
        # spaces. Two reasons:
        #   1. ``format_details`` writes "rationale: <text>" on one line; a multi-line
        #      rationale would break that single-line invariant.
        #   2. ``reports_html._extract_rationale`` parses by line and grabs only the
        #      first one starting with "rationale: " — multi-line content would be
        #      silently truncated in the Judge Verdicts card.
        # The schema asks for "1-2 sentence headline summary"; collapsing whitespace
        # makes that contract explicit.
        collapsed = " ".join(v.split())
        if not collapsed:
            # Whitespace-only / empty input surfaces as a validation error so the
            # judge result records the error string instead of silently emitting
            # a blank ``rationale: `` line in ``format_details``. The
            # ``extract_verdict_from_*`` extractors translate the
            # ``ValidationError`` into a JudgeCriterionResult(score=0.0, error=...)
            # — uniform shape across all three backends.
            raise ValueError("rationale is empty after whitespace collapse")
        return collapsed

    @field_validator("findings", mode="before")
    @classmethod
    def _coerce_findings(cls, v: Any) -> list[str]:
        # Same leniency: drop anything that doesn't look like a list of strings.
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
