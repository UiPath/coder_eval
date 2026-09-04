"""Evaluation results and execution record models."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    computed_field,
    model_validator,
)

from coder_eval.models.agent_config import ResolvedAgentConfig
from coder_eval.models.criteria import SuccessCriterion
from coder_eval.models.enums import FinalStatus
from coder_eval.models.limits import DEFAULT_STOP_EARLY_GATE_THRESHOLD
from coder_eval.models.telemetry import (
    CommandStatistics,
    CommandTelemetry,
    ProviderCallCost,
    TokenUsage,
    TranscriptMessage,
)


class ConfigLineageEntry(BaseModel):
    """Records which config layer provided a specific value."""

    value: Any
    source: Literal[
        "default",
        "task",
        "experiment-defaults",
        "variant",
        "cli",
        "mutation",
    ]
    source_detail: str | None = None


class TaskConfigRecord(BaseModel):
    """Full task configuration snapshot stored in per-task output."""

    resolved: dict[str, Any] = Field(description="Full resolved TaskDefinition as dict")
    source_yaml: str = Field(description="Raw YAML text from the task file")
    source_file: str | None = Field(default=None, description="Path to the original task YAML")
    lineage: dict[str, ConfigLineageEntry] = Field(default_factory=dict, description="Dotted-path → source layer")


class CriterionResult(BaseModel):
    """Result of checking a single success criterion.

    ``extra="allow"`` lets subclass-specific fields (e.g. ``observed_label`` from
    ``ClassificationCriterionResult``, ``analysis`` / ``transcript`` from
    ``JudgeCriterionResult``) round-trip through ``EvaluationResult.model_dump`` →
    ``model_validate_json``. Without this, the typed list ``list[CriterionResult]``
    silently strips fields that aren't on the base class when reading task.json
    back — losing the audit data that's the whole point of the verbose verdict.
    """

    model_config = ConfigDict(extra="allow")

    result_kind: Literal["basic"] = Field(
        default="basic",
        description=(
            "Discriminator for ``CriterionResultUnion`` so subclasses round-trip through "
            "``model_dump_json`` → ``model_validate_json`` with their concrete type preserved."
        ),
    )
    criterion_type: str = Field(description="Type of criterion")
    description: str = Field(description="Description of what was checked")
    score: float = Field(
        ge=0.0, le=1.0, description="Continuous score from 0.0 (complete failure) to 1.0 (perfect success)"
    )
    details: str | None = Field(default=None, description="Additional details about the result")
    error: str | None = Field(default=None, description="Error message if the check failed")
    evaluation_status: Literal["evaluated", "not_evaluated"] = Field(
        default="evaluated",
        description=(
            "Whether the criterion ran. ``not_evaluated`` is distinct from an evaluated "
            "criterion whose score is 0.0 or whose checker returned an error. Defaults to "
            "``evaluated`` so task.json files written before this field existed retain their "
            "original meaning."
        ),
    )
    pass_threshold: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Score required to pass this criterion (mirrors BaseSuccessCriterion.pass_threshold).",
    )
    gating: bool = Field(
        default=True,
        description=(
            "Whether a below-threshold score here fails the task (mirrors "
            "BaseSuccessCriterion.is_gating, i.e. weight > 0). False marks an informational "
            "criterion: it is measured and reported but excluded from the score and the "
            "pass/fail gate, so every display surface must render it as informational rather "
            "than failed. Defaults True so results persisted before this field existed — and "
            "any result built without a source criterion — read back as gating."
        ),
    )


class ClassificationCriterionResult(CriterionResult):
    """Per-row result for classification criteria.

    Carries the observed and expected labels alongside the standard score so
    the suite-level aggregator can compute P/R/F1 and a confusion matrix.
    Keeping these fields on a subclass (rather than the base) means results
    for non-classification criteria don't carry dead label fields.
    """

    result_kind: Literal["classification"] = "classification"  # type: ignore[assignment]
    observed_label: str = Field(
        description="Observed label emitted by the criterion (sentinels like '(none)' / '(other)' allowed)."
    )
    expected_label: str = Field(description="Ground-truth label threaded through from the task / dataset row.")


class JudgeTranscriptToolCall(BaseModel):
    """One tool invocation by an ``agent_judge`` sub-agent — the audit trail for one step.

    Sourced from ``CommandTelemetry`` and reduced to the fields a reviewer needs
    to verify the judge's claims (tool, target, exit status). All free-form
    string fields go through ``scrub_reference`` before persistence.
    """

    tool_name: str = Field(description="SDK tool name (Bash, Read, Grep, ...).")
    detail: str = Field(
        default="",
        description="Per-tool target (Bash command, file path, grep pattern, ...). Truncated and scrubbed.",
    )
    status: str = Field(default="unknown", description="success / error / unknown — copied from telemetry.")
    result_preview: str = Field(
        default="",
        description="First ~200 chars of the tool result, truncated and scrubbed.",
    )


class JudgeTranscript(BaseModel):
    """Captured trajectory of a judge sub-agent — written into ``JudgeCriterionResult``.

    Populated by ``agent_judge`` from the returned ``TurnRecord``. ``llm_judge`` is
    one-shot so its transcript only carries ``raw_verdict`` (the model's response),
    ``token_usage``, and the rendered prompts; ``tool_calls`` is empty there.

    ``judge_prompt`` and ``judge_system_prompt`` capture the exact rendered envelope
    sent to the judge so reviewers can answer "why did the judge say X?" by replaying
    the input post-hoc. Both go through ``scrub_reference`` before persistence so a
    reference solution embedded via ``include_reference=true`` doesn't leak.
    """

    tool_calls: list[JudgeTranscriptToolCall] = Field(
        default_factory=list,
        description="Ordered tool invocations made by the judge during evaluation.",
    )
    token_usage: TokenUsage | None = Field(default=None, description="Token usage for the judge's turn.")
    duration_seconds: float = Field(default=0.0, description="Wall-clock duration of the judge's turn.")
    raw_verdict: str = Field(
        default="",
        description="Scrubbed + truncated raw text of the judge's final assistant message.",
    )
    judge_system_prompt: str = Field(
        default="",
        description="Scrubbed + truncated system prompt the judge was started with (constant per criterion config).",
    )
    judge_prompt: str = Field(
        default="",
        description=(
            "Scrubbed + truncated rendered user message — the rubric + reference + artifacts + "
            "dialog blocks the judge actually saw. Variable per row."
        ),
    )
    truncated: bool = Field(
        default=False,
        description="True when the captured transcript was clipped to fit ``max_transcript_chars``.",
    )


class JudgeCriterionResult(CriterionResult):
    """Per-row result for judge criteria (``llm_judge`` / ``agent_judge``).

    Carries ``findings`` (bullet evidence the judge cited from the artifacts)
    and an optional ``transcript`` so reviewers can audit the judge's verdict
    instead of having to trust the one-line rationale alone. Subclass (rather
    than base fields) keeps non-judge results lean.
    """

    result_kind: Literal["judge"] = "judge"  # type: ignore[assignment]
    findings: list[str] = Field(
        default_factory=list,
        description="Bullet observations the judge cited from the artifacts. Scrubbed before persistence.",
    )
    token_usage: TokenUsage | None = Field(
        default=None,
        description=(
            "Token usage for the judge's LLM call(s) — kept distinct from the "
            "main agent's ``EvaluationResult.total_token_usage``. Populated on "
            "all routes when the model reports usage: on DirectRoute (Anthropic) "
            "it comes from the Anthropic response ``usage``; on Bedrock from the "
            "``/invoke`` JSON ``usage``. ``None`` means the backend surfaced no usage (kept distinct from a "
            "zero TokenUsage). Independent of ``capture_transcript``."
        ),
    )
    transcript: JudgeTranscript | None = Field(
        default=None,
        description=(
            "Captured judge trajectory (tool calls + token usage + raw verdict). "
            "None when the criterion sets ``capture_transcript=False`` or "
            "when the judge errored before producing a turn. Held in memory "
            "during the run for HTML rendering; excluded from ``task.json`` "
            "via ``model_dump_json(exclude=...)`` so on-disk records carry "
            "only ``transcript_path`` to a sibling file."
        ),
    )
    transcript_path: str | None = Field(
        default=None,
        description=(
            "Filename of the sibling YAML file holding this result's full transcript "
            "(``judge-N.yaml`` for canonical results or ``post-failure-judge-N.yaml`` "
            "for diagnostic results), relative to the directory containing ``task.json``. "
            "Set by ``spill_judge_transcripts`` after the run; reloaded by "
            "``load_judge_transcripts`` for re-rendering. None when no transcript was captured."
        ),
    )


# Criterion types whose results are ``JudgeCriterionResult`` / ``ClassificationCriterionResult``.
# Used by ``_criterion_result_discriminator`` to type-infer legacy ``task.json`` records that
# lack the ``result_kind`` field. Listed here (not on each criterion class) because
# ``model_validate_json`` runs without the criteria registry loaded — consumers that import
# ``coder_eval.models`` to deserialize ``task.json`` (e.g. ``coder-eval report``) do NOT
# import ``coder_eval.criteria.*`` (which is where checkers self-register). Putting the
# inference rule on criterion classes would force importing every checker just to deserialize
# a row record, and would create a circular dependency since ``criteria/*.py`` already imports
# from this module.
#
# Convention: when a new criterion type needs a non-``"basic"`` result class, add its
# ``criterion_type`` value to the matching frozenset in the same PR that introduces the
# criterion.
_JUDGE_CRITERION_TYPES = frozenset({"llm_judge", "agent_judge"})
_CLASSIFICATION_CRITERION_TYPES = frozenset({"classification_match", "skill_triggered"})


def _criterion_result_discriminator(v: Any) -> str:
    """Dispatch ``CriterionResult`` subclasses for serialization and validation.

    Prefers explicit ``result_kind`` (written by current code); falls back to
    inference from ``criterion_type`` for legacy task.json files written before
    this field existed. Unknown criterion_types fall through to ``"basic"``.

    Explicit ``result_kind`` wins over ``criterion_type`` inference: a dict
    carrying ``result_kind="basic"`` with ``criterion_type="llm_judge"`` is
    routed to the base class. This matters for forward-compat experiments where
    a writer deliberately downgrades a result shape.
    """
    if isinstance(v, dict):
        kind = v.get("result_kind")
        if kind:
            return str(kind)
        ct = v.get("criterion_type", "")
        if ct in _JUDGE_CRITERION_TYPES:
            return "judge"
        if ct in _CLASSIFICATION_CRITERION_TYPES:
            return "classification"
        return "basic"
    # Pydantic instance — read the discriminator field.
    return getattr(v, "result_kind", "basic")


CriterionResultUnion = Annotated[
    (
        Annotated[CriterionResult, Tag("basic")]
        | Annotated[JudgeCriterionResult, Tag("judge")]
        | Annotated[ClassificationCriterionResult, Tag("classification")]
    ),
    Discriminator(_criterion_result_discriminator),
]


class ResultSummary(BaseModel):
    """Diagnostic fields lifted from the SDK's final ResultMessage.

    Powers the agent's debug log and the error-path formatter that
    surfaces a useful detail string when the CLI crashes. Persisted on
    ``TurnRecord`` for clean turns only — on a crash the agent raises
    before the TurnRecord is constructed, so post-mortem persistence on
    error turns is out of scope here.

    Mirrors the diagnostic-bearing subset of
    ``claude_agent_sdk.ResultMessage``; pure accounting fields
    (``num_turns``, ``duration_ms``) live on ``TurnRecord`` /
    ``TokenUsage``.
    """

    is_error: bool = Field(description="Whether the SDK reported the turn as errored")
    subtype: str = Field(description="Coarse classification (e.g. 'success', 'error_during_execution')")
    stop_reason: str | None = Field(default=None, description="Why the model stopped, if reported")
    result: str | None = Field(default=None, description="Free-form result/error text from the SDK")


class TurnRecord(BaseModel):
    """Record of a single agent turn (input + output)."""

    iteration: int = Field(
        description=(
            "Orchestrator iteration number. Multiple records may share this value when "
            "crashed=True partials precede a retry; use the list index in "
            "EvaluationResult.iterations as the canonical unique key."
        )
    )
    user_input: str = Field(description="Input prompt to the agent")
    agent_output: str = Field(description="Agent's response (legacy format)")
    commands: list[CommandTelemetry] = Field(
        default_factory=list, description="Detailed telemetry for each command executed during this turn"
    )
    timestamp: datetime = Field(default_factory=datetime.now, description="When this turn occurred")
    duration_seconds: float = Field(default=0.0, description="How long this turn took")
    token_usage: TokenUsage | None = Field(
        default=None, description="Token usage for this turn (if available from agent SDK)"
    )
    model_used: str | None = Field(
        default=None, description="Model identifier used for this turn (e.g., 'claude-sonnet-4-5-20250514')"
    )
    assistant_turn_count: int = Field(
        default=0,
        description="Number of AssistantMessage objects received from the SDK in this turn.",
    )
    messages: list[TranscriptMessage] = Field(
        default_factory=list,
        description=(
            "Per-message telemetry in emission order, mirroring the LLM API messages array. "
            "Includes UserMessage entries (simulator text or tool results), AssistantMessage entries "
            "(agent thinking/tool_use/text blocks), and an optional terminal ReconciliationMessage "
            "carrying tokens the agent billed but never surfaced as a generation (so the transcript's "
            "token buckets sum to token_usage). Preserves the full conversation trajectory for "
            "replay and analysis. May be empty for agents/modes that don't surface message detail."
        ),
    )
    num_turns: int | None = Field(
        default=None,
        description=(
            "Number of inner-loop turns the SDK reported for this communicate() call "
            "(from ResultMessage.num_turns). None when the SDK did not emit a "
            "ResultMessage (e.g. crash partial before the final message arrived)."
        ),
    )
    max_turns_exhausted: bool = Field(
        default=False,
        description="Whether the agent hit the max_turns limit without voluntarily completing",
    )
    result_summary: ResultSummary | None = Field(
        default=None,
        description="SDK ResultMessage summary, when one was emitted (clean turns or partials that got one).",
    )
    provider_call_costs: list[ProviderCallCost] = Field(
        default_factory=list,
        description=(
            "Per-call ACTUAL cost + cache captured proxy-side for the open-weight (LiteLLM) backend, "
            "joined onto this turn by litellm_cost.apply_actual_cost. Empty on every other backend "
            "(the SDK reports cost/cache natively there). When present, total_cost_usd is the sum of "
            "these calls' cost_usd (the real bill), not the static rate-card estimate."
        ),
    )
    crashed: bool = Field(
        default=False,
        description=(
            "True if the agent failed mid-turn. Pre-failure commands/files/output are real work "
            "and are counted by aggregators; the retry's API call is billed separately."
        ),
    )
    crash_reason: str | None = Field(
        default=None,
        description="Short human-readable cause when crashed=True; None otherwise.",
    )


class PostRunResult(BaseModel):
    """Result of executing a single post-run command."""

    command: str = Field(description="Command as specified in the task definition")
    exit_code: int | None = Field(default=None, description="Process exit code (None if timed out or failed to start)")
    stdout: str = Field(default="", description="Captured stdout")
    stderr: str = Field(default="", description="Captured stderr")
    duration_seconds: float = Field(default=0.0, description="Execution duration in seconds")
    error: str | None = Field(default=None, description="Error message if the command failed to execute")


class SimulationTelemetry(BaseModel):
    """Per-trial simulation telemetry.

    Populated only when ``TaskDefinition.simulation.enabled`` is True. One
    ``EvaluationResult`` corresponds to one dialog trajectory, so this record
    is also per-dialog.
    """

    model_config = {"extra": "forbid"}

    n_trials: int = Field(description="Total number of trials the task was expanded into", ge=1)
    replicate_index: int = Field(description="Zero-indexed trial number within this (task, variant)", ge=0)
    stop_reason: Literal[
        "criteria_passed",
        "stop_token",
        "max_turns",
        "budget",
        "error",
        "run_limit_exceeded",
    ] = Field(description="Why the dialog terminated")
    simulator_input_tokens: int = Field(default=0, ge=0, description="Sum of simulator prompt tokens across turns")
    simulator_output_tokens: int = Field(default=0, ge=0, description="Sum of simulator completion tokens across turns")
    simulator_failures: int = Field(default=0, ge=0, description="Number of simulator LLM calls that raised")
    total_turns: int = Field(description="Number of user↔agent exchanges completed in this dialog", ge=0)
    simulator_model: str | None = Field(
        default=None,
        description=(
            "Resolved model that played the simulated user, captured so a persisted "
            "task.json is self-describing and its cost prices from a fact rather than "
            "from the run's route. None on records written before the model was pinned."
        ),
    )


class EarlyStopReason(StrEnum):
    """Why an early-stop-on-criterion run was cut short.

    Lives in ``coder_eval.models`` (not ``simulation/``) because
    ``EvaluationResult`` carries it and ``models/`` is a leaf package that
    cannot import from ``simulation``. ``DialogStopReason`` is a stylistic
    reference only. Early-stop is orthogonal telemetry, NOT a ``FinalStatus`` —
    the terminal-status set stays closed, and every reason gates identically
    through ``armed_criteria_passed``'s weighted gate.
    ``DECISION_BUDGET_EXCEEDED`` is a reporting label only: it marks a
    fail-stop whose deciding criterion timed out undecided past its
    ``stop_early.decide_within`` (an *effective* fail latched by the watcher)
    rather than live-failing natively.
    """

    CRITERION_PASSED = "criterion_passed"
    CRITERION_FAILED = "criterion_failed"
    DECISION_BUDGET_EXCEEDED = "decision_budget_exceeded"


class EarlyStopInfo(BaseModel):
    """Records why and when a run stopped early (``None`` when it ran to completion).

    Populated by the orchestrator's ``EarlyStopWatcher`` at the moment the armed
    criteria are decided mid-run. ``early_stop is not None`` is itself the
    "stopped early" flag — no separate bool. Serialized as part of
    ``EvaluationResult`` to ``task.json``; defaults to ``None`` on old files, so
    the round-trip is safe.
    """

    reason: EarlyStopReason = Field(
        description="Why the run stopped: the pass-stop floor locked in (criterion_passed), an armed "
        + "criterion live-failed natively (criterion_failed), or an armed criterion's decision-step "
        + "budget (stop_early.decide_within) expired while still undecided — an effective fail "
        + "(decision_budget_exceeded). All reasons gate identically through the weighted armed gate."
    )
    deciding_criterion_type: str = Field(
        description="Type of the criterion whose live verdict fired the stop (the failing one on "
        + "fail-stop; the last-to-pass on pass-stop)."
    )
    deciding_criterion_description: str = Field(
        description="Description of the deciding criterion (human-readable label from the task YAML)."
    )
    armed_criteria: list[str] = Field(
        default_factory=list,
        description="'type: description' strings for the armed set, so reports can mark the rest "
        + "advisory without re-deriving from task_config.",
    )
    sdk_turn_index: int = Field(
        description="SDK inner-turn count at the stop (watcher counts TurnStartEvents). NOT the "
        + "orchestrator iteration, which is always 1 in single-shot."
    )
    tool_call_index: int = Field(
        description="1-based index of the tool call that decided the stop. NOTE: because the "
        + "stop latches on the tool CALL (ToolStartEvent), this is the index of the deciding "
        + "call INCLUDING that in-flight call — i.e. completed_tool_ends + 1 for a call-latched "
        + "stop. Read it as 'which call decided', not as a count of fully-completed tool calls."
    )
    elapsed_seconds: float = Field(description="Wall-clock seconds from the first agent-start event to the stop.")
    turns_remaining_at_stop: int | None = Field(
        default=None,
        description="max_turns - sdk_turn_index (an upper bound on turns avoided, not a measured "
        + "saving); None when max_turns is unset.",
    )
    gate_threshold: float = Field(
        default=DEFAULT_STOP_EARLY_GATE_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="run_limits.stop_early_gate_threshold in effect for this stop — captured so a "
        + "persisted task.json is self-describing (e.g. comparing early-stopped runs across an "
        + "experiment sweep that varies the threshold) without needing the resolved task config. "
        + "Bounded to mirror the source field so a hand-edited or externally produced record "
        + "cannot represent a value the authoritative field would reject.",
    )


class EvaluationResult(BaseModel):
    """Complete result of a task evaluation."""

    task_id: str = Field(description="ID of the evaluated task")
    task_description: str = Field(description="Description of the task")
    variant_id: str = Field(default="default", description="ID of the experiment variant")
    agent_type: str = Field(description="Type of agent used (registered kind string, e.g. 'claude-code')")
    model_used: str | None = Field(
        default=None, description="Model identifier used for the evaluation (resolved from iterations or agent config)"
    )

    # Execution metadata
    started_at: datetime = Field(description="When evaluation started")
    completed_at: datetime | None = Field(default=None, description="When evaluation completed")
    duration_seconds: float = Field(default=0.0, description="Total evaluation duration")

    # Results
    final_status: FinalStatus = Field(description="Final status of the evaluation")
    max_turns_exhausted: bool = Field(
        default=False,
        description="Whether any iteration hit the agent max_turns limit without the agent voluntarily completing",
    )
    weighted_score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Weighted average of criterion scores (0.0 to 1.0)"
    )
    iteration_count: int = Field(description="Number of iterations completed")
    success_criteria_results: list[CriterionResultUnion] = Field(
        default_factory=list,
        description=(
            "Results of all success criteria checks. Typed as the ``CriterionResultUnion`` "
            "discriminated union so subclass-specific fields (``JudgeCriterionResult.findings`` / "
            "``transcript``, ``ClassificationCriterionResult.observed_label``) round-trip "
            "concretely through ``model_dump_json`` → ``model_validate_json``. Legacy task.json "
            "files without ``result_kind`` are inferred from ``criterion_type``."
        ),
    )
    post_failure_criteria_results: list[CriterionResultUnion] = Field(
        default_factory=list,
        description=(
            "Diagnostic criterion evidence collected after a terminal agent failure while the "
            "sandbox is still readable. These results are intentionally separate from "
            "success_criteria_results: they do not affect weighted_score, task gating, or suite "
            "aggregation. A result with evaluation_status='not_evaluated' records that its "
            "required inputs or remaining task-timeout budget were unavailable."
        ),
    )

    # Detailed transcript
    iterations: list[TurnRecord] = Field(
        default_factory=list,
        description="Complete transcript of agent interactions across all iterations (task attempts)",
        validation_alias=AliasChoices("iterations", "turns"),
    )

    # Error information
    error_message: str | None = Field(default=None, description="Error message if evaluation failed")
    error_details: dict[str, Any] | None = Field(
        default=None, description="Detailed error context from error_handling module"
    )
    error_log_tail: str | None = Field(
        default=None,
        description=(
            "Sanitised tail of task.log captured during the run, populated for failure "
            "statuses (ERROR, BUILD_FAILED, TIMEOUT, FAILURE, and the budget-exceeded "
            "statuses). For BUILD_FAILED it carries the `docker build` log tail. "
            "Used by the HTML report's Logs disclosure."
        ),
    )

    # Environment information
    environment_info: dict[str, Any] = Field(
        default_factory=dict, description="Version information and environment details"
    )

    # Agent configuration. ResolvedAgentConfig (base-typed + SerializeAsAny + registry
    # coercion) so a plugin kind's subclass-only fields survive the dump to task.json
    # and reload — the same round-trip guarantee TaskDefinition.agent has.
    agent_config: ResolvedAgentConfig | None = Field(
        default=None,
        description="Agent configuration used for the evaluation (from task YAML)",
    )

    # SDK options (raw dump of all ClaudeAgentOptions fields including defaults)
    sdk_options: dict[str, Any] | None = Field(
        default=None,
        description="Raw SDK options dump from ClaudeAgentOptions (all fields including defaults)",
    )

    # Artifacts
    sandbox_path: str | None = Field(default=None, description="Path to preserved sandbox (if saved)")

    # Full task configuration (resolved config + source YAML + lineage)
    task_config: TaskConfigRecord | None = Field(default=None, description="Full task configuration snapshot")

    # Command telemetry
    command_stats: CommandStatistics | None = Field(default=None, description="Aggregated command telemetry statistics")

    # Token usage
    total_token_usage: TokenUsage | None = Field(default=None, description="Aggregated token usage across all turns")

    # Assistant turns
    total_assistant_turns: int | None = Field(
        default=None,
        description="Total assistant turns across all orchestrator iterations",
    )

    # Commands efficiency (orchestrator-level tracking)
    expected_commands: int | None = Field(default=None, description="Expected commands from task definition")
    actual_commands: int | None = Field(default=None, description="Actual tool commands executed by the agent")
    commands_efficiency: float | None = Field(
        default=None, description="Commands efficiency score (0-1). expected/max(actual, expected)"
    )

    # Pre-run script results
    pre_run_results: list[PostRunResult] = Field(
        default_factory=list,
        description=(
            "Results of pre-run scripts. "
            "A failed command with fail_on_error=True aborts evaluation; the result is still captured here."
        ),
    )

    # Post-run script results
    post_run_results: list[PostRunResult] = Field(
        default_factory=list, description="Results of post-run scripts (informational, do not affect pass/fail)"
    )

    # Simulation telemetry (only populated when task.simulation.enabled is True)
    simulation: SimulationTelemetry | None = Field(
        default=None,
        description=(
            "Dialog-simulation telemetry: trial index, stop reason, simulator token usage. None in single-shot mode."
        ),
    )

    # Early-stop telemetry (only populated when the run was cut short by the
    # armed-criteria watcher; None on a full run). See EarlyStopInfo.
    early_stop: EarlyStopInfo | None = Field(
        default=None,
        description=(
            "Why/when the run stopped early (reason, deciding criterion, SDK turn/tool index, elapsed). "
            "None on a full run — 'early_stop is not None' is itself the stopped-early flag."
        ),
    )

    def calculate_weighted_score(self, criteria: list[SuccessCriterion]) -> None:
        """Calculate weighted average score from criterion results.

        Args:
            criteria: Original criterion definitions with weights

        This method mutates self.weighted_score.
        """
        if not self.success_criteria_results or not criteria:
            self.weighted_score = 0.0
            return

        if len(self.success_criteria_results) != len(criteria):
            raise ValueError(
                f"Results/criteria length mismatch: {len(self.success_criteria_results)} results "
                + f"vs {len(criteria)} criteria for task {self.task_id}. This indicates an upstream "
                + "bug; refusing to fabricate a weight-ignoring score."
            )

        total_weighted_score = 0.0
        total_weight = 0.0

        for result, criterion in zip(self.success_criteria_results, criteria, strict=True):
            total_weighted_score += result.score * criterion.weight
            total_weight += criterion.weight

        self.weighted_score = total_weighted_score / total_weight if total_weight > 0 else 0.0

    def all_criteria_passed(self, criteria: list[SuccessCriterion]) -> bool:
        """True iff every GATING criterion result meets its pass_threshold.

        Single source of truth for the success gate. ``weight: 0`` criteria are
        informational (``BaseSuccessCriterion.is_gating`` is False): they are
        excluded from the weighted score, so they are excluded from the gate
        too — otherwise a criterion that contributes nothing to the score could
        still single-handedly flip the task to FAILURE. A task whose criteria
        are ALL weight-0 has an empty gate and therefore passes.

        A results/criteria length mismatch raises ``ValueError`` rather than
        silently truncating — the ``len()`` pre-check is required because
        ``all()`` short-circuits on the first failing pair, so
        ``zip(strict=True)`` alone would not reliably reach the length check.
        """
        if len(self.success_criteria_results) != len(criteria):
            raise ValueError(
                f"Results/criteria length mismatch for task {self.task_id}: "
                + f"{len(self.success_criteria_results)} results vs {len(criteria)} criteria."
            )
        return all(
            r.score >= c.pass_threshold
            for r, c in zip(self.success_criteria_results, criteria, strict=True)
            if c.is_gating
        )

    def armed_criteria_passed(
        self, criteria: list[SuccessCriterion], gate_threshold: float = DEFAULT_STOP_EARLY_GATE_THRESHOLD
    ) -> bool:
        """True iff the ARMED subset's weighted score meets ``gate_threshold``.

        The early-stop gate: on an early-stopped run only the armed subset gates
        ``final_status`` (non-armed criteria are advisory — recorded but never
        decisive), so a smoke flavor is not dragged to FAILURE by criteria whose
        work it deliberately skipped. Shares the same results/criteria length
        pre-check as ``all_criteria_passed`` so the gate logic stays
        single-sourced. Raises ``ValueError`` on an empty armed set — unreachable
        when a stop actually fired (a stop requires an armed criterion), so this
        is a defensive guard against misuse. No ``is_gating`` filter is needed
        here: ``BaseSuccessCriterion`` rejects ``weight: 0`` together with any
        early-stop trigger, so every armed criterion is gating by construction.

        Each armed criterion's OWN ``pass_threshold`` still decides whether it
        individually passed — ``r.score`` is converted to a binary 1.0/0.0 via
        ``r.score >= c.pass_threshold`` before weighting, exactly mirroring
        ``all_criteria_passed``'s per-criterion comparison. Only the
        combination rule changes: ``all_criteria_passed`` ANDs those binary
        outcomes, this weights and averages them against ``gate_threshold``.
        This is what makes the ``gate_threshold=1.0`` default an EXACT
        equivalence with the pre-weighting ``all(...)`` rule, not merely an
        approximation that happens to hold for binary-scoring criteria: a
        weighted average of 1.0 requires every armed criterion's binary
        outcome to be 1.0, i.e. every one to have individually passed its own
        ``pass_threshold`` — identical to ``all(...)`` regardless of what
        ``r.score`` itself was. Callers pass
        ``run_limits.stop_early_gate_threshold`` to opt into a genuine
        weighted average below 1.0.
        """
        if len(self.success_criteria_results) != len(criteria):
            raise ValueError(
                f"Results/criteria length mismatch for task {self.task_id}: "
                + f"{len(self.success_criteria_results)} results vs {len(criteria)} criteria."
            )
        armed = [(r, c) for r, c in zip(self.success_criteria_results, criteria, strict=True) if c.is_stop_armed]
        if not armed:
            raise ValueError(
                f"armed_criteria_passed called with no armed criteria for task {self.task_id}; "
                + "the early-stop gate is only valid when at least one criterion sets a stop trigger."
            )
        total_weight = sum(c.weight for _, c in armed)
        if total_weight <= 0.0:
            # Unreachable today (weight=0 + a stop trigger is rejected at the model
            # layer, so every armed criterion carries weight > 0) — but a
            # defensive guard on a pass/fail gate must fail CLOSED, not open,
            # against a future criterion subclass that bypasses that
            # validator. Mirrors EarlyStopWatcher._ceiling's lack of an
            # equivalent guard: that one would raise ZeroDivisionError instead
            # (fails by crashing, not by silently passing) rather than diverge
            # toward a false pass.
            return False
        weighted_score = sum((1.0 if r.score >= c.pass_threshold else 0.0) * c.weight for r, c in armed) / total_weight
        return weighted_score >= gate_threshold


class CriterionStats(BaseModel):
    """Per-criterion-type aggregate stats across a suite's rows."""

    criterion_type: str = Field(description="Criterion type (e.g., 'file_exists')")
    rows_evaluated: int = Field(description="Rows where this criterion appeared at least once")
    average_score: float = Field(ge=0.0, le=1.0, description="Mean score across all evaluations of this criterion")
    error_count: int = Field(default=0, description="Evaluations that surfaced a checker-level error")


class FailedRowSummary(BaseModel):
    """Per-row summary for a row that did not succeed — used for error-sample rendering."""

    row_id: str | None = Field(description="Row id within the suite (None if unavailable)")
    task_id: str = Field(description="Full task id (suite_id/row_id)")
    final_status: FinalStatus = Field(description="Final status of this row")
    weighted_score: float | None = Field(default=None, description="Weighted score (None on error)")
    failure_reasons: list[str] = Field(
        default_factory=list,
        description="Short descriptions of failed criteria (up to a few, truncated per row).",
    )
    error_message: str | None = Field(default=None, description="Top-level error message if the row errored out.")
    task_json_relpath: str = Field(
        description="Path to the row's task.json, relative to run_dir (<variant>/<task_id>/<NN>/task.json)."
    )
    replicate_index: int = Field(
        default=0,
        ge=0,
        description="Replicate index of this failed row (0 when repeats disabled).",
    )


class ClassLabelStats(BaseModel):
    """Per-class precision / recall / F1. Used by classification_match's aggregate details."""

    label: str = Field(description="Class label (may be a sentinel like '(none)' / '(other)')")
    precision: float = Field(ge=0.0, le=1.0, description="TP / (TP + FP); 0.0 when denom is 0")
    recall: float = Field(ge=0.0, le=1.0, description="TP / (TP + FN); 0.0 when denom is 0")
    f1: float = Field(ge=0.0, le=1.0, description="Harmonic mean of precision and recall; 0.0 when both are 0")
    support: int = Field(ge=0, description="Rows where expected_label == this class")


class ConfusionEntry(BaseModel):
    """A single cell of the confusion matrix. Used by classification_match's aggregate details."""

    expected: str
    observed: str
    count: int = Field(ge=0)


class ThresholdCheck(BaseModel):
    """Pass/fail for a single {metric: min_value} threshold at suite level."""

    metric: str = Field(description="Metric name as emitted by the criterion's aggregate() output.")
    min_value: float = Field(description="Configured minimum. The check passes when actual_value >= min_value.")
    actual_value: float | None = Field(
        default=None,
        description="Observed metric value. None when the aggregator did not emit this metric.",
    )
    passed: bool


class CriterionAggregate(BaseModel):
    """Across-row aggregate produced by a criterion's aggregate() method.

    Each success_criterion on a dataset-backed task can emit one of these. The
    ``metrics`` dict holds flat named values (e.g. ``'accuracy'``, ``'f1.macro'``,
    ``'recall.positive'``) that ``suite_thresholds`` on the criterion reference.
    ``details`` carries shape-specific extras for rendering — for
    ``classification_match`` that's labels, per-label stats, and the confusion
    matrix.
    """

    criterion_type: str
    description: str | None = Field(
        default=None,
        description=(
            "The source criterion's description. Set when a task stacks multiple criteria of the "
            "same type (e.g. activation's per-skill skill_triggered criteria) so each aggregate is "
            "distinguishable in the rollup; None for single-criterion-per-type suites."
        ),
    )
    rows_total: int = Field(
        default=0,
        ge=0,
        description=(
            "Total rows in the suite for this variant — the denominator a reader expects. 0 when not "
            "computed at suite level (e.g. a criterion-level aggregate() called outside a rollup)."
        ),
    )
    rows_excluded: int = Field(
        default=0,
        ge=0,
        description=(
            "Rows dropped from this aggregate's per-row slice because they produced no result at this "
            "criterion's position (e.g. ERROR rows landing before criteria ran). The classification/score "
            "denominator is rows_total - rows_excluded."
        ),
    )
    metrics: dict[str, float] = Field(default_factory=dict, description="Flat metric name -> value")
    threshold_checks: list[ThresholdCheck] = Field(default_factory=list)
    passed: bool = Field(description="True when all threshold_checks passed (trivially true when no thresholds).")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Criterion-specific structured data used for markdown rendering (e.g. confusion matrix).",
    )
    error: str | None = Field(
        default=None,
        description="Populated when the criterion declared suite_thresholds but aggregate() returned nothing.",
    )


