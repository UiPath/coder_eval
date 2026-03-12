"""Experiment definition models for multi-run configurations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from coder_eval.models.results import ConfigLineageEntry, EvaluationResult
from coder_eval.models.tasks import TaskDefinition


class ExperimentVariant(BaseModel):
    """A named configuration variant within an experiment."""

    variant_id: str = Field(description="Unique identifier for this variant (e.g., 'sonnet', 'opus')")
    agent: dict[str, Any] | None = Field(default=None, description="Partial agent config overrides")
    max_iterations: int | None = Field(default=None, description="Override max iterations for this variant")
    task_timeout: int | None = Field(default=None, ge=30, description="Override task timeout (seconds)")
    turn_timeout: int | None = Field(default=None, ge=10, description="Override turn timeout (seconds)")


class ExperimentBase(BaseModel):
    """Base settings applied to all variants (overridable per-variant)."""

    max_iterations: int | None = Field(default=None, description="Default max iterations")
    task_timeout: int | None = Field(default=None, ge=30, description="Default task timeout (seconds)")
    turn_timeout: int | None = Field(default=None, ge=10, description="Default turn timeout (seconds)")
    agent: dict[str, Any] | None = Field(default=None, description="Partial agent config overrides")


class ExperimentDefinition(BaseModel):
    """Complete experiment definition with base settings and variants."""

    experiment_id: str = Field(description="Kebab-case identifier for this experiment")
    description: str = Field(default="", description="Human-readable description")
    base: ExperimentBase | None = Field(default=None, description="Base settings applied to all variants")
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
    final_status: str
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
    average_score: float
    average_duration: float
    total_tokens: int | None = None


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
