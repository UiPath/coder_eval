"""Task definition and configuration models."""

from __future__ import annotations

import re
import warnings
from typing import Any, Literal, Self

from claude_agent_sdk import SdkPluginConfig, SettingSource
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from coder_eval.models.criteria import SuccessCriterion
from coder_eval.models.enums import AgentKind
from coder_eval.models.limits import RunLimits
from coder_eval.models.sandbox import SandboxConfig


class AgentConfig(BaseModel):
    """Configuration for the coding agent."""

    model_config = ConfigDict(validate_assignment=True, populate_by_name=True, extra="forbid")

    type: AgentKind | None = Field(
        default=None,
        description=(
            "The type of agent to use (claude-code, aider, etc.). "
            "May be omitted on the task and supplied via experiment defaults or --type."
        ),
    )
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
    plugins: list[SdkPluginConfig] | None = Field(default=None, description="List of Claude Code plugins")

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
    """Defines the source for the reference solution.

    The reference is NEVER shown to the agent being evaluated. It is used by:

    - ``LLMJudgeCriterion``: inlines the reference content into the judge prompt
      (string forms only — ``code`` or ``file``).
    - ``ReferenceComparisonCriterion``: computes AST/token similarity (string forms only).
    - ``AgentJudgeCriterion``: with ``include_reference=true``, the reference is
      copied into the judge sub-agent's working directory (single file inlined
      into the prompt for ``code``/``file``; ``directory`` is mounted at
      ``_reference/`` for the judge to ``Glob``/``Read`` over).

    Exactly one of ``code``, ``file``, or ``directory`` must be provided.
    Security: reference solutions must never leak into agent prompts or logs.
    """

    code: str | None = Field(default=None, description="Inline reference code (for simple, short solutions)")
    file: str | None = Field(default=None, description="Path to file containing reference code (relative to task YAML)")
    directory: str | None = Field(
        default=None,
        description=(
            "Path to a directory containing the reference solution (relative to task YAML). "
            "Only consumed by agent_judge — the judge gets a read-only copy at "
            "``_reference/`` in its working directory and can browse it with Glob/Read. "
            "llm_judge and reference_comparison only accept string forms (code/file)."
        ),
    )

    @model_validator(mode="after")
    def check_exclusive_source(self) -> Self:
        """Ensure exactly one of code / file / directory is provided."""
        provided = sum(1 for v in (self.code, self.file, self.directory) if v is not None)
        if provided > 1:
            raise ValueError("Only one of 'code', 'file', or 'directory' can be provided for reference.")
        if provided == 0:
            raise ValueError("One of 'code', 'file', or 'directory' must be provided for reference.")
        return self


