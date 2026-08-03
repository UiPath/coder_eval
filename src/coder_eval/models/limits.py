"""Run-time budget limits for task execution."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    expected_turns: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Soft target for cumulative visible turns across a task. A 'turn' is one "
            "entry in the Turn timeline: each tool call contributes 1, plus 1 for the "
            "final reply when present. "
            "When the running total exceeds this, the orchestrator logs a one-shot "
            "warning and the report renders a badge — the run is NOT aborted "
            "(use max_turns for a hard cap). None disables the check."
        ),
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
            "(SDK-provided cost). Silently skipped if cost "
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
    count_cache_creation: bool = Field(
        default=False,
        description=(
            "When True, cache_creation_input_tokens count toward input/total "
            "budgets. Needed to budget Codex prompt input: the Codex agent buckets "
            "the fresh (full-price) prompt slice into cache_creation, so with this "
            "False a Codex token budget caps only output. Default False preserves "
            "existing behavior (Claude reports real cache-creation writes here)."
        ),
    )
    stop_early: bool = Field(
        default=False,
        description=(
            "Opt-in master switch for early-stop-on-criterion. When True, the run ends early "
            "once the armed criteria (those with stop_when set, incl. per-instance 'auto') "
            "are decided mid-run: pass-stop when the armed subset's weighted score is "
            "GUARANTEED to reach stop_early_gate_threshold regardless of any criterion still "
            "undecided, fail-stop when it is GUARANTEED it never can (deferred while any "
            "pass-armed criterion is undecided, so a misfire never truncates the recall "
            "signal) - so a raised max_turns is not wasted once the outcome is locked in. "
            "Default False keeps behavior identical. Requires a Claude single-shot task with "
            "at least one observable armed criterion; every unsupported combination is "
            "rejected at resolution time."
        ),
    )
    stop_early_gate_threshold: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum weighted score (Σ weight_i·score_i / Σ weight_i, over the ARMED subset "
            "only) required for an early-stopped run to gate as a pass. Also the bound "
            "early-stop's trigger checks against: a fail-stop fires once no combination of "
            "still-undecided armed criteria could raise the weighted score to this "
            "threshold; a pass-stop fires once it is already guaranteed to meet it regardless "
            "of what's still undecided. Default 1.0 reproduces the pre-weighting behavior "
            "exactly (every armed criterion's live-observable score is binary 0/1, so a "
            "weighted score of 1.0 requires every armed criterion to have actually passed) - "
            "lowering it lets a low-weight armed criterion's failure be absorbed without "
            "truncating the run, at the cost of the gate/trigger becoming a genuine weighted "
            "average rather than a strict AND."
        ),
    )

    @model_validator(mode="after")
    def _check_stop_early_gate_threshold(self) -> Self:
        """Reject a dead or degenerate ``stop_early_gate_threshold``.

        Two hard errors, matching the "never a silent no-op" posture the rest
        of early-stop resolution already holds itself to:

        - Non-default with ``stop_early: False``: the field is read only
          inside the orchestrator's ``early_stop is not None`` branch, so it
          has zero effect while ``stop_early`` is off — a silent no-op rather
          than the "unsupported combination is a hard error" contract.
        - ``<= 0.0`` with ``stop_early: True``: a threshold of exactly 0
          trivially satisfies both the pass-stop floor check and the final
          weighted gate regardless of whether any armed criterion has
          actually decided — a task author could neutralize the entire
          armed pass/fail gate with one YAML line (coder-eval is used as a
          CI gate). Kept as a validator rather than a stricter field bound
          because the floor depends on ``stop_early``.
        """
        if not self.stop_early and self.stop_early_gate_threshold != 1.0:
            raise ValueError(
                "run_limits.stop_early_gate_threshold is set but run_limits.stop_early is "
                + "False; the threshold has no effect unless stop_early is also True."
            )
        if self.stop_early and self.stop_early_gate_threshold <= 0.0:
            raise ValueError(
                "run_limits.stop_early_gate_threshold must be > 0.0 when stop_early is True "
                + "(a threshold of 0 trivially passes the armed gate regardless of whether any "
                + "armed criterion actually decided)."
            )
        return self
