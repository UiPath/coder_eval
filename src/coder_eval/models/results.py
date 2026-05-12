"""Evaluation results and execution record models."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator

from coder_eval.models.criteria import SuccessCriterion
from coder_eval.models.enums import AgentKind, FinalStatus
from coder_eval.models.tasks import AgentConfig
from coder_eval.models.telemetry import CommandStatistics, CommandTelemetry, TokenUsage


logger = logging.getLogger(__name__)


class ConfigLineageEntry(BaseModel):
    """Records which config layer provided a specific value.

    The ``-agent-deprecated`` suffixes mark values that came from the legacy
    location (``agent.max_turns`` / ``agent.turn_timeout`` inside the experiment
    or task YAML). Removed alongside the hoist shim on 2026-05-15.
    """

    value: Any
    source: Literal[
        "default",
        "task",
        "experiment-defaults",
        "variant",
        "cli",
        "mutation",
        "default-agent-deprecated",
        "experiment-defaults-agent-deprecated",
        "variant-agent-deprecated",
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

    criterion_type: str = Field(description="Type of criterion")
    description: str = Field(description="Description of what was checked")
    score: float = Field(
        ge=0.0, le=1.0, description="Continuous score from 0.0 (complete failure) to 1.0 (perfect success)"
    )
    details: str | None = Field(default=None, description="Additional details about the result")
    error: str | None = Field(default=None, description="Error message if the check failed")
    pass_threshold: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Score required to pass this criterion (mirrors BaseSuccessCriterion.pass_threshold).",
    )


class ClassificationCriterionResult(CriterionResult):
    """Per-row result for classification criteria.

    Carries the observed and expected labels alongside the standard score so
    the suite-level aggregator can compute P/R/F1 and a confusion matrix.
    Keeping these fields on a subclass (rather than the base) means results
    for non-classification criteria don't carry dead label fields.
    """

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

    findings: list[str] = Field(
        default_factory=list,
        description="Bullet observations the judge cited from the artifacts. Scrubbed before persistence.",
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
            "Filename of the sibling JSON file holding this result's full transcript "
            "(e.g. ``judge-0.yaml``), relative to the directory containing ``task.json``. "
            "Set by ``spill_judge_transcripts`` after the run; reloaded by "
            "``load_judge_transcripts`` for re-rendering. None when no transcript was captured."
        ),
    )


class FileChange(BaseModel):
    """Record of a file change during agent execution."""

    path: str = Field(description="Path to the changed file")
    operation: Literal["created", "modified", "deleted"] = Field(description="Type of change")
    timestamp: datetime = Field(default_factory=datetime.now, description="When the change occurred")


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
            "EvaluationResult.turns as the canonical unique key."
        )
    )
    user_input: str = Field(description="Input prompt to the agent")
    agent_output: str = Field(description="Agent's response (legacy format)")
    commands: list[CommandTelemetry] = Field(
        default_factory=list, description="Detailed telemetry for each command executed during this turn"
    )
    files_changed: list[FileChange] = Field(default_factory=list, description="Files modified during this turn")
    timestamp: datetime = Field(default_factory=datetime.now, description="When this turn occurred")
    duration_seconds: float = Field(default=0.0, description="How long this turn took")
    snapshot_path: str | None = Field(
        default=None, description="Path to snapshot for this iteration (if snapshots enabled)"
    )
    snapshot_size_bytes: int | None = Field(default=None, description="Size of snapshot in bytes (if created)")
    token_usage: TokenUsage | None = Field(
        default=None, description="Token usage for this turn (if available from agent SDK)"
    )
    model_used: str | None = Field(
        default=None, description="Model identifier used for this turn (e.g., 'claude-sonnet-4-5-20250514')"
    )
    assistant_turn_count: int = Field(
        default=0,
        description="Number of AssistantMessage objects received from the SDK in this turn",
    )
    max_turns_exhausted: bool = Field(
        default=False,
        description="Whether the agent hit the max_turns limit without voluntarily completing",
    )
    result_summary: ResultSummary | None = Field(
        default=None,
        description="SDK ResultMessage summary, when one was emitted (clean turns or partials that got one).",
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


class EvaluationResult(BaseModel):
    """Complete result of a task evaluation."""

    task_id: str = Field(description="ID of the evaluated task")
    task_description: str = Field(description="Description of the task")
    variant_id: str = Field(default="default", description="ID of the experiment variant")
    agent_type: AgentKind = Field(description="Type of agent used")
    model_used: str | None = Field(
        default=None, description="Model identifier used for the evaluation (resolved from turns or agent config)"
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
    success_criteria_results: list[SerializeAsAny[CriterionResult]] = Field(
        default_factory=list,
        description=(
            "Results of all success criteria checks. ``SerializeAsAny`` is required so that "
            "subclass-specific fields (e.g. ``JudgeCriterionResult.analysis`` / ``transcript``, "
            "``ClassificationCriterionResult.observed_label``) are emitted by ``model_dump`` — "
            "without it, pydantic serializes against the declared base type and silently drops "
            "subclass fields, losing the audit data on disk."
        ),
    )

    # Detailed transcript
    turns: list[TurnRecord] = Field(default_factory=list, description="Complete transcript of agent interactions")

    # Error information
    error_message: str | None = Field(default=None, description="Error message if evaluation failed")
    error_details: dict[str, Any] | None = Field(
        default=None, description="Detailed error context from error_handling module"
    )
    error_log_tail: str | None = Field(
        default=None,
        description=(
            "Sanitised tail of task.log captured during the run, populated when "
            "final_status is ERROR, TIMEOUT, or FAILURE. Used by the HTML report's Logs disclosure."
        ),
    )

    # Environment information
    environment_info: dict[str, Any] = Field(
        default_factory=dict, description="Version information and environment details"
    )

    # Agent configuration
    agent_config: AgentConfig | None = Field(
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
            # Length mismatch indicates a programming error upstream — log it
            logger.warning(
                "Results/criteria length mismatch: %d results vs %d criteria for task %s."
                + " Falling back to unweighted average.",
                len(self.success_criteria_results),
                len(criteria),
                self.task_id,
            )
            total_score = sum(r.score for r in self.success_criteria_results)
            self.weighted_score = total_score / len(self.success_criteria_results)
            return

        total_weighted_score = 0.0
        total_weight = 0.0

        for result, criterion in zip(self.success_criteria_results, criteria, strict=True):
            total_weighted_score += result.score * criterion.weight
            total_weight += criterion.weight

        self.weighted_score = total_weighted_score / total_weight if total_weight > 0 else 0.0


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
    pass_rate: float = Field(ge=0.0, le=1.0, description="rows_passed / rows_total")
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


class RunSummary(BaseModel):
    """Summary of an entire evaluation run across multiple tasks."""

    run_id: str = Field(description="Run identifier (timestamp like '2025-10-09_15-30-45')")
    start_time: datetime = Field(description="Run start time")
    end_time: datetime = Field(description="Run end time")
    total_duration_seconds: float = Field(description="Total duration of the run in seconds")

    # Task statistics
    tasks_run: int = Field(description="Total number of tasks executed")
    tasks_succeeded: int = Field(description="Number of tasks that succeeded")
    tasks_failed: int = Field(description="Number of tasks that failed")
    tasks_error: int = Field(description="Number of tasks that encountered errors")

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
    environment_info: dict[str, str] = Field(default_factory=dict, description="Environment and dependency versions")

    @model_validator(mode="after")
    def _check_task_count_invariant(self) -> RunSummary:
        if self.tasks_succeeded + self.tasks_failed + self.tasks_error != self.tasks_run:
            total = f"{self.tasks_succeeded} + {self.tasks_failed} + {self.tasks_error}"
            raise ValueError(f"Task count invariant violated: {total} != {self.tasks_run}")
        return self
