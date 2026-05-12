"""Run-time budget limits for task execution."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunLimits(BaseModel):
    """Mid-run budget caps that abort a task when exceeded.

    Checked after each completed agent turn. Cumulative across all turns
    of a single task. Applies to the subject agent only — judge and
    simulator token spend are not counted.
    """

    model_config = ConfigDict(extra="forbid")

    max_input_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Max cumulative input (prompt) tokens. None = unlimited.",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Max cumulative output (completion) tokens. None = unlimited.",
    )
    max_total_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Max cumulative input+output tokens. None = unlimited.",
    )
    max_usd: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Max cumulative cost in USD. Requires per-turn cost reporting "
            "(proxy mode or SDK-provided cost). Silently skipped if cost "
            "is None for every turn."
        ),
    )
    count_cached_input: bool = Field(
        default=False,
        description=(
            "When True, cache_read_input_tokens count toward input/total "
            "budgets. Default False — cached reads are typically free."
        ),
    )

    @model_validator(mode="after")
    def at_least_one_budget(self) -> Self:
        # Explicit None check (not truthiness) — field constraints already reject
        # 0 / 0.0, but reading `any((self.max_input_tokens, ...))` invites the
        # "is 0 falsy here?" question every time. Spell out the intent.
        if not any(
            v is not None
            for v in (
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_total_tokens,
                self.max_usd,
            )
        ):
            raise ValueError(
                "run_limits requires at least one of: max_input_tokens, max_output_tokens, max_total_tokens, max_usd"
            )
        return self