class SuiteRollup(BaseModel):
    """Pass-rate rollup for a dataset-backed suite under a single variant.

    Written to ``<run_dir>/<variant_id>/<suite_id>/suite.json`` and
    ``suite.md`` next to the per-row directories. Presence is gated on
    the task having been expanded from a Dataset.
    """

    suite_id: str
    variant_id: str
    rows_total: int
    rows_passed: int
    rows_failed: int
    rows_error: int
    # The fourth bucket, matching RunSummary.tasks_not_graded and
    # VariantAggregate.tasks_not_graded. Defaulted so a suite.json written before
    # `execute` existed still parses.
    rows_not_graded: int = Field(default=0, ge=0)
    pass_rate: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "rows_passed / rows_graded (ungraded rows excluded). None — never 0.0 — when nothing "
            "was graded: a suite that was never measured has no pass rate, and 0.0 would publish "
            "'0.0%' for it, indistinguishable from a suite where every row failed."
        ),
    )
    average_weighted_score: float | None = Field(
        default=None, description="Mean weighted_score across rows that produced one."
    )
    criterion_stats: list[CriterionStats] = Field(default_factory=list)
    failed_samples: list[FailedRowSummary] = Field(
        default_factory=list,
        description="Up to K failed/errored rows with failure reasons for error analysis.",
    )
    criterion_aggregates: list[CriterionAggregate] = Field(
        default_factory=list,
        description=(
            "One entry per criterion_type whose aggregate() returned a result. "
            "Empty when no criterion opted into across-row aggregation."
        ),
    )
    passed: bool = Field(
        default=True,
        description=(
            "True when every criterion_aggregate passed its thresholds (or none had thresholds). "
            "Drives CLI exit code for dataset-backed tasks."
        ),
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rows_graded(self) -> int:
        """The denominator behind ``pass_rate``, serialized like its two twins.

        ``RunSummary.tasks_graded`` exists for the same reason and states it: a
        consumer that cannot read the denominator has to re-derive it, which is
        precisely how two surfaces end up publishing different numbers for the
        same suite.
        """
        return self.rows_total - self.rows_not_graded

    @model_validator(mode="after")
    def _check_row_count_invariant(self) -> SuiteRollup:
        """The same guard RunSummary carries, which this model was missing.

        Without it a row that lands outside all four buckets — the shape a new
        FinalStatus category takes before every counter is updated — silently
        drops out of the rollup instead of failing.
        """
        buckets = self.rows_passed + self.rows_failed + self.rows_error + self.rows_not_graded
        if buckets != self.rows_total:
            total = f"{self.rows_passed} + {self.rows_failed} + {self.rows_error} + {self.rows_not_graded}"
            raise ValueError(f"Suite row count invariant violated: {total} != {self.rows_total}")
        return self


class SkippedTask(BaseModel):
    """A task YAML that was excluded from the run before reaching the orchestrator.

    Recorded by ``resolve_all_tasks`` in two cases:

    * **Load failure** — ``load_task`` (YAML parse / Pydantic validation) or
      ``expand_dataset`` raised. ``reason`` carries the exception type + message.
    * **Intentional opt-out** — the YAML set ``skip: true``. ``reason`` is
      prefixed ``"skip: true"`` so consumers can distinguish opt-outs from errors.

    Either way the run continues with the remaining tasks; the suite is not
    aborted by a single bad file or quarantined task.
    """

    path: str = Field(description="Absolute path to the task YAML that was excluded.")
    reason: str = Field(
        description=(
            "Short reason — exception type + message for load failures, or "
            "``'skip: true (task_id=...)'`` for intentional opt-outs."
        ),
    )


def row_cost_incomplete(row: Mapping[str, Any]) -> bool:
    """True when a task row's recorded spend is missing money.

    Reads the row's own ``cost_complete`` flag. Absent means complete: rows written
    before the field existed are read as priced rather than inferred from their
    token counts, which would be a second definition of the same predicate.

    Defined here, on the row schema, so the reports and ``RunSummary`` cannot
    disagree about which rows lost money.
    """
    return row.get("cost_complete") is False


def sum_costs(*components: float | None) -> float | None:
    """Add whichever cost components were priced. ``None`` when none were.

    The one way cost components are combined, so every surface adds them up the
    same way. Never raises and never invents a zero: an unpriced component is
    skipped, which makes the result a floor rather than a failure.
    """
    priced = [c for c in components if c is not None]
    return sum(priced) if priced else None


def eval_overhead_cost(rows: Iterable[Mapping[str, Any]]) -> float | None:
    """Total judge + simulator spend across ``rows``, or ``None`` if none was recorded.

    ``None`` rather than ``0.0`` so "no judge ran" stays distinct from "a judge ran free".
    """
    return sum_costs(*(row.get(key) for row in rows for key in ("judge_cost_usd", "simulator_cost_usd")))


def judge_cost_usd(result: EvaluationResult) -> float | None:
    """Sum the priced judge spend across an evaluation's criterion results.

    Covers both flavors: ``llm_judge`` prices its own one-shot call from the
    criterion's model, ``agent_judge`` inherits the SDK's cost on the sub-agent's
    turn. Post-failure diagnostic judges are included because their calls still
    incur real spend even though their results cannot affect the canonical score.
    ``None`` when no criterion reported cost.
    """
    criterion_results = result.success_criteria_results + result.post_failure_criteria_results
    usages = [
        cr.token_usage
        for cr in criterion_results
        if isinstance(cr, JudgeCriterionResult) and cr.token_usage is not None
    ]
    return sum_costs(*(u.total_cost_usd for u in usages))


def simulator_cost_usd(result: EvaluationResult) -> float | None:
    """Price an evaluation's simulator turns. ``None`` outside simulation mode.

    Priced at the SIMULATOR's own model — ``SimulationConfig.model``, recorded on
    the record as ``simulator_model`` — not the subject's and not the route's. The
    two older fallbacks remain for records written before the model was pinned, when
    the simulator inherited ``BEDROCK_MODEL`` from the route.

    A floor. ``UserSimulator`` records only ``uncached_input_tokens`` and drops
    both cache buckets, so a cached prefix is largely absent from the count.
    """
    from coder_eval.pricing import calculate_cost

    sim = result.simulation
    if sim is None or not (sim.simulator_input_tokens or sim.simulator_output_tokens):
        return None
    route_model = (result.environment_info or {}).get("bedrock_model")
    model = sim.simulator_model or (
        # Legacy records only: the route's model, else the subject's on a non-Bedrock
        # route where the SDK picked its own default and nothing named it.
        route_model if isinstance(route_model, str) and route_model else result.model_used
    )
    if not model:
        return None
    return calculate_cost(
        model,
        uncached_input_tokens=sim.simulator_input_tokens,
        output_tokens=sim.simulator_output_tokens,
    )


def eval_result_total_cost(result: EvaluationResult) -> float | None:
    """Everything one evaluation cost: agent + judge + simulator.

    ``None`` when nothing could be priced, a floor when only part of it could. The
    same figure the row projection publishes as ``total_cost_usd``, for the surfaces
    that hold an ``EvaluationResult`` rather than a row dict.
    """
    agent = result.total_token_usage.total_cost_usd if result.total_token_usage else None
    return sum_costs(agent, judge_cost_usd(result), simulator_cost_usd(result))


class RunSummary(BaseModel):
    """Summary of an entire evaluation run across multiple tasks.

    ``pass_rate`` is ``tasks_succeeded / tasks_graded``: every task that was
    MEASURED is in the denominator, errors included as misses. An earlier formula
    excluded errors, which paid a bonus for erroring. ``error_share`` reports how
    much of the rate is errors, so a bad infrastructure night shows instead of
    being absorbed. Only ungraded tasks (``coder-eval execute``) leave the
    denominator — they were never measured, so a 0/0 run has no rate at all
    rather than a 0% one.

    This is the framework's single denominator: every reporting surface reads
    ``pass_rate`` rather than re-deriving one. Derived metrics here are computed,
    never stored, so they cannot drift from the counts they come from.
    """

    run_id: str = Field(description="Run identifier (timestamp like '2025-10-09_15-30-45')")
    start_time: datetime = Field(description="Run start time")
    end_time: datetime = Field(description="Run end time")
    total_duration_seconds: float = Field(description="Total duration of the run in seconds")

    # Task statistics
    tasks_run: int = Field(description="Total number of tasks executed")
    tasks_succeeded: int = Field(description="Number of tasks that succeeded")
    tasks_failed: int = Field(description="Number of tasks that failed")
    tasks_error: int = Field(description="Number of tasks that encountered errors")
    # Part of the task_count invariant (a fourth bucket, not a sub-counter), but
    # defaulted so run.json written before `coder-eval execute` existed — where
    # no task can be ungraded — still deserialises.
    tasks_not_graded: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of tasks executed without grading (`coder-eval execute`). "
            "Excluded from BOTH sides of pass_rate — an ungraded task was never measured."
        ),
    )

    # Informational sub-counters: subsets of tasks_failed (NOT part of the
    # task_count invariant). Default 0 so old serialized RunSummary JSON
    # without these fields deserialises cleanly.
    tasks_token_budget_exceeded: int = Field(
        default=0,
        ge=0,
        description="Subset of tasks_failed where run_limits token caps tripped.",
    )
    tasks_cost_budget_exceeded: int = Field(
        default=0,
        ge=0,
        description="Subset of tasks_failed where run_limits cost cap tripped.",
    )

    # Tasks excluded at resolution time — either load failures (YAML / schema
    # errors) or intentional opt-outs (``skip: true``). Distinct from
    # tasks_error: these never reached the orchestrator. Empty for runs
    # where every task YAML loaded cleanly and none opted out.
    skipped_tasks: list[SkippedTask] = Field(
        default_factory=list,
        description=(
            "Task YAMLs excluded from execution — either failed schema "
            "validation or opted out via ``skip: true``. See SkippedTask.reason."
        ),
    )

    # Configured concurrency (BatchRunConfig.max_parallel). Defaulted so existing
    # callers and fixtures don't have to set it.
    max_parallel: int = Field(default=1, ge=1, description="Configured max concurrent tasks for this run")

    # Detailed results
    task_results: list[dict[str, Any]] = Field(description="List of task results with {task_id, status, duration}")

    # Environment info
    framework_version: str = Field(description="Version of coder_eval framework")
    environment_info: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Environment and dependency versions. Values are usually strings but may be "
            "nested (e.g. ``tool_plugins`` is a ``{plugin: version}`` dict), so the value "
            "type is ``Any`` to match ``EvaluationResult.environment_info``."
        ),
    )

    @model_validator(mode="after")
    def _check_task_count_invariant(self) -> RunSummary:
        buckets = self.tasks_succeeded + self.tasks_failed + self.tasks_error + self.tasks_not_graded
        if buckets != self.tasks_run:
            total = f"{self.tasks_succeeded} + {self.tasks_failed} + {self.tasks_error} + {self.tasks_not_graded}"
            raise ValueError(f"Task count invariant violated: {total} != {self.tasks_run}")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tasks_graded(self) -> int:
        """Tasks that were actually measured — the denominator for every rate below.

        A ``computed_field`` rather than a plain property so it reaches run.json:
        it is the denominator of `pass_rate` and `error_share`, and a consumer
        that cannot read it has to re-derive the rate from the raw counts — which
        is precisely how a consumer ends up publishing a different number for the
        same run. REPORT_SCHEMA.md documents it as serialized.
        """
        return self.tasks_run - self.tasks_not_graded

    @property
    def _nothing_was_measured(self) -> bool:
        """True when no row in this run produced a criteria verdict.

        ``tasks_graded`` is the complement of the ungraded bucket, so it still
        counts ERROR rows — correct under ``run``, where an errored row was
        attempted and genuinely missed. Under ``coder-eval execute`` no criterion
        runs on ANY row, yet a crashed one lands in the ``error`` bucket rather
        than the ``ungraded`` one, so it stayed in the denominator on its own: a
        100-task execute night with 5 crashes published ``pass_rate 0.0`` and
        ``error_share 1.0`` — a measured-looking total failure for a run that was
        never measured at all, and a real 0% point on the evalboard trend.

        The test is evidence again: if not one row reached a pass or a fail, the
        rate has no numerator to be a fraction of.
        """
        return self.tasks_not_graded > 0 and (self.tasks_succeeded + self.tasks_failed) == 0

    # Derived run metrics: computed_fields over the stored counts and
    # ``task_results``, so they serialize into run.json while staying impossible to
    # set to something the rows disagree with. Consumers should read these rather
    # than re-derive them.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_rate(self) -> float | None:
        """``tasks_succeeded / tasks_graded`` as a 0-1 fraction. ``None`` on an empty run.

        The denominator excludes ungraded tasks (``coder-eval execute``), which were
        never measured — counting them as misses would report a clean execute run as
        0% pass. Identical to ``tasks_run`` for every graded run.

        ``None`` also when no row produced a verdict at all, not just when the run
        is empty — see ``_nothing_was_measured``.
        """
        if self._nothing_was_measured:
            return None
        return self.tasks_succeeded / self.tasks_graded if self.tasks_graded else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def error_share(self) -> float | None:
        """``tasks_error / tasks_graded`` as a 0-1 fraction. ``None`` on an empty run.

        Diagnostic only, never adjusts the rate: a drop at a high error share is an
        infrastructure night, the same drop at a normal share is the model. Shares
        ``pass_rate``'s denominator so the two are directly comparable — including
        being ``None`` on a run where nothing was measured, or an execute night
        with one crashed row reports 100% error.
        """
        if self._nothing_was_measured:
            return None
        return self.tasks_error / self.tasks_graded if self.tasks_graded else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tasks_cost_incomplete(self) -> int:
        """Rows with money missing from their recorded spend (see ``row_cost_incomplete``)."""
        return sum(1 for row in self.task_results if row_cost_incomplete(row))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost_complete(self) -> bool:
        """False when any row's spend is incomplete, so every cost total here is a floor."""
        return self.tasks_cost_incomplete == 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_cost_usd(self) -> float | None:
        """Subject-agent spend across the run. ``None`` when no row reported cost.

        The comparison figure, not the bill: judge spend is a property of the suite's
        criteria and identical across harnesses, so folding it in would make two
        harnesses look closer than they are. ``total_cost_usd`` is the bill.
        """
        return sum_costs(*(row.get("agent_cost_usd") for row in self.task_results))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def eval_overhead_cost_usd(self) -> float | None:
        """Judge + simulator spend across the run. ``None`` when neither reported cost."""
        return eval_overhead_cost(self.task_results)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cost_usd(self) -> float | None:
        """What the run cost: agent + judge + simulator.

        The number to quote for a run's bill, and what every surface means by "total
        cost". ``None`` when nothing could be priced, and a floor rather than an error
        when only some of it could.
        """
        return sum_costs(*(row.get("total_cost_usd") for row in self.task_results))