class Dataset(BaseModel):
    """Dataset that fans out a single task into N sub-tasks, one per row.

    Exactly one of ``rows`` (inline list of dicts) or ``paths`` (one or more
    JSONL files concatenated in declared order) must be provided. Each row
    must contain the field named by ``id_field``; that value is used as the
    stable row identifier and becomes a suffix on the task_id
    ("<task_id>/<row.id>").

    Row values are substituted into the task's ``initial_prompt`` and into
    string fields of each ``success_criteria`` entry using ``${row.<field>}``
    syntax. Substitution happens in ``task_loader.expand_dataset`` before
    variant resolution, so variants cannot override the dataset.
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] | None = Field(
        default=None,
        description=(
            "JSONL file paths (relative to the task YAML), concatenated into a single "
            "row stream in declared order. Mutually exclusive with 'rows'."
        ),
    )
    rows: list[dict[str, Any]] | None = Field(
        default=None,
        description="Inline list of row dicts. Mutually exclusive with 'paths'.",
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
        if self.paths is None and self.rows is None:
            raise ValueError("Dataset must specify either 'paths' or 'rows'")
        if self.paths is not None and self.rows is not None:
            raise ValueError("Dataset must specify only one of 'paths' or 'rows'")
        if self.paths is not None and not self.paths:
            raise ValueError("Dataset.paths must be a non-empty list")
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


class PreRunCommand(BaseModel):
    """A command to execute before agent evaluation starts.

    Pre-run commands run inside the sandbox after setup completes but before
    the agent starts. By default (fail_on_error=True), a non-zero exit code,
    timeout, or exception aborts the evaluation with FinalStatus.ERROR — the
    agent should not run against a broken environment.
    """

    model_config = ConfigDict(extra="forbid")

    command: str = Field(description="Shell command to execute (run via shell, supports pipes/redirects)")
    timeout: int = Field(default=30, ge=1, le=300, description="Maximum seconds to wait for the command to complete")
    fail_on_error: bool = Field(
        default=True,
        description=(
            "When True (default), a non-zero exit code, timeout, or exception aborts the evaluation "
            "with FinalStatus.ERROR. Set to False for optional/informational setup commands."
        ),
    )


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
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Tags for categorizing and filtering tasks. Each tag is kebab-case, optionally namespaced "
            "as 'key:value' where both key and value are kebab-case (e.g., 'smoke', 'lifecycle:generate')."
        ),
    )
    skip: bool = Field(
        default=False,
        description=(
            "When true, the task is recorded in RunSummary.skipped_tasks at resolution time and "
            "never reaches the orchestrator. Use to quarantine known-blocked tasks without deleting "
            "the YAML — pair with a comment citing the blocker (e.g. Jira link, upstream dependency)."
        ),
    )
    agent: AgentConfig | None = Field(
        default=None, description="Agent configuration (resolved from experiment if omitted)"
    )
    sandbox: SandboxConfig = Field(description="Sandbox configuration")
    success_criteria: list[SuccessCriterion] = Field(description="List of criteria that must all pass for task success")
    max_turns: int | None = Field(
        default=None,
        gt=0,
        description="Maximum agent inner-loop turns per iteration. None = SDK default.",
    )
    turn_timeout: int | None = Field(
        default=None,
        ge=10,
        description="Maximum seconds per agent communicate() call. None = no limit.",
    )
    task_timeout: int | None = Field(
        default=None,
        ge=30,
        description="Maximum seconds for the entire evaluation loop (all iterations). None = no limit.",
    )
    run_limits: RunLimits | None = Field(
        default=None,
        description=(
            "Budget caps (tokens/USD) that abort the task when exceeded. "
            "Checked after each completed agent turn. See RunLimits."
        ),
    )
    reference: ReferenceSource | None = Field(
        default=None,
        description=(
            "Reference solution for llm_judge / agent_judge and reference_comparison criteria. "
            "HIDDEN from the agent - never included in prompts."
        ),
    )
    expected_commands: int | None = Field(
        default=None,
        ge=1,
        description="Expected number of tool commands for orchestrator-level efficiency tracking",
    )
    pre_run: list[PreRunCommand] = Field(
        default_factory=list,
        description=(
            "Commands to execute before agent evaluation starts. "
            "By default a failing command aborts evaluation with FinalStatus.ERROR. "
            "Set fail_on_error=False per command to make it informational."
        ),
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

    @model_validator(mode="before")
    @classmethod
    def _hoist_legacy_agent_timing(cls, data: Any) -> Any:
        """Back-compat: lift max_turns/turn_timeout from agent: to top level.

        Scheduled removal: 2026-05-15. See plan
        c/2026-05-07-move-agent-timing-to-task.md.
        """
        if not isinstance(data, dict):
            return data
        agent = data.get("agent")
        if not isinstance(agent, dict):
            return data
        for field in ("max_turns", "turn_timeout"):
            if field in agent:
                if data.get(field) is not None:
                    raise ValueError(
                        f"{field!r} set both at top level and under agent: — "
                        + "remove the one under agent: (deprecated location)."
                    )
                data[field] = agent.pop(field)
                warnings.warn(
                    f"{field!r} under agent: is deprecated and will be removed on 2026-05-15; "
                    + "move it to the top level of the task definition.",
                    DeprecationWarning,
                    stacklevel=2,
                )
        return data

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
    def check_directory_reference_compatibility(self) -> Self:
        """Reject ``reference.directory`` paired with criteria that need a string reference.

        ``reference_comparison`` and ``llm_judge`` only consume ``reference_code``
        (the string forms ``code`` / ``file``). When ``reference.directory`` is
        the only form set, those criteria silently degrade — ``reference_comparison``
        deterministically scores 0.0 ("No reference code provided") and
        ``llm_judge`` runs without the reference even when ``include_reference=True``.
        Catch the misconfiguration at load time instead of at run time.
        """
        if self.reference is None or self.reference.directory is None:
            return self
        offenders: list[str] = []
        for c in self.success_criteria:
            ctype = getattr(c, "type", None)
            if ctype == "reference_comparison":
                offenders.append("reference_comparison")
            elif ctype == "llm_judge" and getattr(c, "include_reference", False):
                offenders.append("llm_judge (include_reference=true)")
        if offenders:
            raise ValueError(
                "reference.directory is only consumed by agent_judge; the following "
                f"criteria require a string reference (use 'code' or 'file' instead): {offenders}"
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
