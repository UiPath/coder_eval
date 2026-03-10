"""Unified exports for all coder_eval data models.

All models can be imported from coder_eval.models regardless of
which submodule they're defined in.
"""

# SDK Types
from claude_agent_sdk import SdkPluginConfig

# Enums
# Criteria
from coder_eval.models.criteria import (
    BaseSuccessCriterion,
    CommandExecutedCriterion,
    FileCheckCriterion,
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    PylintScoreCriterion,
    PytestCriterion,
    ReferenceComparisonCriterion,
    RegexPattern,
    RunCommandCriterion,
    SuccessCriterion,
    UiPathEvalCriterion,
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
    NodeEnvConfig,
    PythonEnvConfig,
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
    # SDK Types
    "SdkPluginConfig",
    # Enums
    "AgentKind",
    "AgentState",
    "SnapshotMode",
    # Criteria
    "BaseSuccessCriterion",
    "FileExistsCriterion",
    "FileContainsCriterion",
    "FileCheckCriterion",
    "RegexPattern",
    "RunCommandCriterion",
    "PytestCriterion",
    "FileMatchesRegexCriterion",
    "PylintScoreCriterion",
    "ReferenceComparisonCriterion",
    "CommandExecutedCriterion",
    "UiPathEvalCriterion",
    "SuccessCriterion",
    # Templates
    "BaseTemplateSource",
    "RepoSource",
    "TemplateDirSource",
    "StarterFilesSource",
    "StarterFile",
    "TemplateSource",
    # Sandbox
    "NodeEnvConfig",
    "PythonEnvConfig",
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
