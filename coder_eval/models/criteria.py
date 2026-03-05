"""Success criteria models for task evaluation."""

from __future__ import annotations

from abc import ABC
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, model_validator


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

    requires_agent: ClassVar[bool] = False
    """True if this criterion requires agent turn records to evaluate correctly."""

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

    requires_agent: ClassVar[bool] = True

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


class CommandExecutedCriterion(BaseSuccessCriterion):
    """Check whether the agent executed specific commands/tools.

    Inspects CommandTelemetry records from TurnRecord.commands to verify
    that the agent used specific tools or commands during evaluation.

    Pure data model - checking logic in CommandExecutedChecker._check_impl()

    Example YAML:
        success_criteria:
          - type: "command_executed"
            description: "Agent used curl to fetch weather"
            tool_name: "Bash"
            command_pattern: "curl.*wttr\\.in"
            min_count: 1
            require_success: true
    """

    requires_agent: ClassVar[bool] = True

    type: Literal["command_executed"] = "command_executed"
    tool_name: str | None = Field(default=None, description="Tool name filter (e.g., 'Bash'). None = any tool.")
    command_pattern: str | None = Field(
        default=None, description="Regex to match command parameters. None = any command."
    )
    min_count: int = Field(default=1, ge=1, description="Minimum matching commands required.")
    require_success: bool = Field(default=False, description="If True, only count successful commands.")


class UiPathEvalCriterion(BaseSuccessCriterion):
    """Check evaluation results against UiPath agent performance.

    Pure data model - checking logic in UiPathEvalChecker._check_impl()

    Threshold semantics: all thresholds are *minimum acceptable values* — a metric
    passes if ``metric_value >= threshold``. For metrics where lower is better
    (e.g. latency), negate or invert the metric on the evaluation side rather than
    here (e.g. use ``throughput = 1 / avg_time`` and threshold on that instead).
    """

    type: Literal["uipath_eval"] = "uipath_eval"
    agent_name: str = Field(description="Name of the UiPath agent to evaluate")
    eval_set: str = Field(description="Evaluation set identifier")
    thresholds: dict[str, float] = Field(
        description=(
            "Minimum acceptable value per metric (e.g., {'accuracy': 0.8, 'f1': 0.75}). "
            "A metric passes if its value >= the threshold."
        )
    )


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
    | CommandExecutedCriterion
    | UiPathEvalCriterion
)
