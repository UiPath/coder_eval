"""Run-time budget limits for task execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field


if TYPE_CHECKING:
    from coder_eval.models.telemetry import TokenUsage


DEFAULT_STOP_EARLY_GATE_THRESHOLD: Final[float] = 1.0
"""Default ``stop_early_gate_threshold``: reproduces strict-AND gating exactly.

Single-sourced here so the field default below, the monitor's ``for_task``
fallback, and the orchestrator's finalize fallback can never drift apart.
"""

_BUDGET_OVERSHOOT: Final[str] = (
    "Enforced live by the TurnMonitor; overshoot is soft by one usage report (see usage_granularity in "
    "docs/agents/HARNESS_PARITY.md) plus any calls in flight. None = unlimited."
)


class RunLimits(BaseModel):
    """Run-time caps on a task.

    Unifies structural caps (max_tool_calls, max_turns, task_timeout, turn_timeout) and
    budget caps (tokens, USD). Structural caps and budget caps stop the task at
    the agent's next poll boundary; both are cumulative across every turn of the
    task and apply to the subject agent only.

    Any subset of fields is valid; an empty block is legal.
    """

    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Hard cap on main-thread resolved tool calls across the whole task (every retry attempt and every "
            "dialog turn). Enforced by the TurnMonitor at the agent's next poll boundary on every "
            "harness: the round that reaches the cap is processed whole, so tool calls already in "
            "flight can still land after it. The run finalizes cleanly as tool_calls_exhausted; "
            "criteria are still checked. None = no cap."
        ),
    )
    max_turns: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Hard cap on main-thread model turns (model responses) across the whole task (every retry attempt "
            "and every dialog turn); a sub-agent's turns do not count. The TurnMonitor stops the agent at its "
            "next poll once turn N+1 starts, so part of that turn can still land. Only harnesses with a "
            "per-response turn boundary accept it (usage_granularity generation or step); the others reject it "
            "at resolution. The run finalizes cleanly as tool_calls_exhausted; criteria are still checked. "
            "None = no cap."
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
    expected_turns: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Soft target for cumulative main-thread model turns across a task, counted like max_turns. Exceeding "
            "it logs a one-shot warning and badges the report; the run is NOT aborted (use max_turns for a hard "
            "cap). Only harnesses with a per-response turn boundary accept it; the others reject it at resolution."
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
        description="Max cumulative input (prompt) tokens across the task. " + _BUDGET_OVERSHOOT,
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Max cumulative output (completion) tokens across the task. " + _BUDGET_OVERSHOOT,
    )
    max_total_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Max cumulative input+output tokens across the task. " + _BUDGET_OVERSHOOT,
    )
    max_usd: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Max cumulative cost in USD across the task. Enforced live by the TurnMonitor: priced from the "
            "harness's reported cost when it reports one, else from pricing.py for the reported model or "
            "agent.model. On a harness that does not report its own cost (reports_cost in "
            "docs/agents/HARNESS_PARITY.md), agent.model must be priced, or the task is rejected at resolution "
            "(register_pricing adds a plugin rate). A run that still cannot price a turn finishes ERROR. "
            "Overshoot is soft by one usage report (see usage_granularity) plus any calls in flight."
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

    def budgeted_tokens(self, usage: TokenUsage) -> tuple[int, int, int]:
        """(input, output, total) as this block counts them: cache buckets join input only when flagged."""
        input_tokens = usage.uncached_input_tokens
        if self.count_cache_creation:
            input_tokens += usage.cache_creation_input_tokens
        if self.count_cached_input:
            input_tokens += usage.cache_read_input_tokens
        return input_tokens, usage.output_tokens, input_tokens + usage.output_tokens
