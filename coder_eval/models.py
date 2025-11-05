"""Pydantic models for task definitions, configurations, and evaluation results."""

from __future__ import annotations

from abc import ABC
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# No longer need Sandbox import - models are pure data containers
# Business logic moved to SuccessChecker in evaluator.py

# ============================================================================
# Type Aliases
# ============================================================================

# Forward declarations for type aliases (defined after their referenced classes)
# These improve readability and maintainability of complex type hints

# ============================================================================
# Sandbox Configuration Models
# ============================================================================


class StarterFile(BaseModel):
    """A file to create in the sandbox before agent starts."""

    path: str = Field(description="Relative path in sandbox (e.g., 'src/main.py')")
    content: str = Field(description="File content")


class BaseTemplateSource(BaseModel, ABC):
    """Base class for template sources - defines the discriminated union.

    Note: No type field here - each subclass defines its own Literal type.
    """

    pass


class TemplateDirSource(BaseTemplateSource):
    """Copy files from a local directory into the sandbox."""

    type: Literal["template_dir"] = "template_dir"
    path: str = Field(description="Path to template directory (relative to task YAML or absolute)")


class StarterFilesSource(BaseTemplateSource):
    """Create inline files from YAML definitions."""

    type: Literal["starter_files"] = "starter_files"
    files: list[StarterFile] = Field(description="List of files to create")


class RepoSource(BaseTemplateSource):
    """Clone files from a git repository."""

    type: Literal["repo"] = "repo"
    url: str = Field(description="Git repository URL")
    commit: str | None = Field(default=None, description="Specific commit SHA to checkout")


# Discriminated union of template sources
TemplateSource = TemplateDirSource | StarterFilesSource | RepoSource


class ResourceLimits(BaseModel):
    """Resource limits for sandbox execution."""

    timeout: int = Field(default=300, description="Maximum execution time in seconds")
    max_memory_mb: int | None = Field(default=None, description="Maximum memory in MB (currently not enforced)")
    max_disk_mb: int | None = Field(default=None, description="Maximum disk usage in MB (currently not enforced)")


class SnapshotMode(str, Enum):
    """Snapshot mode for iteration snapshots.

    Note: No separate 'enabled' flag needed - use DISABLED to disable snapshots.
    """

    DISABLED = "disabled"  # No snapshots
    FULL = "full"  # Copy entire sandbox every iteration
    INCREMENTAL = "incremental"  # Only changed files
    HYBRID = "hybrid"  # Full at checkpoints, incremental otherwise


class SnapshotConfig(BaseModel):
    """Configuration for iteration snapshots.

    Note: No 'enabled' flag - use mode=DISABLED to disable snapshots.
    This avoids redundant state (e.g., enabled=False, mode=FULL).
    """

    mode: SnapshotMode = Field(
        default=SnapshotMode.DISABLED, description="Snapshot mode (default: disabled for backward compatibility)"
    )
    checkpoint_frequency: int = Field(
        default=5, ge=1, description="Full snapshot every N iterations (hybrid mode only)"
    )
    ignore_patterns: list[str] = Field(
        default_factory=list, description="Additional file patterns to exclude (beyond sandbox defaults like .venv)"
    )


class SnapshotManifest(BaseModel):
    """Metadata for a single snapshot.

    Stored as manifest.json in each snapshot directory.
    """

    created_at: datetime = Field(description="When this snapshot was created")
    iteration: int = Field(description="Iteration number (0-indexed)")
    mode: SnapshotMode = Field(description="Snapshot mode used (full/incremental)")
    size_bytes: int = Field(description="Total size of snapshot in bytes")
    file_count: int = Field(description="Number of files in snapshot")
    changed_files: list[str] = Field(
        default_factory=list,
        description="List of changed file paths (for incremental snapshots, includes DELETED: markers)",
    )
    base_iteration: int | None = Field(
        default=None, description="For incremental: which iteration to apply changes to (typically iteration - 1)"
    )


