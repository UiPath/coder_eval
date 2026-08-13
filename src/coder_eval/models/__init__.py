"""Unified exports for all coder_eval data models.

All models can be imported from coder_eval.models regardless of
which submodule they're defined in.
"""

# Agent config
from coder_eval.models.agent_config import (
    AgentConfig,
    AntigravityAgentConfig,
    BaseAgentConfig,
    ClaudeCodeAgentConfig,
    CodexAgentConfig,
    LocalPluginConfig,
    NoneAgentConfig,
    ResolvedAgentConfig,
    parse_agent_config,
)

# Container paths (leaf constants; re-exported so consumers obey CE001)
from coder_eval.models.container_paths import (
    AGENT_GID,
    AGENT_HOME,
    AGENT_UID,
    CONTAINER_AGENT_WORK_DIR,
    CONTAINER_CLAUDE_SHIM,
    CONTAINER_DROP_SHIM,
    CONTAINER_GRADER_DIR,
    CONTAINER_INPUT_DIR,
    CONTAINER_OUTPUT_DIR,
    CONTAINER_TASK_DIR,
    CONTAINER_WORK_DIR,
    RESERVED_CONTAINER_DIRS,
)

# Enums
# Criteria
from coder_eval.models.criteria import (
    AgentJudgeCriterion,
    BaseSuccessCriterion,
    ClassificationMatchCriterion,
    CliCalledCriterion,
    CommandExecutedCriterion,
    CommandsEfficiencyCriterion,
    FileCheckCriterion,
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    FlagMatch,
    JMESPathAssertion,
    JsonCheckCriterion,
    LivePolarity,
    LiveSuccessCriterion,
    LLMJudgeCriterion,
    ReferenceComparisonCriterion,
    RegexPattern,
    RunCommandCriterion,
    SkillTriggeredCriterion,
    StopEarlyPolicy,
    SuccessCriterion,
    UiPathEvalCriterion,
)
from coder_eval.models.enums import (
    AgentKind,
    AgentState,
    ApiBackend,
    FinalStatus,
    PermissionMode,
    PreservationMode,
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

# Judge
from coder_eval.models.judge import JudgeVerdict

# Judge defaults
from coder_eval.models.judge_defaults import DEFAULT_JUDGE_MODEL

# Limits
from coder_eval.models.limits import DEFAULT_STOP_EARLY_GATE_THRESHOLD, RunLimits

# Merge strategy
from coder_eval.models.merge_strategy import (
    APPEND_ORDER_KEY,
    MERGE_STRATEGY_KEY,
    AppendOrder,
    MergeField,
    MergeStrategy,
    append_order_of,
    classify_annotation,
    merge_strategy_of,
)

# Mutations
from coder_eval.models.mutations import (
    PromptMutation,
    PromptPrefix,
    PromptReplace,
    PromptSuffix,
    PromptTemplate,
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
    CriterionResultUnion,
    CriterionStats,
    EarlyStopInfo,
    EarlyStopReason,
    EvaluationResult,
    FailedRowSummary,
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
    eval_overhead_cost,
    eval_result_total_cost,
    judge_cost_usd,
    row_cost_incomplete,
    simulator_cost_usd,
    sum_costs,
)

# Routing
from coder_eval.models.routing import (
    ROUTE_NAMES,
    ApiRoute,
    BedrockRoute,
    DirectRoute,
    JudgeTransport,
    LiteLLMRoute,
    resolve_evaluation_route,
    resolve_route,
    to_bedrock_inference_profile,
)

# Sandbox
from coder_eval.models.sandbox import (
    RECORD_CLI_DIR,
    RECORD_CLI_LOG,
    DockerBuildConfig,
    DockerDriverConfig,
    NodeEnvConfig,
    PythonEnvConfig,
    RecordedCli,
    ResourceLimits,
    SandboxConfig,
    validate_template_sources_list,
)

# Tasks
from coder_eval.models.tasks import (
    DEFAULT_SIMULATION_STOP_TOKEN,
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
    AssistantMessage,
    CommandStatistics,
    CommandTelemetry,
    ContentBlock,
    ProviderCallCost,
    ReconciliationMessage,
    SlowestCommandInfo,
    TokenUsage,
    TranscriptMessage,
    UserMessage,
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
    # Agent config
    "AgentConfig",
    "AntigravityAgentConfig",
    "BaseAgentConfig",
    "ClaudeCodeAgentConfig",
    "CodexAgentConfig",
    "LocalPluginConfig",
    "NoneAgentConfig",
    "ResolvedAgentConfig",
    "parse_agent_config",
    # Enums
    "AgentKind",
    "AgentState",
    "ApiBackend",
    "FinalStatus",
    "PermissionMode",
    "PreservationMode",
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
    "FileMatchesRegexCriterion",
    "ReferenceComparisonCriterion",
    "CommandExecutedCriterion",
    "CliCalledCriterion",
    "FlagMatch",
    "CommandsEfficiencyCriterion",
    "UiPathEvalCriterion",
    "LLMJudgeCriterion",
    "AgentJudgeCriterion",
    "SkillTriggeredCriterion",
    "StopEarlyPolicy",
    "LiveSuccessCriterion",
    "LivePolarity",
    "SuccessCriterion",
    # Routing
    "ROUTE_NAMES",
    "ApiRoute",
    "DirectRoute",
    "BedrockRoute",
    "LiteLLMRoute",
    "JudgeTransport",
    "resolve_route",
    "resolve_evaluation_route",
    "to_bedrock_inference_profile",
    # Templates
    "BaseTemplateSource",
    "RepoSource",
    "TemplateDirSource",
    "StarterFilesSource",
    "StarterFile",
    "TemplateSource",
    # Sandbox
    "DockerBuildConfig",
    "AGENT_GID",
    "AGENT_HOME",
    "AGENT_UID",
    "CONTAINER_AGENT_WORK_DIR",
    "CONTAINER_CLAUDE_SHIM",
    "CONTAINER_DROP_SHIM",
    "CONTAINER_GRADER_DIR",
    "CONTAINER_INPUT_DIR",
    "CONTAINER_OUTPUT_DIR",
    "CONTAINER_TASK_DIR",
    "CONTAINER_WORK_DIR",
    "RESERVED_CONTAINER_DIRS",
    "DockerDriverConfig",
    "NodeEnvConfig",
    "PythonEnvConfig",
    "SandboxConfig",
    "RecordedCli",
    "RECORD_CLI_DIR",
    "RECORD_CLI_LOG",
    "ResourceLimits",
    "validate_template_sources_list",
    # Telemetry
    "AssistantMessage",
    "CommandTelemetry",
    "CommandStatistics",
    "ContentBlock",
    "ProviderCallCost",
    "ReconciliationMessage",
    "SlowestCommandInfo",
    "TokenUsage",
    "TranscriptMessage",
    "UserMessage",
    # Results
    "ClassificationCriterionResult",
    "ClassLabelStats",
    "ConfigLineageEntry",
    "ConfusionEntry",
    "CriterionAggregate",
    "CriterionResult",
    "CriterionResultUnion",
    "CriterionStats",
    "FailedRowSummary",
    "ThresholdCheck",
    "JudgeCriterionResult",
    "JudgeTranscript",
    "JudgeTranscriptToolCall",
    "PostRunResult",
    "ResultSummary",
    "TurnRecord",
    "EarlyStopInfo",
    "EarlyStopReason",
    "EvaluationResult",
    "SimulationTelemetry",
    "SuiteRollup",
    "TaskConfigRecord",
    "RunSummary",
    "SkippedTask",
    # Cost helpers, shared by RunSummary's computed fields and the reports so
    # every surface agrees on what a total costs and which rows lost money.
    "row_cost_incomplete",
    "eval_overhead_cost",
    "sum_costs",
    "eval_result_total_cost",
    "judge_cost_usd",
    "simulator_cost_usd",
    # Judge defaults
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_STOP_EARLY_GATE_THRESHOLD",
    # Judge
    "JudgeVerdict",
    # Limits
    "RunLimits",
    # Merge strategy
    "MERGE_STRATEGY_KEY",
    "APPEND_ORDER_KEY",
    "AppendOrder",
    "MergeField",
    "MergeStrategy",
    "append_order_of",
    "classify_annotation",
    "merge_strategy_of",
    # Tasks
    "TaskDefinition",
    "DEFAULT_SIMULATION_STOP_TOKEN",
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
    "PromptMutation",
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
# Typed as the discriminated union so callers that iterate the result list
# and use ``isinstance(cr, JudgeCriterionResult)`` get the precise variant
# membership. The runtime objects are concrete subclasses regardless; the
# alias change is purely a type-checking precision fix that mirrors the
# ``EvaluationResult.success_criteria_results`` field type.
type CriteriaResults = list[CriterionResultUnion]
type SuccessCriteria = list[SuccessCriterion]
type TurnRecords = list[TurnRecord]
