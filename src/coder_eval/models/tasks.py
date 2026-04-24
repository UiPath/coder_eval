"""Task definition and configuration models."""

from __future__ import annotations

import re
from typing import Any, Literal, Self

from claude_agent_sdk import SdkPluginConfig, SettingSource
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from coder_eval.models.criteria import SuccessCriterion
from coder_eval.models.enums import AgentKind
from coder_eval.models.gateway import DEFAULT_GATEWAY_MODEL
from coder_eval.models.sandbox import SandboxConfig


class AgentConfig(BaseModel):
    """Configuration for the coding agent."""

    model_config = ConfigDict(validate_assignment=True, populate_by_name=True, extra="forbid")

    type: AgentKind = Field(description="The type of agent to use (claude-code, aider, etc.)")
    permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions"] = Field(
        default="acceptEdits", description="Permission mode for agent actions"
    )
    allowed_tools: list[str] | None = Field(
        default=None, description="List of allowed tools (e.g., ['Read', 'Write', 'Bash'])"
    )
    disallowed_tools: list[str] | None = Field(
        default=None, description="List of disallowed tools (e.g., ['TodoWrite'])"
    )
    model: str | None = Field(default=None, description="Specific model to use (if applicable)")
    max_turns: int | None = Field(default=None, description="Maximum agent inner-loop turns per iteration")
    plugins: list[SdkPluginConfig] | None = Field(default=None, description="List of Claude Code plugins")

    turn_timeout: int | None = Field(
        default=None,
        ge=10,
        description="Maximum seconds per agent turn (communicate call). None = no limit.",
    )

    # Customizable ignore patterns for file tracking
    ignore_patterns: list[str] = Field(
        default_factory=list,
        description="Additional patterns to ignore during file change detection (beyond defaults)",
        validation_alias=AliasChoices("ignore_patterns", "additional_ignore_patterns"),
    )

    system_prompt: str | None = Field(
        default=None,
        description=(
            "Custom system prompt injected into the Claude Code agent. "
            "Replaces the default system prompt. Supports inline text or multi-line YAML strings. "
            "Mutually exclusive with system_prompt_file."
        ),
    )
    system_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to a file containing the system prompt (relative to task YAML). "
            "The file contents are loaded at task resolution time and set as system_prompt. "
            "Mutually exclusive with system_prompt."
        ),
    )
    setting_sources: list[SettingSource] | None = Field(
        default=None,
        description=(
            "Claude Code setting sources to load (e.g., ['project', 'user']). "
            "Defaults to ['project'] so .mcp.json is discovered. Set to [] to disable all settings. "
            "None means use the framework default (['project'])."
        ),
    )
    claude_settings: str | dict[str, Any] | None = Field(
        default=None,
        description=(
            "Claude Code settings passed via --settings. Accepts a JSON-serializable dict "
            "(inlined) or a file path string. Use permissions.deny to block tool access to "
            'specific paths: {"permissions": {"deny": ["Read(/some/path/**)"]}}.'
        ),
    )

    @model_validator(mode="after")
    def check_prompt_exclusivity(self) -> Self:
        """Ensure system_prompt and system_prompt_file are mutually exclusive."""
        if self.system_prompt is not None and self.system_prompt_file is not None:
            raise ValueError("Only one of 'system_prompt' or 'system_prompt_file' can be provided, not both")
        return self


class LLMReviewerConfig(BaseModel):
    """Configuration for the LLM-based qualitative reviewer.

    All models are accessed through UiPath LLM Gateway using LangChain integration.
    Use Gateway model naming convention (e.g., anthropic.claude-3-5-sonnet-20240620-v1:0, gpt-4o-2024-08-06).
    """

    enabled: bool = Field(default=False, description="Whether to enable LLM review")
    model: str = Field(
        default=DEFAULT_GATEWAY_MODEL,
        description="Gateway model name (e.g., anthropic.claude-3-5-sonnet-20240620-v1:0, gpt-4o-2024-08-06)",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="Temperature for LLM sampling")
    max_tokens: int = Field(default=1000, gt=0, description="Maximum tokens in response")
    prompt: str | None = Field(
        default=None,
        description="Task-specific review instructions appended to the review prompt",
    )