class SandboxConfig(BaseModel):
    """Configuration for the sandboxed execution environment."""

    driver: Literal["tempdir"] = Field(default="tempdir", description="Sandbox driver type (only tempdir supported)")
    python_version: str = Field(default="3.13", description="Python version to use in the sandbox")
    env_packages: list[str] = Field(default_factory=list, description="Python packages to install in the environment")
    network_enabled: bool = Field(default=False, description="Whether network access is enabled")
    limits: ResourceLimits = Field(default_factory=ResourceLimits, description="Resource limits for execution")

    # Multi-source template support
    template_sources: list[TemplateSource] | None = Field(
        default=None, description="Sequential list of template sources to apply"
    )

    # Snapshot configuration
    snapshots: SnapshotConfig = Field(default_factory=SnapshotConfig, description="Iteration snapshot configuration")

    # Customizable ignore patterns
    additional_ignore_patterns: list[str] = Field(
        default_factory=list,
        description="Additional patterns to ignore during template setup and snapshots (beyond defaults)",
    )

    @model_validator(mode="after")
    def validate_template_sources(self) -> SandboxConfig:
        """Validate template sources configuration."""
        if self.template_sources:
            # Check for multiple RepoSource entries
            repo_sources = [src for src in self.template_sources if isinstance(src, RepoSource)]
            if len(repo_sources) > 1:
                raise ValueError("Only one RepoSource is allowed in template_sources.")

            # Check that RepoSource (if present) is first
            if len(repo_sources) == 1 and not isinstance(self.template_sources[0], RepoSource):
                raise ValueError(
                    "RepoSource must be the first element in template_sources (git clone requires an empty directory)."
                )

            # Warn if too many sources
            if len(self.template_sources) > 10:
                import warnings

                warnings.warn(
                    f"Many template sources ({len(self.template_sources)}) - this may be a misconfiguration",
                    UserWarning,
                    stacklevel=2,
                )

        return self


# ============================================================================
# Agent Configuration
# ============================================================================


class AgentKind(str, Enum):
    """Supported agent types."""

    CLAUDE_CODE = "claude-code"
    AIDER = "aider"


class AgentConfig(BaseModel):
    """Configuration for the coding agent."""

    type: AgentKind = Field(description="The type of agent to use (claude-code, aider, etc.)")
    permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions"] = Field(
        default="acceptEdits", description="Permission mode for agent actions"
    )
    allowed_tools: list[str] | None = Field(
        default=None, description="List of allowed tools (e.g., ['Read', 'Write', 'Bash'])"
    )
    model: str | None = Field(default=None, description="Specific model to use (if applicable)")

    # Customizable ignore patterns for file tracking
    additional_ignore_patterns: list[str] = Field(
        default_factory=list,
        description="Additional patterns to ignore during file change detection (beyond defaults)",
    )


# ============================================================================
# Success Criteria
# ============================================================================


class BaseSuccessCriterion(BaseModel, ABC):
    """Base class for success criteria - pure data container.

    Success criteria are data models that describe WHAT to check,
    but not HOW to check it. The checking logic belongs in SuccessChecker.

    This separation provides:
    - Models focus on data + validation (Pydantic's strength)
    - SuccessChecker focuses on business logic
    - Easier testing with mocked sandboxes
    - Better separation of concerns
    - No circular dependency with Sandbox class
    """

    description: str = Field(description="Human-readable description of what this criterion checks")

    weight: float = Field(default=1.0, gt=0.0, description="Relative importance of this criterion (default: 1.0)")

    pass_threshold: float = Field(
        default=0.9, ge=0.0, le=1.0, description="Minimum score required to pass (default: 0.9 = 90%)"
    )

    # Business logic (check operations) moved to SuccessChecker in evaluator.py


class FileExistsCriterion(BaseSuccessCriterion):
    """Check if a file exists at the specified path.

    Pure data model - checking logic in SuccessChecker._check_file_exists()
    """

    type: Literal["file_exists"] = "file_exists"
    path: str = Field(description="Path to the file that must exist")


class FileContainsCriterion(BaseSuccessCriterion):
    """Check if a file contains specific strings.

    Pure data model - checking logic in SuccessChecker._check_file_contains()
    """

    type: Literal["file_contains"] = "file_contains"
    path: str = Field(description="Path to the file to check")
    includes: list[str] = Field(description="List of strings that must be present in the file")
    excludes: list[str] | None = Field(default=None, description="List of strings that must NOT be present in the file")


class RunCommandCriterion(BaseSuccessCriterion):
    """Check if a command runs successfully.

    Pure data model - checking logic in SuccessChecker._check_run_command()
    """

    type: Literal["run_command"] = "run_command"
    command: str = Field(description="Command to execute")
    timeout: int = Field(default=30, description="Timeout in seconds")
    expected_exit_code: int = Field(default=0, description="Expected exit code")


