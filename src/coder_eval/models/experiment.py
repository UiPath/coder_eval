"""Experiment definition models for multi-run configurations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from coder_eval.models.enums import FinalStatus
from coder_eval.models.limits import RunLimits
from coder_eval.models.mutations import PromptMutation
from coder_eval.models.results import ConfigLineageEntry, EvaluationResult
from coder_eval.models.sandbox import SandboxConfig
from coder_eval.models.tasks import PostRunCommand, PreRunCommand, TaskDefinition
from coder_eval.models.templates import TemplateSource


class ExperimentVariant(BaseModel):
    """A named configuration variant within an experiment."""

    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(description="Unique identifier for this variant (e.g., 'sonnet', 'opus')")
    description: str = Field(default="", description="Human-readable description of what this variant tests")
    agent: dict[str, Any] | None = Field(default=None, description="Partial agent config overrides")
    simulation: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Partial SimulationConfig overrides for this variant. Shallow-merged onto the task's "
            "simulation block (and/or experiment defaults) — common use cases are overriding the "
            "simulator persona/model/temperature per variant."
        ),
    )
    repeats: int | None = Field(
        default=None,
        ge=1,
        description="Number of replicates for each task under this variant. None = inherit.",
    )
    checker_context: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Partial TaskDefinition.checker_context overrides for this variant — currently the "
            "reserved `api_route` namespace (`route`/`model`). Shallow-merged per-namespace onto the "
            "task's (and/or experiment defaults') checker_context."
        ),
    )
    template_sources: list[TemplateSource] | None = Field(
        default=None, description="Additional template sources appended after task's base templates"
    )
    prompt_mutations: list[PromptMutation] | None = Field(
        default=None, description="Ordered list of mutations to apply to the task's initial_prompt"
    )
    initial_prompt: str | None = Field(
        default=None,
        description="Full replacement for the task's initial_prompt. Mutually exclusive with prompt_mutations.",
    )
    initial_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to a file containing a full replacement initial_prompt (relative to experiment YAML). "
            "Mutually exclusive with initial_prompt and prompt_mutations."
        ),
    )
    run_limits: RunLimits | None = Field(
        default=None,
        description=(
            "Per-variant overrides for the task's run_limits block. Field-merge — "
            "per-key precedence inside the block; keys absent here leave the task-level "
            "value intact."
        ),
    )
    driver: Literal["tempdir", "docker"] | None = Field(
        default=None,
        description=(
            "Override the sandbox driver for this variant. Slots into layer 4 of the "
            "5-layer config merge; None inherits from task/experiment-defaults. "
            "Enables variant-A=tempdir vs variant-B=docker comparisons."
        ),
    )

    @model_validator(mode="after")
    def check_prompt_exclusivity(self) -> Self:
        """Ensure prompt_mutations, initial_prompt, and initial_prompt_file are mutually exclusive."""
        set_fields = []
        if self.prompt_mutations is not None:
            set_fields.append("prompt_mutations")
        if self.initial_prompt is not None:
            set_fields.append("initial_prompt")
        if self.initial_prompt_file is not None:
            set_fields.append("initial_prompt_file")
        if len(set_fields) > 1:
            raise ValueError(f"Only one of {', '.join(set_fields)} can be provided, not multiple")
        return self


class ExperimentDefaults(BaseModel):
    """Default settings applied to all variants (overridable per-variant and per-task)."""

    model_config = ConfigDict(extra="forbid")

    repeats: int | None = Field(
        default=None,
        ge=1,
        description="Default number of replicates across all variants. None = 1 (no repetition).",
    )
    agent: dict[str, Any] | None = Field(default=None, description="Partial agent config defaults")
    checker_context: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Default TaskDefinition.checker_context applied to all variants — currently the reserved "
            "`api_route` namespace (e.g. `{api_route: {model: gpt-5}}`). Shallow-merged per-namespace "
            "with the task's and the variant's checker_context."
        ),
    )
    simulation: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Default simulation config applied to tasks that do not set one explicitly. "
            "Fields map to SimulationConfig (enabled, persona, goal, model, max_turns, n_trials, ...)."
        ),
    )
    template_sources: list[TemplateSource] | None = Field(
        default=None, description="Additional template sources appended after task's base templates (for all variants)"
    )
    prompt_mutations: list[PromptMutation] | None = Field(
        default=None, description="Default prompt mutations applied to all variants (before variant-specific mutations)"
    )
    post_run: list[PostRunCommand] | None = Field(
        default=None,
        description="Default post-run commands appended after each task's own post_run (run for every task).",
    )
    pre_run: list[PreRunCommand] | None = Field(
        default=None,
        description="Default pre-run commands prepended before each task's own pre_run (run for every task).",
    )
    run_limits: RunLimits | None = Field(
        default=None,
        description=(
            "Default run-time caps (turns, wall-clock, tokens, USD). "
            "Field-merge — task and variant layers override individual keys without replacing the block."
        ),
    )
    driver: Literal["tempdir", "docker"] | None = Field(
        default=None,
        description=(
            "Default sandbox driver applied to all tasks under this experiment. Slots into "
            "layer 2 of the 5-layer merge; variant- and task-level driver fields override it."
        ),
    )
    sandbox: SandboxConfig | None = Field(
        default=None,
        description=(
            "Default sandbox config applied to all tasks under this experiment. "
            "Field-merge — task and variant layers override individual keys without replacing the block."
        ),
    )


class ExperimentDefinition(BaseModel):
    """Complete experiment definition with base settings and variants."""

    model_config = ConfigDict(extra="forbid")

    experiment_id: str = Field(description="Kebab-case identifier for this experiment")
    description: str = Field(default="", description="Human-readable description")
    defaults: ExperimentDefaults | None = Field(default=None, description="Default settings applied to all variants")
    variants: list[ExperimentVariant] = Field(description="Configuration variants (at least 1)")

    @field_validator("experiment_id")
    @classmethod
    def validate_experiment_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", v):
            raise ValueError(f"experiment_id '{v}' must be kebab-case (e.g., 'model-comparison')")
        return v

    @model_validator(mode="after")
    def validate_variants(self) -> ExperimentDefinition:
        if len(self.variants) < 1:
            raise ValueError("Experiment must have at least 1 variant")
        ids = [v.variant_id for v in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Variant IDs must be unique, got duplicates in: {ids}")
        return self


class VariantResult(BaseModel):  # noqa: CE009 -- persisted result model; round-trip leniency like models/results.py
    """Result for a single variant on a single task."""

    variant_id: str
    task_id: str
    # None when nothing was graded (`coder-eval execute`), mirroring
    # EvaluationResult.weighted_score. A plain float here would launder the
    # ungraded None into 0.000, which renders as — and is picked as a best
    # variant against — a real score of zero.
    weighted_score: float | None = None
    final_status: FinalStatus
    duration_seconds: float
    total_tokens: int | None = None
    iteration_count: int | None = None
    total_assistant_turns: int | None = None
    reference_similarity: float | None = None
    replicate_index: int = Field(
        default=0,
        ge=0,
        description="Replicate index for this per-task result (0 when no replicates).",
    )
    replicate_count: int = Field(
        default=1,
        ge=1,
        description="Number of replicates aggregated into this VariantResult (1 when repeats disabled).",
    )


class VariantAggregate(BaseModel):  # noqa: CE009 -- persisted result model; round-trip leniency like models/results.py
    """Aggregated statistics for a single variant across all tasks.

    ``pass_rate`` uses the same denominator as ``RunSummary.pass_rate``: every task
    the variant ran, errors included as misses. Otherwise an A/B whose variants
    error at different rates compares two different denominators.
    """

    variant_id: str
    tasks_run: int
    tasks_succeeded: int
    tasks_failed: int
    tasks_error: int
    # Fourth bucket of the task_count invariant (see RunSummary.tasks_not_graded).
    # Defaulted so experiment.json written before `coder-eval execute` still loads.
    tasks_not_graded: int = Field(
        default=0,
        ge=0,
        description="Tasks executed without grading (`coder-eval execute`). Excluded from pass_rate entirely.",
    )
    # None when nothing in this variant was graded (`coder-eval execute`).
    # A 0.0 here is indistinguishable from "measured and scored zero" — the same
    # reason EvaluationResult.weighted_score is Optional.
    average_score: float | None
    average_duration: float
    total_tokens: int | None = None
    replicate_count: int = Field(
        default=1,
        ge=1,
        description="Replicate multiplicity (modal value across tasks).",
    )
    # Sub-counters of tasks_failed (NOT part of the task_count invariant).
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

    @model_validator(mode="after")
    def _check_task_count_invariant(self) -> VariantAggregate:
        buckets = self.tasks_succeeded + self.tasks_failed + self.tasks_error + self.tasks_not_graded
        if buckets != self.tasks_run:
            total = f"{self.tasks_succeeded} + {self.tasks_failed} + {self.tasks_error} + {self.tasks_not_graded}"
            raise ValueError(f"Task count invariant violated: {total} != {self.tasks_run}")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tasks_graded(self) -> int:
        """Tasks actually measured — ``pass_rate``'s denominator.

        Serialized for the same reason as its RunSummary twin: a consumer that
        cannot read the denominator re-derives the rate and drifts.
        """
        return self.tasks_run - self.tasks_not_graded

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_rate(self) -> float | None:
        """``tasks_succeeded / tasks_graded`` as a 0-1 fraction. ``None`` when nothing was graded.

        Mirrors ``RunSummary.pass_rate``: ungraded tasks leave both sides.
        """
        return self.tasks_succeeded / self.tasks_graded if self.tasks_graded else None


class TaskExperimentSummary(BaseModel):  # noqa: CE009 -- persisted result model; round-trip leniency like models/results.py
    """Cross-variant summary for a single task."""

    task_id: str
    variant_results: list[VariantResult]
    best_variant: str
    is_tie: bool = Field(default=False, description="True when multiple variants share the highest score")
    score_spread: float
    replicate_count: int = Field(
        default=1,
        ge=1,
        description="Replicate count per variant on this task. Drives Replicate Statistics rendering.",
    )


class ExperimentResult(BaseModel):  # noqa: CE009 -- persisted result model; round-trip leniency like models/results.py
    """Top-level experiment result with per-task summaries and variant aggregates."""

    experiment_id: str
    description: str
    variant_ids: list[str]
    task_summaries: list[TaskExperimentSummary]
    variant_aggregates: dict[str, VariantAggregate]
    total_duration_seconds: float
    per_replicate_scores: dict[str, dict[str, list[float]]] = Field(
        default_factory=dict,
        description=(
            "Raw weighted_score per replicate, keyed variant_id → task_id → [scores]. "
            "Always populated by aggregate_results; list length equals the replicate count. "
            "Empty dict only on deserialized results from before this field existed."
        ),
    )


class ResolvedTask(BaseModel):  # noqa: CE009 -- programmatic resolution model, not YAML input
    """A fully-resolved task ready for batch execution.

    Created by the resolution phase after applying all 5 config layers
    (default → experiment defaults → task YAML → variant → CLI).
    Consumed by run_batch() as the sole input type.
    """

    task: TaskDefinition
    task_file: Path
    run_dir: Path
    variant_id: str
    source_yaml: str = ""
    config_lineage: dict[str, ConfigLineageEntry] = Field(default_factory=dict)
    replicate_index: int = Field(
        default=0,
        ge=0,
        description=(
            "0-based replicate index within the (task, variant) group. Set by resolve_all_tasks. "
            "Always 0 for single-shot tasks and for simulation tasks with n_trials=1. "
            "Simulation tasks with n_trials > 1 are expanded into replicate_index=0..n_trials-1."
        ),
    )


class TaskResult(BaseModel):  # noqa: CE009 -- persisted result model; round-trip leniency like models/results.py
    """Result from executing a single resolved task.

    Replaces the untyped dict[str, Any] with keys {task_id, result, duration}
    that previously flowed between run_batch and aggregate_results.
    """

    task_id: str
    result: EvaluationResult
    duration: float
    variant_id: str
    suite_id: str | None = Field(
        default=None,
        description="Parent suite id when this task came from dataset fan-out. Signal for suite rollup reporting.",
    )
    row_id: str | None = Field(
        default=None,
        description="Row id within the suite (from Dataset.id_field) when this task came from dataset fan-out.",
    )
    replicate_index: int = Field(
        default=0,
        ge=0,
        description="0-based replicate index from ResolvedTask. Populated by batch.py.",
    )