DEFAULT_SIMULATION_STOP_TOKEN = "<<<END>>>"
"""Sentinel token the user simulator emits when it considers the task complete."""


CriteriaCheckTiming = Literal["end_of_dialog", "every_turn", "both"]
"""When success criteria are evaluated inside a simulated dialog."""


class SimulationConfig(BaseModel):
    """Configuration for multi-turn user simulation.

    When present on a TaskDefinition, the orchestrator replaces its single-shot
    iteration loop with a dialog between the coding agent and a simulated user
    (a second LLM). The simulator is configured with a persona, a goal, and
    optional behavioral constraints; it generates each subsequent user prompt
    after the first (which is still the task's ``initial_prompt``).

    Semantics relative to the existing task fields:
      - ``max_iterations`` keeps its meaning (number of task *attempts*).
        In simulation mode it typically stays at 1: retrying a stochastic
        dialog rarely adds signal. Use ``n_trials`` for variance sampling.
      - ``max_turns`` (below) bounds the intra-dialog agent<->user exchanges.

    Security: ``persona``, ``goal``, and ``constraints`` are passed to the
    simulator only. The task's ``reference`` is NEVER passed to the simulator,
    mirroring the guarantee already in place for the coding agent.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Master switch — when false, simulation is skipped entirely.")

    # The simulator runs as a tools-disabled Claude Code agent sharing the
    # coding agent's ApiRoute, so model/temperature/max_tokens are resolved at
    # the route level and are not configured here.

    # Persona / goal.
    persona: str = Field(
        description=(
            "Who the simulator is roleplaying — background, tone, level of domain knowledge. "
            "Passed verbatim into the simulator system prompt."
        ),
    )
    goal: str = Field(
        description=(
            "What the simulated user wants to achieve. This drives the conversation. "
            "You can optionally withhold information (see ``constraints``) to force clarification behavior."
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Optional behavioral rules the simulator must follow "
            "(e.g., 'do not paste code', 'reveal requirement X only if asked')."
        ),
    )

    # Termination.
    max_turns: int = Field(
        default=8,
        ge=1,
        le=100,
        description="Hard cap on simulated user<->agent exchanges within a single dialog.",
    )
    stop_token: str = Field(
        default=DEFAULT_SIMULATION_STOP_TOKEN,
        min_length=1,
        description="If the simulator emits a message containing this token, the dialog ends.",
    )
    stop_on_criteria_pass: bool = Field(
        default=False,
        description=(
            "When True, end the dialog as soon as all success criteria pass. "
            "Requires ``check_criteria: every_turn`` or ``both``."
        ),
    )
    max_total_tokens: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional budget across the whole dialog (sum of simulator + agent tokens). "
            "When exceeded, the dialog terminates with stop_reason='budget'."
        ),
    )

    # Sampling.
    n_trials: int = Field(
        default=1,
        ge=1,
        le=100,
        description="Number of independent dialog trajectories to run per (task, variant).",
    )
    parallel_trials: bool = Field(
        default=True,
        description="When True, trials run concurrently (subject to batch max_parallel).",
    )

    # Criteria timing.
    check_criteria: CriteriaCheckTiming = Field(
        default="end_of_dialog",
        description=(
            "When to evaluate success criteria: end_of_dialog (cheapest), "
            "every_turn (enables early stop), or both (record every turn AND at end)."
        ),
    )

    @field_validator("persona", "goal")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _validate_timing(self) -> Self:
        if self.stop_on_criteria_pass and self.check_criteria == "end_of_dialog":
            raise ValueError(
                "simulation.stop_on_criteria_pass=True requires check_criteria='every_turn' or 'both' — "
                + "set check_criteria accordingly, or disable stop_on_criteria_pass."
            )
        return self


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
    def check_exclusive_source(self) -> Self:
        """Ensure exactly one source is provided."""
        if self.code is not None and self.file is not None:
            raise ValueError("Only one of 'code' or 'file' can be provided for reference code.")
        if self.code is None and self.file is None:
            raise ValueError("One of 'code' or 'file' must be provided for reference code.")
        return self


class Dataset(BaseModel):
    """Dataset that fans out a single task into N sub-tasks, one per row.

    Exactly one of ``rows`` (inline list of dicts) or ``path`` (JSONL file
    relative to the task YAML) must be provided. Each row must contain the
    field named by ``id_field``; that value is used as the stable row
    identifier and becomes a suffix on the task_id ("<task_id>/<row.id>").

    Row values are substituted into the task's ``initial_prompt`` and into
    string fields of each ``success_criteria`` entry using ``${row.<field>}``
    syntax. Substitution happens in ``task_loader.expand_dataset`` before
    variant resolution, so variants cannot override the dataset.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(
        default=None,
        description="Path to a JSONL file (one JSON object per line), relative to the task YAML.",
    )
    rows: list[dict[str, Any]] | None = Field(
        default=None,
        description="Inline list of row dicts. Mutually exclusive with 'path'.",
    )
    id_field: str = Field(
        default="id",
        description="Field in each row to use as the row identifier (default: 'id').",
    )
    sample: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Task-level default: use only the first N rows. Overridden by CLI '--sample' when provided. "
            "Useful for committing a cheap-smoke default while still allowing full runs on demand."
        ),
    )

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if self.path is None and self.rows is None:
            raise ValueError("Dataset must specify either 'path' or 'rows'")
        if self.path is not None and self.rows is not None:
            raise ValueError("Dataset must specify only one of 'path' or 'rows', not both")
        return self


