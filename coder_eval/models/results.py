"""Evaluation results and execution record models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from coder_eval.models.criteria import SuccessCriterion
from coder_eval.models.enums import AgentKind
from coder_eval.models.telemetry import CommandStatistics, CommandTelemetry, TokenUsage


class CriterionResult(BaseModel):
    """Result of checking a single success criterion."""

    criterion_type: str = Field(description="Type of criterion")
    description: str = Field(description="Description of what was checked")
    score: float = Field(
        ge=0.0, le=1.0, description="Continuous score from 0.0 (complete failure) to 1.0 (perfect success)"
    )
    details: str | None = Field(default=None, description="Additional details about the result")
    error: str | None = Field(default=None, description="Error message if the check failed")


class LLMDecision(BaseModel):
    """Decision from the LLM reviewer with direct, developer-style feedback.

    Uses terse code review language focused on problems and actions,
    not diplomatic assessments.

    v0.2.0+: Field names changed from assessment/suggestions to issues/next_steps.
    Pydantic aliases maintain backward compatibility with old JSON.
    """

    model_config = {"populate_by_name": True}  # Allow both new and old field names

    issues: str = Field(
        alias="assessment",  # Backward compatibility: old JSON with "assessment" still works
        description="Direct critique in 1-2 sentences. Focus on problems, not praise.",
    )
    score: float = Field(ge=0.0, le=1.0, description="Score from 0.0 (broken) to 1.0 (perfect)")
    next_steps: list[str] = Field(
        default_factory=list,
        alias="suggestions",  # Backward compatibility: old JSON with "suggestions" still works
        description="Action-oriented imperatives (e.g., 'Fix X', 'Add Y')",
    )
    should_continue: bool = Field(description="Whether the agent should continue working")


class FileChange(BaseModel):
    """Record of a file change during agent execution."""

    path: str = Field(description="Path to the changed file")
    operation: Literal["created", "modified", "deleted"] = Field(description="Type of change")
    timestamp: datetime = Field(default_factory=datetime.now, description="When the change occurred")


class TurnRecord(BaseModel):
    """Record of a single agent turn (input + output)."""

    iteration: int = Field(description="Turn number")
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


class EvaluationResult(BaseModel):
    """Complete result of a task evaluation."""

    task_id: str = Field(description="ID of the evaluated task")
    task_description: str = Field(description="Description of the task")
    agent_type: AgentKind = Field(description="Type of agent used")

    # Execution metadata
    started_at: datetime = Field(description="When evaluation started")
    completed_at: datetime | None = Field(default=None, description="When evaluation completed")
    duration_seconds: float = Field(default=0.0, description="Total evaluation duration")

    # Results
    final_status: Literal["SUCCESS", "FAILURE", "ERROR", "TIMEOUT"] = Field(
        description="Final status of the evaluation"
    )
    weighted_score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Weighted average of criterion scores (0.0 to 1.0)"
    )
    iteration_count: int = Field(description="Number of iterations completed")
    success_criteria_results: list[CriterionResult] = Field(
        default_factory=list, description="Results of all success criteria checks"
    )
    llm_review: LLMDecision | None = Field(default=None, description="Optional LLM reviewer decision")

    # Detailed transcript
    turns: list[TurnRecord] = Field(default_factory=list, description="Complete transcript of agent interactions")

    # Error information
    error_message: str | None = Field(default=None, description="Error message if evaluation failed")
    error_details: dict[str, Any] | None = Field(
        default=None, description="Detailed error context from error_handling module"
    )

    # Environment information
    environment_info: dict[str, Any] = Field(
        default_factory=dict, description="Version information and environment details"
    )

    # Artifacts
    sandbox_path: str | None = Field(default=None, description="Path to preserved sandbox (if saved)")

    # Command telemetry
    command_stats: CommandStatistics | None = Field(default=None, description="Aggregated command telemetry statistics")

    # Token usage
    total_token_usage: TokenUsage | None = Field(default=None, description="Aggregated token usage across all turns")

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
            # Length mismatch - use simple average as fallback
            total_score = sum(r.score for r in self.success_criteria_results)
            self.weighted_score = total_score / len(self.success_criteria_results)
            return

        total_weighted_score = 0.0
        total_weight = 0.0

        for result, criterion in zip(self.success_criteria_results, criteria, strict=True):
            total_weighted_score += result.score * criterion.weight
            total_weight += criterion.weight

        self.weighted_score = total_weighted_score / total_weight if total_weight > 0 else 0.0


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

    # Detailed results
    task_results: list[dict[str, Any]] = Field(description="List of task results with {task_id, status, duration}")

    # Environment info
    framework_version: str = Field(description="Version of coder_eval framework")
    environment_info: dict[str, str] = Field(default_factory=dict, description="Environment and dependency versions")
