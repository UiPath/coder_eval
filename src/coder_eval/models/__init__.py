"""Unified exports for all coder_eval data models.

All models can be imported from coder_eval.models regardless of
which submodule they're defined in.
"""

# SDK Types
from claude_agent_sdk import SdkPluginConfig

# Enums
# Criteria
from coder_eval.models.criteria import (
    AgentJudgeCriterion,
    BaseSuccessCriterion,
    ClassificationMatchCriterion,
    CommandExecutedCriterion,
    CommandsEfficiencyCriterion,
    FileCheckCriterion,
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    ImportCheckCriterion,
    JMESPathAssertion,
    JsonCheckCriterion,
    LLMJudgeCriterion,
    PylintScoreCriterion,
    PytestCriterion,
    ReferenceComparisonCriterion,
    RegexPattern,
    RunCommandCriterion,
    SkillTriggeredCriterion,
    SuccessCriterion,
    UiPathEvalCriterion,
)
from coder_eval.models.enums import (
    AgentKind,
    AgentState,
    ApiBackend,
    FinalStatus,
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

# Gateway
from coder_eval.models.gateway import DEFAULT_GATEWAY_MODEL

# Judge
from coder_eval.models.judge import JudgeVerdict

# Limits
from coder_eval.models.limits import RunLimits

# Mutations
from coder_eval.models.mutations import (
    PromptMutation,
    PromptPrefix,
    PromptRephrase,
    PromptReplace,
    PromptSuffix,
    PromptTemplate,
    RephraseFn,
    apply_prompt_mutations,
)

# Results
from coder_eval.models.results import (
    ClassificationCriterionResult,
    ClassLabelStats,
    ConfigLineageEntry,
    ConfusionEntry,
    CriterionAggregate,
    CriterionResult,
    CriterionStats,
    EvaluationResult,
    FailedRowSummary,
    FileChange,
    JudgeCriterionResult,
    JudgeTranscript,
    JudgeTranscriptToolCall,
    PostRunResult,
    ResultSummary,
    RunSummary,
    SimulationTelemetry,
    SkippedTask,
    SuiteRollup,
    TaskConfigRecord,
    ThresholdCheck,
    TurnRecord,
)

# Routing
from coder_eval.models.routing import (
    ROUTE_NAMES,
    ApiRoute,
    BedrockRoute,
    DirectRoute,
    ProxyRoute,
    proxy_config_from_settings,
    resolve_route,
    to_bedrock_inference_profile,
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
    DEFAULT_SIMULATION_STOP_TOKEN,
    AgentConfig,
    CriteriaCheckTiming,
    Dataset,
    PostRunCommand,
    PreRunCommand,
    ReferenceSource,
    SimulationConfig,
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
    "ApiBackend",
    "FinalStatus",
    "SnapshotMode",
    # Criteria
    "BaseSuccessCriterion",
    "ClassificationMatchCriterion",
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
    "LLMJudgeCriterion",
    "AgentJudgeCriterion",
    "SkillTriggeredCriterion",
    "SuccessCriterion",
    # Routing
    "ROUTE_NAMES",
    "ApiRoute",
    "DirectRoute",
    "ProxyRoute",
    "BedrockRoute",
    "resolve_route",
    "proxy_config_from_settings",
    "to_bedrock_inference_profile",
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
    "ClassificationCriterionResult",
    "ClassLabelStats",
    "ConfigLineageEntry",
    "ConfusionEntry",
    "CriterionAggregate",
    "CriterionResult",
    "CriterionStats",
    "FailedRowSummary",
    "ThresholdCheck",
    "FileChange",
    "JudgeCriterionResult",
    "JudgeTranscript",
    "JudgeTranscriptToolCall",
    "PostRunResult",
    "ResultSummary",
    "TurnRecord",
    "EvaluationResult",
    "SimulationTelemetry",
    "SuiteRollup",
    "TaskConfigRecord",
    "RunSummary",
    "SkippedTask",
    # Gateway
    "DEFAULT_GATEWAY_MODEL",
    # Judge
    "JudgeVerdict",
    # Limits
    "RunLimits",
    # Tasks
    "TaskDefinition",
    "DEFAULT_SIMULATION_STOP_TOKEN",
    "AgentConfig",
    "CriteriaCheckTiming",
    "Dataset",
    "PostRunCommand",
    "PreRunCommand",
    "ReferenceSource",
    "SimulationConfig",
    # Mutations
    "PromptPrefix",
    "PromptSuffix",
    "PromptReplace",
    "PromptTemplate",
    "PromptRephrase",
    "PromptMutation",
    "RephraseFn",
    "apply_prompt_mutations",
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