class ProgramStdoutEqualsCriterion(BaseSuccessCriterion):
    """Check if program output matches expected output.

    Pure data model - checking logic in SuccessChecker._check_program_stdout()
    """

    type: Literal["program_stdout_equals"] = "program_stdout_equals"
    command: str = Field(description="Command to execute")
    expected_output: str = Field(description="Expected stdout output (exact match)")
    timeout: int = Field(default=30, description="Timeout in seconds")


class PytestCriterion(BaseSuccessCriterion):
    """Run pytest and check for success.

    Pure data model - checking logic in SuccessChecker._check_pytest()
    """

    type: Literal["pytest"] = "pytest"
    path: str = Field(default=".", description="Path to test directory or file")
    args: list[str] = Field(default_factory=list, description="Additional pytest arguments")
    timeout: int = Field(default=60, description="Timeout in seconds")


class FileMatchesRegexCriterion(BaseSuccessCriterion):
    """Check if file content matches a regex pattern.

    Pure data model - checking logic in SuccessChecker._check_file_matches_regex()
    """

    type: Literal["file_matches_regex"] = "file_matches_regex"
    path: str = Field(description="Path to the file to check")
    pattern: str = Field(description="Regex pattern that must match somewhere in the file")
    must_match: bool = Field(default=True, description="If True, pattern must match; if False, pattern must NOT match")
    flags: int = Field(default=0, description="Regex flags (e.g., re.IGNORECASE=2, re.MULTILINE=8, re.DOTALL=16)")


class CodeLintsCriterion(BaseSuccessCriterion):
    """Run a code linter and check for success.

    Pure data model - checking logic in SuccessChecker._check_code_lints()
    """

    type: Literal["code_lints"] = "code_lints"
    linter: str = Field(description="Linter command (e.g., 'ruff check', 'pylint', 'eslint')")
    path: str = Field(default=".", description="Path to lint (file or directory)")
    args: list[str] = Field(default_factory=list, description="Additional linter arguments")
    timeout: int = Field(default=60, description="Timeout in seconds")
    allow_warnings: bool = Field(
        default=False, description="If True, only fail on errors; if False, fail on warnings too"
    )


class PylintScoreCriterion(BaseSuccessCriterion):
    """Run pylint and evaluate code based on its quality score.

    Pylint provides a comprehensive score from 0-10 that reflects:
    - Code correctness (errors)
    - Code quality (warnings, conventions)
    - Maintainability (refactoring opportunities)
    - Style compliance (formatting, naming)

    This criterion normalizes pylint's score to 0.0-1.0 for evaluation.

    Pure data model - checking logic in SuccessChecker._check_pylint_score()

    Example YAML:
        success_criteria:
          - type: "pylint_score"
            path: "src/"
            pass_threshold: 0.85  # Requires 8.5/10
            weight: 1.5
            description: "Code must meet high quality standards"
            min_score: 8.5        # Optional: explicit minimum (overrides pass_threshold)
            args: ["--disable=C0111"]  # Optional: extra args
            timeout: 120
    """

    type: Literal["pylint_score"] = "pylint_score"
    path: str = Field(description="Path to analyze (file or directory)")
    min_score: float | None = Field(
        default=None,
        le=10.0,
        description=(
            "Optional explicit minimum score (0-10, can be negative for very poor code). "
            "If set, overrides pass_threshold for clarity."
        ),
    )
    fail_under: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description="Optional pylint --fail-under flag (0-10). Causes pylint to exit non-zero below this.",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Additional pylint arguments (e.g., ['--disable=C0111', '--max-line-length=120'])",
    )
    rcfile: str | None = Field(
        default=None, description="Path to pylintrc configuration file (relative to sandbox root)"
    )
    timeout: int = Field(default=120, description="Timeout in seconds (pylint can be slow on large codebases)")

    @model_validator(mode="after")
    def normalize_min_score_to_threshold(self) -> PylintScoreCriterion:
        """Convert min_score (0-10) to pass_threshold (0-1) if set.

        This allows users to specify thresholds in the familiar pylint scale
        while maintaining internal consistency with the 0-1 score range.

        Handles negative scores by clamping to 0.0.

        **Important:** If both min_score and pass_threshold are specified,
        min_score takes precedence and overrides pass_threshold. This is
        intentional to avoid confusion between the 0-10 and 0-1 scales.
        """
        if self.min_score is not None:
            # Normalize to 0-1, clamping negative scores to 0
            # Examples:
            #   min_score=8.5  -> pass_threshold=0.85
            #   min_score=-1.0 -> pass_threshold=0.0
            #   min_score=10.0 -> pass_threshold=1.0
            self.pass_threshold = max(0.0, min(1.0, self.min_score / 10.0))
        return self


