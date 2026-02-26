"""Unified exports for all coder_eval data models.

All models can be imported from coder_eval.models regardless of
which submodule they're defined in.
"""

# Enums
# Criteria (all 9 + union + base)
from coder_eval.models.criteria import (
    BaseSuccessCriterion,
    CodeLintsCriterion,
    CommandExecutedCriterion,
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    ProgramStdoutEqualsCriterion,
    PylintScoreCriterion,
    PytestCriterion,
    ReferenceComparisonCriterion,
    RunCommandCriterion,
    SuccessCriterion,
)
from coder_eval.models.enums import (
    AgentKind,
    AgentState,
    SnapshotMode,
)

# Results
from coder_eval.models.results import (
    CriterionResult,
    EvaluationResult,
    FileChange,
    LLMDecision,
    RunSummary,
    TurnRecord,
)

# Sandbox
from coder_eval.models.sandbox import (
    ResourceLimits,
    SandboxConfig,
    SnapshotConfig,
    SnapshotManifest,
)

# Tasks
from coder_eval.models.tasks import (
    AgentConfig,
    LLMReviewerConfig,
    ReferenceSource,
    TaskDefinition,
)

# Telemetry
from coder_eval.models.telemetry import (
    CommandStatistics,
    CommandTelemetry,
    SlowestCommandInfo,
    TokenUsage,
)

# Templates
from coder_eval.models.templates import (
    BaseTemplateSource,
    RepoSource,
    StarterFile,
    StarterFilesSource,
    TemplateDirSource,
    TemplateSource,
)


__all__ = [  # noqa: RUF022 - Keep grouped by category for readability
    # Enums
    "AgentKind",
    "AgentState",
    "SnapshotMode",
    # Criteria
    "BaseSuccessCriterion",
    "FileExistsCriterion",
    "FileContainsCriterion",
    "RunCommandCriterion",
    "ProgramStdoutEqualsCriterion",
    "PytestCriterion",
    "FileMatchesRegexCriterion",
    "CodeLintsCriterion",
    "PylintScoreCriterion",
    "ReferenceComparisonCriterion",
    "CommandExecutedCriterion",
    "SuccessCriterion",
    # Templates
    "BaseTemplateSource",
    "RepoSource",
    "TemplateDirSource",
    "StarterFilesSource",
    "StarterFile",
    "TemplateSource",
    # Sandbox
    "SandboxConfig",
    "SnapshotConfig",
    "SnapshotManifest",
    "ResourceLimits",
    # Telemetry
    "CommandTelemetry",
    "CommandStatistics",
    "SlowestCommandInfo",
    "TokenUsage",
    # Results
    "CriterionResult",
    "LLMDecision",
    "FileChange",
    "TurnRecord",
    "EvaluationResult",
    "RunSummary",
    # Tasks
    "TaskDefinition",
    "AgentConfig",
    "LLMReviewerConfig",
    "ReferenceSource",
]

# Type aliases (forward compatible)
type CriteriaResults = list[CriterionResult]
type SuccessCriteria = list[SuccessCriterion]
type FileTree = dict[str, float]  # path -> modification time
type FileChanges = list[FileChange]
type TurnRecords = list[TurnRecord]