class PostRunCommand(BaseModel):
    """A command to execute after evaluation completes.

    Post-run commands run inside the sandbox after the evaluation verdict is finalized.
    They do NOT affect pass/fail status — they are for artifact generation, data extraction,
    cleanup, or any side effects needed after the run.
    """

    model_config = ConfigDict(extra="forbid")

    command: str = Field(description="Shell command to execute (run via shell, supports pipes/redirects)")
    timeout: int = Field(default=30, ge=1, le=300, description="Maximum seconds to wait for the command to complete")


class TaskDefinition(BaseModel):
    """Complete definition of an evaluation task."""

    task_id: str = Field(description="Unique identifier for this task")
    description: str = Field(description="Human-readable description of what the task is testing")
    initial_prompt: str | None = Field(
        default=None,
        description="The initial prompt to send to the agent. Mutually exclusive with initial_prompt_file.",
    )
    initial_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to a file containing the initial prompt (relative to task YAML). "
            "Mutually exclusive with initial_prompt."
        ),
    )
    max_iterations: int = Field(default=3, description="Maximum number of agent turns")
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Tags for categorizing and filtering tasks. Each tag is kebab-case, optionally namespaced "
            "as 'key:value' where both key and value are kebab-case (e.g., 'smoke', 'lifecycle:generate')."
        ),
    )
    agent: AgentConfig | None = Field(
        default=None, description="Agent configuration (resolved from experiment if omitted)"
    )
    sandbox: SandboxConfig = Field(description="Sandbox configuration")
    success_criteria: list[SuccessCriterion] = Field(description="List of criteria that must all pass for task success")
    task_timeout: int | None = Field(
        default=None,
        ge=30,
        description="Maximum seconds for the entire evaluation loop (all iterations). None = no limit.",
    )
    llm_reviewer: LLMReviewerConfig = Field(
        default_factory=LLMReviewerConfig, description="Optional LLM reviewer configuration"
    )
    reference: ReferenceSource | None = Field(
        default=None,
        description=(
            "Reference solution for LLM review and code comparison. HIDDEN from the agent - never included in prompts."
        ),
    )
    expected_commands: int | None = Field(
        default=None,
        ge=1,
        description="Expected number of tool commands for orchestrator-level efficiency tracking",
    )
    post_run: list[PostRunCommand] = Field(
        default_factory=list,
        description="Commands to execute after evaluation completes. Do not affect pass/fail.",
    )
    dataset: Dataset | None = Field(
        default=None,
        description=(
            "Optional dataset to fan out this task into one sub-task per row. "
            "Row values substitute into initial_prompt and success_criteria via ${row.<field>}. "
            "Expansion happens before variant resolution, so variants cannot override the dataset."
        ),
    )
    suite_id: str | None = Field(
        default=None,
        description=(
            "Set by the dataset expander on expanded row-tasks to the original task_id. "
            "Signal for suite-level pass-rate rollup reporting."
        ),
    )
    row_id: str | None = Field(
        default=None,
        description="Set by the dataset expander on expanded row-tasks to the value from Dataset.id_field.",
    )
    simulation: SimulationConfig | None = Field(
        default=None,
        description=(
            "Optional multi-turn user-simulation config. When present and enabled, the orchestrator runs "
            "the task as a dialog between the coding agent and a simulated user LLM instead of "
            "the default single-shot iteration loop."
        ),
    )

    @model_validator(mode="after")
    def check_prompt_fields(self) -> Self:
        """Validate initial_prompt / initial_prompt_file combination.

        - initial_prompt and initial_prompt_file are always mutually exclusive.
        - Exactly one is required for single-shot tasks.
        - In simulation mode (``simulation.enabled=True``) both may be omitted;
          the simulator then generates the opening user utterance from its
          persona and goal.
        """
        if self.initial_prompt is not None and self.initial_prompt_file is not None:
            raise ValueError("Only one of 'initial_prompt' or 'initial_prompt_file' can be provided, not both")
        in_simulation = self.simulation is not None and self.simulation.enabled
        if self.initial_prompt is None and self.initial_prompt_file is None and not in_simulation:
            raise ValueError(
                "Either 'initial_prompt' or 'initial_prompt_file' must be provided "
                + "(unless 'simulation.enabled' is true, in which case the simulator generates the opener)"
            )
        return self

    @model_validator(mode="after")
    def check_suite_thresholds_require_dataset(self) -> Self:
        """Across-row suite_thresholds only make sense for dataset-backed tasks.

        Skipped for expanded row-tasks (``suite_id`` set), which have dataset
        cleared by design — the original parent task already passed this check.
        """
        if self.dataset is None and self.suite_id is None:
            for c in self.success_criteria:
                if getattr(c, "suite_thresholds", None):
                    raise ValueError(
                        f"success_criteria[{c.type!r}].suite_thresholds requires a dataset: block "
                        + "(thresholds are evaluated on aggregated across-row metrics)"
                    )
        return self

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str]) -> list[str]:
        """Validate tags are kebab-case, optionally namespaced as 'key:value'.

        Each side of an optional single colon must be lowercase kebab-case (a-z0-9, hyphens
        between segments). Examples: 'smoke', 'uipath-python', 'lifecycle:generate',
        'connector:google-tasks'. Rejected: leading/trailing colons, multiple colons,
        underscores, uppercase, spaces.
        """
        tag_pattern = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*(:[a-z0-9]+(-[a-z0-9]+)*)?$")
        for tag in v:
            if not tag_pattern.match(tag):
                msg = (
                    f"Tag '{tag}' must be lowercase kebab-case, optionally namespaced as "
                    "'key:value' (e.g., 'smoke', 'uipath-python', 'lifecycle:generate')"
                )
                raise ValueError(msg)
        return v

    @field_validator("success_criteria", mode="before")
    @classmethod
    def check_removed_criteria_types(cls, v: Any) -> Any:
        """Provide helpful errors for criterion types removed in the consolidation."""
        removed: dict[str, str] = {
            "program_stdout_equals": "Use 'run_command' with 'expected_stdout' and 'stdout_match' instead.",
            "code_lints": "Use 'run_command' to run your linter directly instead.",
            "scored_command": "Use 'run_command' with 'score_from_stdout: true' instead.",
        }
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    ctype = item.get("type")
                    if ctype in removed:
                        raise ValueError(f"Criterion type '{ctype}' has been removed. {removed[ctype]}")
        return v

    @field_validator("success_criteria")
    @classmethod
    def validate_success_criteria(cls, v: Any) -> Any:
        """Ensure at least one success criterion is defined."""
        if not v:
            raise ValueError("At least one success criterion must be defined")
        return v
