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
    CommandsEfficiencyCriterion,
    FileCheckCriterion,
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    ImportCheckCriterion,
    JMESPathAssertion,
    JsonCheckCriterion,
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

# Experiment
from coder_eval.models.experiment import (
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentResult,
    ExperimentVariant,
    ResolvedTask,
    TaskExperimentSummary,
    TaskResult,
    VariantAggregate,
    VariantResult,
)

# Results
from coder_eval.models.results import (
    ConfigLineageEntry,
    CriterionResult,
    EvaluationResult,
    FileChange,
    LLMDecision,
    RunSummary,
    TaskConfigRecord,
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
    validate_template_sources_list,
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
    "JMESPathAssertion",
    "JsonCheckCriterion",
    "RegexPattern",
    "RunCommandCriterion",
    "PytestCriterion",
    "FileMatchesRegexCriterion",
    "PylintScoreCriterion",
    "ReferenceComparisonCriterion",
    "CommandExecutedCriterion",
    "CommandsEfficiencyCriterion",
    "UiPathEvalCriterion",
    "ImportCheckCriterion",
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
    "validate_template_sources_list",
    # Telemetry
    "CommandTelemetry",
    "CommandStatistics",
    "SlowestCommandInfo",
    "TokenUsage",
    # Results
    "ConfigLineageEntry",
    "CriterionResult",
    "LLMDecision",
    "FileChange",
    "TurnRecord",
    "EvaluationResult",
    "TaskConfigRecord",
    "RunSummary",
    # Tasks
    "TaskDefinition",
    "AgentConfig",
    "LLMReviewerConfig",
    "ReferenceSource",
    # Experiment
    "ExperimentDefaults",
    "ExperimentDefinition",
    "ExperimentResult",
    "ExperimentVariant",
    "ResolvedTask",
    "TaskExperimentSummary",
    "TaskResult",
    "VariantAggregate",
    "VariantResult",
]

# Type aliases (forward compatible)
type CriteriaResults = list[CriterionResult]
type SuccessCriteria = list[SuccessCriterion]
type FileTree = dict[str, float]  # path -> modification time
type FileChanges = list[FileChange]
type TurnRecords = list[TurnRecord]
