"""Experiment definition models for multi-run configurations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from coder_eval.models.enums import FinalStatus
from coder_eval.models.mutations import PromptMutation
from coder_eval.models.results import ConfigLineageEntry, EvaluationResult
from coder_eval.models.tasks import PostRunCommand, TaskDefinition
from coder_eval.models.templates import TemplateSource


class ExperimentVariant(BaseModel):
    """A named configuration variant within an experiment."""

    variant_id: str = Field(description="Unique identifier for this variant (e.g., 'sonnet', 'opus')")
    agent: dict[str, Any] | None = Field(default=None, description="Partial agent config overrides")
    max_iterations: int | None = Field(default=None, description="Override max iterations for this variant")
    task_timeout: int | None = Field(default=None, ge=30, description="Override task timeout (seconds)")
    turn_timeout: int | None = Field(default=None, ge=10, description="Override turn timeout (seconds)")
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

    max_iterations: int | None = Field(default=None, description="Default max iterations")
    task_timeout: int | None = Field(default=None, ge=30, description="Default task timeout (seconds)")
    turn_timeout: int | None = Field(default=None, ge=10, description="Default turn timeout (seconds)")
    agent: dict[str, Any] | None = Field(default=None, description="Partial agent config defaults")
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


class ExperimentDefinition(BaseModel):
    """Complete experiment definition with base settings and variants."""

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


class VariantResult(BaseModel):
    """Result for a single variant on a single task."""

    variant_id: str
    task_id: str
    weighted_score: float
    final_status: FinalStatus
    duration_seconds: float
    total_tokens: int | None = None
    iteration_count: int | None = None
    total_assistant_turns: int | None = None
    reference_similarity: float | None = None


class VariantAggregate(BaseModel):
    """Aggregated statistics for a single variant across all tasks."""

    variant_id: str
    tasks_run: int
    tasks_succeeded: int
    tasks_failed: int
    tasks_error: int
    average_score: float
    average_duration: float
    total_tokens: int | None = None

    @model_validator(mode="after")
    def _check_task_count_invariant(self) -> VariantAggregate:
        if self.tasks_succeeded + self.tasks_failed + self.tasks_error != self.tasks_run:
            raise ValueError(
                f"Task count invariant violated: {self.tasks_succeeded} + {self.tasks_failed} + {self.tasks_error}"
                f" != {self.tasks_run}"
            )
        return self


class TaskExperimentSummary(BaseModel):
    """Cross-variant summary for a single task."""

    task_id: str
    variant_results: list[VariantResult]
    best_variant: str
    is_tie: bool = Field(default=False, description="True when multiple variants share the highest score")
    score_spread: float


class ExperimentResult(BaseModel):
    """Top-level experiment result with per-task summaries and variant aggregates."""

    experiment_id: str
    description: str
    variant_ids: list[str]
    task_summaries: list[TaskExperimentSummary]
    variant_aggregates: dict[str, VariantAggregate]
    total_duration_seconds: float


class ResolvedTask(BaseModel):
    """A fully-resolved task ready for batch execution.

    Created by the resolution phase after applying all 5 config layers
    (default experiment -> task YAML -> experiment base -> variant -> CLI overrides).
    Consumed by run_batch() as the sole input type.
    """

    task: TaskDefinition
    task_file: Path
    run_dir: Path
    variant_id: str
    source_yaml: str = ""
    config_lineage: dict[str, ConfigLineageEntry] = Field(default_factory=dict)


class TaskResult(BaseModel):
    """Result from executing a single resolved task.

    Replaces the untyped dict[str, Any] with keys {task_id, result, duration}
    that previously flowed between run_batch and aggregate_results.
    """

    task_id: str
    result: EvaluationResult
    duration: float
    variant_id: str