class ReferenceComparisonCriterion(BaseSuccessCriterion):
    """Compare agent code against reference solution.

    Uses the top-level `reference` block from TaskDefinition.
    The reference code is loaded by the orchestrator and passed to the success checker.

    Pure data model - checking logic in SuccessChecker._check_reference_comparison()

    Example YAML:
        success_criteria:
          - type: "reference_comparison"
            description: "Code structure matches reference"
            agent_file: "solution.py"
            comparison_method: "ast"
            similarity_threshold: 0.8
            weight: 1.0
            pass_threshold: 0.8
    """

    type: Literal["reference_comparison"] = "reference_comparison"

    # Required fields
    agent_file: str = Field(description="Path to agent's generated file (relative to sandbox root)")

    comparison_method: Literal["ast", "token", "complexity"] = Field(
        default="ast",
        description="Method for comparing code: 'ast' (structure), 'token' (text), 'complexity' (metrics)",
    )

    similarity_threshold: float = Field(
        default=0.8, ge=0.0, le=1.0, description="Minimum similarity score to pass (0.0-1.0)"
    )

    @model_validator(mode="after")
    def align_threshold_to_similarity(self) -> ReferenceComparisonCriterion:
        """Align pass_threshold to similarity_threshold for clarity.

        Since similarity_threshold is the domain-specific field, it takes precedence.
        """
        self.pass_threshold = self.similarity_threshold
        return self


# Discriminated union of all success criteria
SuccessCriterion = (
    FileExistsCriterion
    | FileContainsCriterion
    | RunCommandCriterion
    | ProgramStdoutEqualsCriterion
    | PytestCriterion
    | FileMatchesRegexCriterion
    | CodeLintsCriterion
    | PylintScoreCriterion
    | ReferenceComparisonCriterion
)


# ============================================================================
# Evaluation Results (needed by criteria check() methods)
# ============================================================================


class CriterionResult(BaseModel):
    """Result of checking a single success criterion."""

    criterion_type: str = Field(description="Type of criterion")
    description: str = Field(description="Description of what was checked")
    score: float = Field(
        ge=0.0, le=1.0, description="Continuous score from 0.0 (complete failure) to 1.0 (perfect success)"
    )
    details: str | None = Field(default=None, description="Additional details about the result")
    error: str | None = Field(default=None, description="Error message if the check failed")


# ============================================================================
# LLM Reviewer Configuration
# ============================================================================


class LLMReviewerConfig(BaseModel):
    """Configuration for the LLM-based qualitative reviewer.

    All models are accessed through UiPath LLM Gateway using LangChain integration.
    Use Gateway model naming convention (e.g., anthropic.claude-3-5-sonnet-20240620-v1:0, gpt-4o-2024-08-06).
    """

    enabled: bool = Field(default=False, description="Whether to enable LLM review")
    model: str = Field(
        default="anthropic.claude-3-5-sonnet-20240620-v1:0",
        description="Gateway model name (e.g., anthropic.claude-3-5-sonnet-20240620-v1:0, gpt-4o-2024-08-06)",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="Temperature for LLM sampling")
    max_tokens: int = Field(default=1000, gt=0, description="Maximum tokens in response")


# ============================================================================
# Reference Solution
# ============================================================================


class ReferenceSource(BaseModel):
    """Defines the source for reference solution code.

    This code is NEVER shown to the agent being evaluated.
    It is used by:
    - LLMReviewer: To provide expert feedback comparing agent output to reference
    - ReferenceComparisonCriterion: For objective code similarity checks

    Security: Reference solutions must never leak into agent prompts or logs.
    """

    code: str | None = Field(default=None, description="Inline reference code (for simple, short solutions)")
    file: str | None = Field(default=None, description="Path to file containing reference code (relative to task YAML)")

    @model_validator(mode="after")
    def check_exclusive_source(self):
        """Ensure exactly one source is provided."""
        if self.code is not None and self.file is not None:
            raise ValueError("Only one of 'code' or 'file' can be provided for reference code.")
        if self.code is None and self.file is None:
            raise ValueError("One of 'code' or 'file' must be provided for reference code.")
        return self


# ============================================================================
# Task Definition
# ============================================================================


