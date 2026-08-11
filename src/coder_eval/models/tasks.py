"""Task definition and configuration models."""

from __future__ import annotations

import re
import warnings
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coder_eval.models.agent_config import ResolvedAgentConfig
from coder_eval.models.container_paths import REFERENCE_DIR_TOKEN, path_uses_token
from coder_eval.models.criteria import SuccessCriterion
from coder_eval.models.enums import AgentKind
from coder_eval.models.limits import RunLimits
from coder_eval.models.merge_strategy import MergeField
from coder_eval.models.sandbox import SandboxConfig


class UnknownTaskFieldWarning(DeprecationWarning):
    """Emitted by TaskDefinition for unknown top-level fields under the soft-launch
    policy in ``_warn_on_unknown_fields``. Dedicated subclass so callers (e.g.
    ``coder-eval plan``) can match by ``issubclass`` instead of substring-matching
    the message text — and so other ``DeprecationWarning``s emitted during load
    don't get conflated with stale-field signal.
    """


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
    constraints: list[str] = MergeField(
        strategy="replace",  # each layer's constraints wholesale-replace lower layers'
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
    """Defines the source for the reference solution — always a directory.

    The reference is NEVER shown to the agent being evaluated. The whole
    directory is staged into a per-run private copy and kept at mode ``000``
    for the duration of every agent turn (see
    ``fs_permissions.py``), so an agent cannot read the solution
    even though it shares a filesystem with the harness.

    Criteria address files inside it with the ``$REFERENCE_DIR`` token:

    - ``LLMJudgeCriterion`` / ``AgentJudgeCriterion``: list specific files in
      ``files:`` as ``$REFERENCE_DIR/rubric.md``, or set ``include_reference:
      true`` to attach the whole reference (inlined for ``llm_judge``, mounted
      at ``_reference/`` for ``agent_judge`` to ``Glob``/``Read``).
    - ``ReferenceComparisonCriterion``: names one file via ``reference_file``,
      resolved inside this directory.

    Inline ``code`` and single-file ``file`` forms were removed: a directory is
    the only form that can be permission-gated as a unit, and a one-file
    reference is expressible as a directory containing one file.
    """

    model_config = ConfigDict(extra="forbid")

    directory: str = Field(
        description=(
            "Path to a directory containing the reference solution, relative to the "
            "task YAML's own directory. Exposed to criteria as the REFERENCE_DIR env "
            "var and the $REFERENCE_DIR token; mounted at /work/references under "
            "driver: docker. Never readable by the agent under evaluation."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_string_forms(cls, data: Any) -> Any:
        """Give the removed ``code:`` / ``file:`` forms an actionable error.

        Without this they surface as a bare pair of pydantic errors ("directory
        Field required" + "file Extra inputs are not permitted") that names
        neither the replacement nor the reason. Mirrors the treatment the removed
        ``run_limits.stop_early: true`` key gets.
        """
        if isinstance(data, dict):
            removed = [k for k in ("code", "file") if k in data]
            if removed:
                raise ValueError(
                    f"reference.{'/'.join(removed)} was removed — a reference is now always a DIRECTORY. "
                    + "Move the solution into a directory and use `reference: {directory: <dir>}` "
                    + "(a single-file reference is a directory containing one file). "
                    + "See docs/TASK_DEFINITION_GUIDE.md#reference-solutions."
                )
        return data

    @field_validator("directory")
    @classmethod
    def _non_empty_directory(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("reference.directory must be a non-empty path relative to the task YAML directory.")
        return cleaned


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
    sample_per_stratum: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Stratified random sample: keep up to N rows per stratum, where the stratum is the "
            "value of 'stratify_field' on each row. Strata with <= N rows are taken whole. "
            "Designed for classification datasets (e.g. activation) where the metric is computed "
            "per stratum, so a uniform sample would starve rare strata. CLI '--sample' (a flat "
            "uniform-random N over the whole dataset) overrides this when set."
        ),
    )
    stratify_field: str = Field(
        default="expected_skill",
        description="Row field whose value defines the stratum for 'sample_per_stratum' (default: 'expected_skill').",
    )
    sample_seed: int | None = Field(
        default=None,
        description=(
            "Seed for the stratified 'sample_per_stratum' draw. Unlike CLI --sample (fixed-seed, "
            "reproducible by default), that draw is NONDETERMINISTIC when this is None: it re-draws "
            "every run, which broadens coverage for the nightly suites. This holds whether the "
            "per-stratum count comes from YAML or the CLI --sample-per-stratum flag. Set an integer "
            "here to pin a reproducible sample."
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


class TaskDefinition(BaseModel):  # noqa: CE009 -- soft-launch: see _warn_on_unknown_fields below
    """Complete definition of an evaluation task.

    Unlike ``ReferenceSource`` / ``BaseSuccessCriterion`` / sibling models in
    this file, ``TaskDefinition`` does NOT declare ``extra='forbid'``: this is
    the top-level user-facing input and downstream consumers (notably the
    ``~/src/skills/tests/tasks/`` repo) carry a long tail of stale fields
    (``max_iterations``, ``llm_reviewer``, …) that used to be silently
    dropped. Flipping to forbid would break those task YAMLs in lockstep,
    which is too disruptive to ship in a single PR.

    Instead, ``_warn_on_unknown_fields`` (below) logs a ``DeprecationWarning``
    naming each unknown top-level key, surfacing the typo to authors without
    blocking the run. Once the downstream backlog is cleared, this can be
    tightened to ``extra='forbid'`` and the ``# noqa: CE009`` suppression
    removed.
    """

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
    tags: list[str] = MergeField(
        strategy="replace",  # not layer-merged today; replace = the engine default if it ever is
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
    # ResolvedAgentConfig = base-typed + registry-driven dict coercion + SerializeAsAny:
    # the concrete subclass (built-in or plugin) is chosen by parse_agent_config, not a
    # static discriminated union, so any registered plugin kind validates here and its
    # subclass-only fields (e.g. sdk_options) survive model_dump().
    agent: ResolvedAgentConfig | None = Field(
        default=None,
        description=(
            "Agent configuration (resolved from experiment if omitted). Set `agent: {type: none}` for a "
            "no-op / system task: no coding agent runs and no model API call is made — coder-eval sets up "
            "the sandbox, executes pre_run, and checks the success_criteria directly. Use for system / "
            "canary checks (e.g. verifying Orchestrator or Integration Service connectivity) that reuse the "
            "eval infrastructure without involving an agent. A `type: none` task must declare no "
            "`initial_prompt` / `initial_prompt_file` and no `simulation` (no agent reads them), and every "
            "criterion must be agent-independent (no `requires_agent` criteria such as command_executed / "
            "skill_triggered / reference_comparison / commands_efficiency)."
        ),
    )
    sandbox: SandboxConfig = Field(
        default_factory=SandboxConfig,
        description="Sandbox configuration (defaults to tempdir if omitted)",
    )
    success_criteria: list[SuccessCriterion] = MergeField(
        strategy="replace",  # not layer-merged today; replace = the engine default if it ever is
        description="List of criteria that must all pass for task success",
    )
    run_limits: RunLimits | None = Field(
        default=None,
        description=(
            "Run-time caps (turns, wall-clock, tokens, USD) that abort the task when exceeded. See RunLimits."
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
    pre_run: list[PreRunCommand] = MergeField(
        strategy="append",
        default_factory=list,
        description=(
            "Commands to execute before agent evaluation starts. "
            "By default a failing command aborts evaluation with FinalStatus.ERROR. "
            "Set fail_on_error=False per command to make it informational. "
            "Experiment-defaults pre_run runs first (baseline setup), then the task's."
        ),
    )
    post_run: list[PostRunCommand] = MergeField(
        strategy="append",
        append_order="reverse",
        default_factory=list,
        description=(
            "Commands to execute after evaluation completes. Do not affect pass/fail. "
            "The task's commands run first, then experiment-defaults post_run (cleanup-last)."
        ),
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
    def _warn_on_unknown_fields(cls, data: Any) -> Any:
        """Soft replacement for ``extra='forbid'`` — log a warning per unknown key.

        ``TaskDefinition`` cannot flip to strict mode in this PR without breaking
        the downstream ``~/src/skills/tests/tasks/`` task repo, which carries a
        long tail of stale top-level fields (``max_iterations``, ``llm_reviewer``,
        …) that used to be silently dropped. This validator surfaces each unknown
        key as a ``DeprecationWarning`` so authors see the typo at load time,
        without blocking the run.

        Skips computed / writer-only fields that ``model_dump`` round-trips but
        loaders typically omit. The known-fields set is derived from
        ``cls.model_fields`` so it stays in sync if the model grows.
        """
        if not isinstance(data, dict):
            return data
        known = set(cls.model_fields)
        unknown = [k for k in data if k not in known]
        for key in unknown:
            warnings.warn(
                f"TaskDefinition: unknown top-level field {key!r} will be ignored. "
                + "If this is a typo, fix it; if the field is stale from a previous "
                + "schema, remove it. Future versions may reject unknown fields.",
                UnknownTaskFieldWarning,
                stacklevel=2,
            )
        return data

    @property
    def is_none_agent(self) -> bool:
        """True when this task runs the no-op agent (``agent: {type: none}``).

        The single signal for "no coding agent runs" — keyed off ``agent.type``,
        not a separate flag. Note this is only meaningful after the agent block
        is present; an unresolved task that omits ``agent`` entirely is False.
        """
        return self.agent is not None and self.agent.type == AgentKind.NONE

    @model_validator(mode="after")
    def check_prompt_fields(self) -> Self:
        """Validate initial_prompt / initial_prompt_file combination.

        - initial_prompt and initial_prompt_file are always mutually exclusive.
        - Exactly one is required for single-shot tasks.
        - In simulation mode (``simulation.enabled=True``) both may be omitted;
          the simulator then generates the opening user utterance from its
          persona and goal.
        - No-op (``agent: {type: none}``) tasks need no prompt — no agent reads
          it. (``check_none_agent`` additionally rejects a prompt if one is set.)
        """
        if self.initial_prompt is not None and self.initial_prompt_file is not None:
            raise ValueError("Only one of 'initial_prompt' or 'initial_prompt_file' can be provided, not both")
        in_simulation = self.simulation is not None and self.simulation.enabled
        if (
            self.initial_prompt is None
            and self.initial_prompt_file is None
            and not in_simulation
            and not self.is_none_agent
        ):
            raise ValueError(
                "Either 'initial_prompt' or 'initial_prompt_file' must be provided "
                + "(unless 'simulation.enabled' is true, in which case the simulator generates the opener, "
                + "or 'agent.type' is 'none', in which case no agent runs)"
            )
        return self

    @model_validator(mode="after")
    def check_none_agent(self) -> Self:
        """Validate the no-op agent (``agent: {type: none}``) contract.

        A ``type: none`` task runs no coding agent and makes no model API call, so:

        - It must declare no ``initial_prompt`` / ``initial_prompt_file`` and no
          enabled ``simulation`` — there is no agent to read a prompt or hold a
          dialog, so setting them is a silent no-op; reject loudly instead.
        - Every criterion must be agent-independent: criteria with
          ``requires_agent=True`` (e.g. command_executed, skill_triggered,
          reference_comparison, commands_efficiency) inspect the agent's
          trajectory, which does not exist when no agent runs.
        """
        if not self.is_none_agent:
            return self
        if self.initial_prompt is not None or self.initial_prompt_file is not None:
            raise ValueError(
                "A 'type: none' task must not set 'initial_prompt' / 'initial_prompt_file' "
                + "(no agent runs to read it). Remove the prompt or pick a real agent type."
            )
        if self.simulation is not None and self.simulation.enabled:
            raise ValueError(
                "A 'type: none' task cannot enable 'simulation' (no agent for the simulator to talk to). "
                + "Remove the simulation block or pick a real agent type."
            )
        offending = sorted({c.type for c in self.success_criteria if c.requires_agent})
        if offending:
            raise ValueError(
                f"A 'type: none' task cannot use criteria that require an agent trajectory: {offending}. "
                + "Use agent-independent criteria (run_command, file_exists, file_contains, json_check, "
                + "file_matches_regex, file_check, ...)."
            )
        return self

    @model_validator(mode="after")
    def check_reference_consumers_have_a_reference(self) -> Self:
        """Reject criteria that DEMAND the reference when no ``reference:`` block is set.

        These degrade silently otherwise — ``reference_comparison``
        deterministically scores 0.0, and a ``$REFERENCE_DIR/...`` entry in
        ``files:`` records a missing file. Catch it at load time instead.

        ``include_reference`` is deliberately NOT an offender, even when true: its
        documented contract is "silently omitted if no reference is configured", it
        DEFAULTS to true, and most judge tasks legitimately run without a reference.
        (Keying on ``model_fields_set`` to catch only an explicit ``true`` doesn't
        work either — a ``model_dump()`` round-trip marks every field as set.)
        """
        if self.reference is not None:
            return self
        offenders: list[str] = []
        for c in self.success_criteria:
            if c.type == "reference_comparison":
                offenders.append(f"{c.type} (needs a reference to compare against)")
            elif any(path_uses_token(f, REFERENCE_DIR_TOKEN) for f in getattr(c, "files", None) or []):
                offenders.append(f"{c.type} (files: uses {REFERENCE_DIR_TOKEN})")
            elif REFERENCE_DIR_TOKEN in (getattr(c, "command", None) or ""):
                # run_command is the third documented consumer: with no reference
                # the env var is simply absent, so `diff -r "$REFERENCE_DIR" out/`
                # expands to an empty argument and misbehaves instead of failing.
                offenders.append(f"{c.type} (command: uses {REFERENCE_DIR_TOKEN})")
        if offenders:
            raise ValueError(
                "These criteria consume the reference solution but the task defines no "
                + f"'reference:' block: {offenders}. Add `reference: {{directory: <path>}}`."
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
        """Normalize legacy criterion aliases and error on removed types."""
        removed: dict[str, str] = {
            "program_stdout_equals": "Use 'run_command' with 'expected_stdout' and 'stdout_match' instead.",
            "code_lints": "Use 'run_command' to run your linter directly instead.",
            "scored_command": "Use 'run_command' with 'score_from_stdout: true' instead.",
        }
        if isinstance(v, list):
            normalized: list[Any] = []
            for item in v:
                if isinstance(item, dict):
                    ctype = item.get("type")
                    if ctype in removed:
                        raise ValueError(f"Criterion type '{ctype}' has been removed. {removed[ctype]}")
                    if ctype == "command_not_executed":
                        item = {
                            **item,
                            "type": "command_executed",
                            "min_count": 0,
                            "max_count": 0,
                        }
                normalized.append(item)
            return normalized
        return v

    @field_validator("success_criteria")
    @classmethod
    def validate_success_criteria(cls, v: Any) -> Any:
        """Ensure at least one success criterion is defined."""
        if not v:
            raise ValueError("At least one success criterion must be defined")
        return v
