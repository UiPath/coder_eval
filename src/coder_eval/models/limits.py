"""Run-time budget limits for task execution."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field


DEFAULT_STOP_EARLY_GATE_THRESHOLD: Final[float] = 1.0
"""Default ``stop_early_gate_threshold``: reproduces strict-AND gating exactly.

Single-sourced here so the field default below, the monitor's ``for_task``
fallback, and the orchestrator's finalize fallback can never drift apart.
"""


class RunLimits(BaseModel):
    """Run-time caps on a task.

    Unifies structural caps (max_tool_calls, task_timeout, turn_timeout) and
    budget caps (tokens, USD). The tool-call cap stops the task at the agent's
    next poll boundary and is cumulative across every turn. Budget caps are
    checked after each completed agent turn and are cumulative across all
    turns of a single task: a single-iteration task finishes and is then
    marked over budget, and a dialog stops after the turn that crossed the
    budget. Budgets apply to the subject agent only — judge and simulator
    token spend are not counted.

    Any subset of fields is valid; an empty block is legal.
    """

    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Hard cap on resolved tool calls across the whole task (every retry attempt and every "
            "dialog turn). Enforced by the TurnMonitor at the agent's next poll boundary on every "
            "harness: the round that reaches the cap is processed whole, so tool calls already in "
            "flight can still land after it. The run finalizes cleanly as tool_calls_exhausted; "
            "criteria are still checked. None = no cap."
        ),
    )
    expected_tool_calls: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Soft target for cumulative visible tool calls across a task (each resolved tool call "
            "counts 1, plus 1 for the final reply when present). Exceeding it logs a one-shot warning "
            "and badges the report; the run is NOT aborted (use max_tool_calls for a hard cap)."
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
    stop_early: bool | None = Field(
        default=None,
        description=(
            "Run-level early-stop KILL SWITCH — there is no run-level master arm. Arming is "
            "per-criterion: a live-observable criterion's stop_early: block alone activates "
            "the run's TurnMonitor. None (default): armed criteria decide; the run may "
            "end early once they resolve mid-run (pass-stop when the on_pass=stop subset's "
            "weighted score is GUARANTEED to reach stop_early_gate_threshold regardless of "
            "any criterion still undecided, fail-stop when the armed set's weighted score is "
            "GUARANTEED to never reach it — deferred while any pass-capable armed criterion "
            "is undecided, so a misfire never truncates the recall signal; a decide_within "
            "timeout latches an effective fail that feeds the same fail-stop rule). False: "
            "force-disarm every criterion's block for this run — the one-line experiment/"
            "variant override that turns a smoke flavor back into an authoritative, "
            "non-truncated run. True is rejected at resolution time (the master arm was "
            "removed; put a stop_early: block on the criterion instead). An armed run "
            "requires a single-shot task on a cooperative-stop agent; every unsupported "
            "combination is rejected at resolution time."
        ),
    )
    stop_early_gate_threshold: float = Field(
        default=DEFAULT_STOP_EARLY_GATE_THRESHOLD,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum weighted score (Σ weight_i·score_i / Σ weight_i, over the ARMED subset "
            "only) required for an EARLY-STOPPED run to gate as a pass (a run that completes "
            "naturally gates strict-AND over the full criteria set, armed or not). Also the bound "
            "early-stop's trigger checks against: a fail-stop fires once no combination of "
            "still-undecided armed criteria could raise the weighted score to this "
            "threshold (a decide_within timeout counts as a fail here); a pass-stop "
            "fires once the on_pass=stop subset is already guaranteed to meet it regardless "
            "of what's still undecided. Default 1.0 reproduces strict-AND behavior exactly "
            "(a weighted score of 1.0 requires every armed criterion to have actually "
            "passed) - lowering it lets a low-weight armed criterion's failure (or timeout) "
            "be absorbed without truncating the run, at the cost of the gate/trigger "
            "becoming a genuine weighted average rather than a strict AND."
        ),
    )

    # A degenerate gate threshold on an ARMED task, and the removed master arm, are
    # both rejected -- but in orchestration/early_stop.py::validate_early_stop, NOT
    # here. Whether a task is armed lives on the criteria, which RunLimits cannot
    # see, and post-merge is the only place with enough visibility.
    # Rationale: .claude/notes/orchestration.md § Why the guardrails are not model validators