class TaskDefinition(BaseModel):
    """Complete definition of an evaluation task."""

    task_id: str = Field(description="Unique identifier for this task")
    description: str = Field(description="Human-readable description of what the task is testing")
    initial_prompt: str = Field(description="The initial prompt to send to the agent")
    max_iterations: int = Field(default=3, description="Maximum number of agent turns")
    agent: AgentConfig = Field(description="Agent configuration")
    sandbox: SandboxConfig = Field(description="Sandbox configuration")
    success_criteria: list[SuccessCriterion] = Field(description="List of criteria that must all pass for task success")
    llm_reviewer: LLMReviewerConfig = Field(
        default_factory=LLMReviewerConfig, description="Optional LLM reviewer configuration"
    )
    reference: ReferenceSource | None = Field(
        default=None,
        description=(
            "Reference solution for LLM review and code comparison. HIDDEN from the agent - never included in prompts."
        ),
    )

    @field_validator("success_criteria")
    @classmethod
    def validate_success_criteria(cls, v):
        """Ensure at least one success criterion is defined."""
        if not v:
            raise ValueError("At least one success criterion must be defined")
        return v


# ============================================================================
# Execution Results
# ============================================================================


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


class CommandTelemetry(BaseModel):
    """Telemetry for a single command execution.

    Captures detailed information about each tool use by the agent,
    enabling analysis, debugging, and optimization.
    """

    # Identity
    tool_name: str = Field(description="Tool name (Read, Write, Bash, Edit, Glob, etc.)")
    tool_id: str = Field(description="Unique ID from Claude SDK for this tool invocation")

    # Timing
    timestamp: datetime = Field(description="When the command was executed")
    duration_ms: float | None = Field(
        default=None,
        description="Command execution time in milliseconds (None = not complete, calculated in two-phase processing)",
    )

    # Parameters (structured)
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Structured command parameters (e.g., {'file_path': 'main.py'} for Read)"
    )

    # Results
    result_status: Literal["success", "error", "unknown"] | None = Field(
        default=None,
        description="Whether the command succeeded or failed (None = pending result, set during two-phase processing)",
    )
    result_summary: str | None = Field(
        default=None, description="Brief summary of result (e.g., 'File read: 245 bytes', 'Exit code: 0')"
    )
    error_message: str | None = Field(default=None, description="Error message if command failed")

    # Metadata
    sequence_number: int = Field(default=0, description="Order within the turn (0-indexed)")


class SlowestCommandInfo(BaseModel):
    """Information about a slow command for performance analysis.

    Type-safe model for reporting slowest commands in statistics.
    """

    tool: str = Field(description="Tool name (e.g., 'Bash', 'Read')")
    duration_ms: float = Field(description="Execution duration in milliseconds")
    parameters: dict[str, Any] = Field(description="Command parameters")
    tool_id: str | None = Field(default=None, description="Optional: Unique tool invocation ID")


class CommandStatistics(BaseModel):
    """Aggregated statistics for command usage in an evaluation.

    Provides summary metrics for analysis and reporting.
    """

    total_commands: int = Field(description="Total number of commands executed")
    commands_by_tool: dict[str, int] = Field(
        default_factory=dict, description="Count of commands per tool (e.g., {'Bash': 45, 'Read': 12})"
    )

    # Timing
    total_command_time_ms: float = Field(default=0.0, description="Total time spent executing commands (milliseconds)")
    avg_command_time_ms: float | None = Field(default=None, description="Average command execution time")
    slowest_commands: list[SlowestCommandInfo] = Field(
        default_factory=list, description="Top 5 slowest commands with details (type-safe model)"
    )

    # Success/Failure
    successful_commands: int = Field(default=0, description="Commands that succeeded")
    failed_commands: int = Field(default=0, description="Commands that failed")
    unknown_commands: int = Field(
        default=0,
        description="Commands with unknown status (missing ResultMessage, indicates agent/SDK interruption)",
    )
    success_rate: float = Field(
        default=0.0,
        description="Percentage of known commands that succeeded: success / (success + failed), excludes unknown",
    )

    # Patterns
    most_common_sequence: str | None = Field(
        default=None, description="Most common 3-command sequence (e.g., 'Read → Edit → Bash')"
    )


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

    def calculate_weighted_score(self, criteria: SuccessCriteria) -> None:
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


# ============================================================================
# Run Summary (for multiple task evaluations)
# ============================================================================


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


# ============================================================================
# Type Alias Definitions (After Class Declarations)
# ============================================================================

# Success criteria and results
type CriteriaResults = list[CriterionResult]
type SuccessCriteria = list[SuccessCriterion]

# File tracking
type FileTree = dict[str, float]  # path -> modification time
type FileChanges = list[FileChange]

# Agent execution
type TurnRecords = list[TurnRecord]
