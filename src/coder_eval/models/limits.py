"""Run-time budget limits for task execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RunLimits(BaseModel):
    """Run-time caps that abort a task when exceeded.

    Unifies structural caps (max_turns, task_timeout, turn_timeout) and
    budget caps (tokens, USD). Budget caps are checked after each completed
    agent turn and are cumulative across all turns of a single task; they
    apply to the subject agent only — judge and simulator token spend are
    not counted.

    Any subset of fields is valid; an empty block is legal.
    """

    model_config = ConfigDict(extra="forbid")

    max_turns: int | None = Field(
        default=None,
        gt=0,
        description="Max agent inner-loop turns per iteration. None = SDK default.",
    )
    task_timeout: int | None = Field(
        default=None,
        ge=30,
        description="Max seconds for the entire evaluation loop (all iterations). None = no limit.",
    )
    turn_timeout: int | None = Field(
        default=None,
        ge=10,
        description="Max seconds per agent communicate() call. None = no limit.",
    )
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
