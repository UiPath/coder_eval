"""Agent configuration model and SDK pass-through validation."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any, ClassVar, Literal, Self, TypedDict

from claude_agent_sdk import ClaudeAgentOptions
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from coder_eval.models.enums import AgentKind, PermissionMode
from coder_eval.models.merge_strategy import MergeField


type SettingSource = Literal["user", "project", "local"]
"""Vendor-neutral mirror of claude_agent_sdk.SettingSource (no SDK dependency)."""


class LocalPluginConfig(TypedDict):
    """Vendor-neutral local plugin/skills source: a directory the agent scans for skills.

    Mirrors the runtime shape of claude_agent_sdk.SdkPluginConfig but carries no SDK
    dependency, so the agnostic BaseAgentConfig can declare ``plugins`` without leaking a
    Claude-Code type onto Codex / NoOp configs. Entries remain plain dicts at runtime
    (TypedDict), so all consumers — Codex skill discovery, docker_runner auto-mount,
    utils.process_plugins, and the Claude SDK pass-through — are unchanged.
    """

    type: Literal["local"]
    path: str


_VALID_SDK_OPTION_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(ClaudeAgentOptions))
# Keys that `coder_eval` already owns at the AgentConfig level OR that are
# transport / lifecycle / security-critical. Setting them via the
# sdk_options pass-through would either silently shadow a typed field, let
# the user inject pre-LLM lifecycle hooks (hooks / mcp_servers /
# permission_prompt_tool_name / can_use_tool / agents), or bypass
# framework-managed runtime state (cwd / env / resume / max_turns / ...).
# Most relevantly: AgentJudgeCriterion forces setting_sources=[] for
# security; allowing `hooks` through sdk_options would re-open that hole.
# NOTE on the allow/deny model: validation works as ALLOW iff
#   (key in _VALID_SDK_OPTION_FIELDS)  AND  (key not in _FRAMEWORK_OWNED_SDK_FIELDS)
# i.e. the user-visible set is ``_VALID - _FRAMEWORK_OWNED``. The denylist is
# explicit so the curated rationale (transport / lifecycle / security / typed-
# mirror) stays close to the code. To keep this from being fail-open as the
# SDK grows: ``tests/test_sdk_option_classification.py`` asserts EVERY field
# on ``ClaudeAgentOptions`` is classified — either typed-mirrored or in
# ``_FRAMEWORK_OWNED_SDK_FIELDS``. A new SDK release adding an unclassified
# field will fail that test loudly rather than silently being passed through.
_FRAMEWORK_OWNED_SDK_FIELDS: frozenset[str] = frozenset(
    {
        # mirrored as typed AgentConfig fields:
        "model",
        "permission_mode",
        "allowed_tools",
        "disallowed_tools",
        "plugins",
        "system_prompt",
        "system_prompt_file",
        "settings",
        # transport / runtime — set by the agent, not the user:
        "cwd",
        "env",
        "stderr",
        "debug_stderr",
        "resume",
        "max_turns",
        "session_id",
        "session_store",
        "session_store_flush",
        # session lifecycle — coder_eval owns this via `resume` and the
        # orchestrator's "advance session_id only on clean turns" logic.
        # Letting YAML override would silently bypass that.
        "continue_conversation",
        "fork_session",
        # budgeting — overlaps with RunLimits.max_usd / RunLimits.max_total_tokens
        # which the orchestrator enforces with explicit FinalStatus codes
        # (TOKEN_BUDGET_EXCEEDED / COST_BUDGET_EXCEEDED). Two independent
        # budget guards would disagree on counts; route everything through
        # RunLimits.
        "max_budget_usd",
        "task_budget",
        # security-critical: arbitrary code injection or settings-bypass
        # surfaces. Keep out of the YAML-visible knob.
        "hooks",
        "mcp_servers",
        "cli_path",
        "extra_args",
        "agents",
        "can_use_tool",
        "permission_prompt_tool_name",
        "tools",
        "sandbox",
        "skills",
        "add_dirs",
        "setting_sources",  # framework-controlled to prevent hook injection
        # OS uid drop — framework-managed by the docker user/permission isolation
        # barrier (set from agent_run_uid, not YAML). Letting it through sdk_options
        # would both bypass the barrier's gating and collide with the explicit
        # `user=` the agent sets in the ClaudeAgentOptions(...) block.
        "user",
        # telemetry: required by ClaudeCodeAgent to recover per-emission
        # output_tokens via message_delta stream events (works around
        # anthropics/claude-code#22686 where the assistant event's
        # output_tokens is a partial streaming snapshot). Letting YAML
        # turn it off would silently drop per-message output accounting.
        "include_partial_messages",
    }
)
# Precomputed user-visible allowlist (= valid SDK fields minus framework-owned).
# Pre-sorted once at module load so error paths don't recompute it.
_USER_VISIBLE_SDK_FIELDS: tuple[str, ...] = tuple(sorted(_VALID_SDK_OPTION_FIELDS - _FRAMEWORK_OWNED_SDK_FIELDS))


class BaseAgentConfig(BaseModel):
    """Base configuration for all agent types."""

    model_config = ConfigDict(validate_assignment=True, populate_by_name=True, extra="forbid")

    # Cross-field merge exclusion: setting either prompt field at any layer clears
    # the sibling (the generic resolver honors this uniformly). ClassVar -> not a
    # model field; Pydantic does not validate/assign it.
    _merge_exclusive_groups: ClassVar[tuple[tuple[str, ...], ...]] = (("system_prompt", "system_prompt_file"),)

    type: str | None = Field(
        default=None,
        description=(
            "The type of agent to use (claude-code, codex, or any plugin-registered kind). "
            "May be omitted on the task and supplied via experiment defaults or --type. "
            "Validated against the agent registry by parse_agent_config / task resolution."
        ),
    )
    model: str | None = Field(default=None, description="Specific model to use (if applicable)")
    permission_mode: PermissionMode = Field(
        default=PermissionMode.ACCEPT_EDITS, description="Permission mode for agent actions"
    )
    allowed_tools: list[str] | None = MergeField(
        strategy="replace", default=None, description="List of allowed tools (e.g., ['Read', 'Write', 'Bash'])"
    )
    disallowed_tools: list[str] | None = MergeField(
        strategy="replace", default=None, description="List of disallowed tools (e.g., ['TodoWrite'])"
    )
    plugins: list[LocalPluginConfig] | None = MergeField(
        strategy="replace", default=None, description="List of plugins (local skills/plugin sources)"
    )
    system_prompt: str | None = Field(
        default=None,
        description=(
            "Custom system prompt. Replaces the default system prompt. "
            "Supports inline text or multi-line YAML strings. "
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

    # Customizable ignore patterns for file tracking
    ignore_patterns: list[str] = MergeField(
        strategy="replace",
        default_factory=list,
        description=(
            "Pattern overrides applied when copying the workspace into a judge "
            "sub-agent sandbox. Plain entries add to the defaults; entries "
            "prefixed with '!' remove a default (gitignore-style negation)."
        ),
        validation_alias=AliasChoices("ignore_patterns", "additional_ignore_patterns"),
    )

    # Runtime-resolved (NOT task-authored): the unprivileged uid the docker
    # in-container entrypoint drops this agent's CLI subprocess to, under the
    # user/permission isolation barrier. Set by run_task_internal_command from the
    # container context.json; None everywhere else (no drop). Each agent reads it
    # and wires its own spawn seam (claude user=, codex launch_args_override,
    # antigravity PATH-shadow). Not a YAML field — carried via context.json, not the
    # 5-layer merge — so it needs no MergeField and no doc-parity entry. Authored
    # values are rejected at task-load (parse_task_dict) and CLI-override
    # (apply_overrides) time, since the contract is framework-set only.
    agent_run_uid: int | None = Field(
        default=None,
        exclude=True,  # runtime-only: set by direct write in the container; must NOT persist
        # into task.json/EvaluationResult, else the host read-back (model_validate) carries it
        # back through parse_agent_config and trips the framework-set-only authoring guard.
        description=(
            "Runtime-resolved unprivileged uid to run the agent's CLI subprocess as "
            "(docker user/permission isolation barrier). Framework-set, not task-authored."
        ),
    )

    @field_validator("ignore_patterns")
    @classmethod
    def _validate_ignore_patterns(cls, values: list[str]) -> list[str]:
        from coder_eval.resources import normalize_ignore_pattern_entry

        return [normalize_ignore_pattern_entry(v) for v in values]

    @model_validator(mode="after")
    def check_prompt_exclusivity(self) -> Self:
        """Ensure system_prompt and system_prompt_file are mutually exclusive."""
        if self.system_prompt is not None and self.system_prompt_file is not None:
            raise ValueError("Only one of 'system_prompt' or 'system_prompt_file' can be provided, not both")
        return self


class ClaudeCodeAgentConfig(BaseAgentConfig):
    """Claude Code agent configuration."""

    type: Literal[AgentKind.CLAUDE_CODE]  # type: ignore[assignment]

    claude_settings: str | dict[str, Any] | None = MergeField(
        strategy="deep",
        default=None,
        description=(
            "Claude Code settings passed via --settings. Accepts a JSON-serializable dict "
            "(inlined) or a file path string. Use permissions.deny to block tool access to "
            'specific paths: {"permissions": {"deny": ["Read(/some/path/**)"]}}. '
            "Merged deeply across config layers when both sides are dicts; a str/None value replaces."
        ),
    )
    sdk_options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Pass-through dict of Claude Code SDK ClaudeAgentOptions fields that coder_eval "
            "does not own directly (e.g. 'effort'). Keys must be valid ClaudeAgentOptions "
            "fields and must NOT be framework-managed (model/allowed_tools/permission_mode/...). "
            "Validated at YAML load; values are forwarded verbatim to the SDK."
        ),
    )
    setting_sources: list[SettingSource] | None = MergeField(
        strategy="replace",
        default=None,
        description=(
            "Claude Code setting sources to load (e.g., ['project', 'user']). "
            "Set to [] for maximum isolation (no host settings or hooks) — used by judge agents and simulators. "
            "Defaults to None, which at runtime becomes ['project'] so .mcp.json is discovered. "
            "Users may override this value for custom setting loading behavior."
        ),
    )

    @field_validator("sdk_options")
    @classmethod
    def _validate_sdk_options_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            return v
        for key in v:
            if key not in _VALID_SDK_OPTION_FIELDS:
                raise ValueError(
                    f"sdk_options key {key!r} is not a ClaudeAgentOptions field "
                    + f"(valid keys: {list(_USER_VISIBLE_SDK_FIELDS)})"
                )
            if key in _FRAMEWORK_OWNED_SDK_FIELDS:
                raise ValueError(
                    f"sdk_options key {key!r} is framework-managed; set it as a top-level AgentConfig field instead"
                )
        return v


class CodexAgentConfig(BaseAgentConfig):
    """Codex agent configuration."""

    type: Literal[AgentKind.CODEX]  # type: ignore[assignment]


# Gemini "thinking level" (reasoning effort) for the Antigravity backend. Mirrors
# google.antigravity.types.ThinkingLevel as a plain Literal so this config module
# imports without the optional `google-antigravity` SDK installed (the SDK is an
# opt-in extra; base installs must still load every config class).
type ThinkingLevel = Literal["minimal", "low", "medium", "high"]


class AntigravityAgentConfig(BaseAgentConfig):
    """Antigravity agent configuration (Google's Gemini coding agent harness).

    Runs via the ``google-antigravity`` SDK's local harness, authenticated with
    ``GEMINI_API_KEY``. ``model`` defaults (when unset on the task / ``--model`` /
    ``ANTIGRAVITY_MODEL``) to the recommended Gemini 3.1 Pro coding model
    (``gemini-3.1-pro-preview``).
    """

    type: Literal[AgentKind.ANTIGRAVITY]  # type: ignore[assignment]

    thinking_level: ThinkingLevel = Field(
        default="medium",
        description=(
            "Gemini reasoning effort (minimal/low/medium/high). 'medium' is Google's recommended "
            "daily-driver default — the API otherwise defaults to the more expensive 'high'."
        ),
    )


class NoneAgentConfig(BaseAgentConfig):
    """No-op ("agentless") agent configuration.

    Selected with ``agent: {type: none}``. Binds to ``NoOpAgent`` (Null Object
    pattern): coder-eval sets up the sandbox, runs ``pre_run``, and checks the
    success_criteria directly — the agent's ``start``/``communicate``/``stop``
    are no-ops and no model API call is made. Use for system / canary checks
    (e.g. Orchestrator or Integration Service connectivity) that reuse the eval
    infrastructure (sandbox, reports, evalboard, ADX) without involving an agent.

    The task must declare no ``initial_prompt`` / ``initial_prompt_file`` and no
    ``simulation`` (no agent reads them), and every criterion must be
    agent-independent (no ``requires_agent`` criteria) — enforced by
    ``TaskDefinition.check_none_agent``. The inherited ``model`` / prompt /
    tool fields are accepted but ignored.
    """

    type: Literal[AgentKind.NONE]  # type: ignore[assignment]


# Discriminated union type for type hints, validation, and YAML serialization
# Only includes the concrete subclasses (not BaseAgentConfig) since the discriminator
# must be a Literal type. BaseAgentConfig is returned by parse_agent_config when type=None.
type AgentConfig = Annotated[
    ClaudeCodeAgentConfig | CodexAgentConfig | AntigravityAgentConfig | NoneAgentConfig,
    Field(discriminator="type"),
]


def parse_agent_config(**kwargs: Any) -> BaseAgentConfig:
    """Factory function for agent configuration with registry-driven dispatch.

    Routes to the config class the agent registry binds to the ``type`` kind —
    ``ClaudeCodeAgentConfig`` / ``CodexAgentConfig`` / ``NoneAgentConfig`` for the
    built-ins, or any plugin-registered config subclass. If ``type`` is None (or
    omitted) returns a bare ``BaseAgentConfig`` so type resolution can happen
    later at the experiment / CLI layer.

    This replaces the import-time ``TypeAdapter(AgentConfig)`` discriminated-union
    dispatch (which could only ever see the built-in kinds) with a per-kind
    lookup resolved *after* plugin load — the BYOA seam.

    Args:
        **kwargs: Configuration fields including the ``type`` kind.

    Returns:
        The registered config subclass for ``type``, or ``BaseAgentConfig`` when
        ``type`` is None.

    Raises:
        ValueError: If ``type`` is a kind no agent is registered for.
        ValidationError: If configuration values are invalid for the config class.

    Example:
        >>> cfg = parse_agent_config(type="claude-code", model="claude-opus-4-7")
        >>> isinstance(cfg, ClaudeCodeAgentConfig)
        True
    """
    from coder_eval.agents.registry import AgentRegistry
    from coder_eval.plugins import ensure_plugins_loaded

    # agent_run_uid is framework-set ONLY (the docker isolation barrier assigns it
    # by direct attribute write on the already-constructed config). Reject any
    # non-None value arriving through CONSTRUCTION — this is the single choke point
    # every authoring path funnels through: YAML (TaskDefinition -> ResolvedAgentConfig
    # BeforeValidator -> here) AND the experiment variant / experiment-defaults merge
    # (resolve_root("agent") -> here). The framework's own direct attribute write
    # (task.agent.agent_run_uid = AGENT_UID) does NOT pass through this factory, so it
    # stays open. None (the model_dump round-trip default of an un-dropped config, e.g.
    # the container's staged task.yaml re-parse) is allowed.
    if kwargs.get("agent_run_uid") is not None:
        raise ValueError(
            "agent.agent_run_uid is framework-set only (docker isolation barrier); "
            + "it cannot be supplied from a task definition, experiment variant, or override"
        )

    agent_type = kwargs.get("type")

    if agent_type is None:
        # No type specified - return BaseAgentConfig with type=None
        # This allows type resolution to happen at the experiment/CLI layer
        filtered_kwargs = {k: v for k, v in kwargs.items() if k != "type"}
        return BaseAgentConfig(**filtered_kwargs)

    ensure_plugins_loaded()
    registration = AgentRegistry.get(agent_type)
    if registration is None:
        raise AgentRegistry.unregistered_kind_error(agent_type)
    return registration.config_class.model_validate(kwargs)


def _coerce_agent_config(value: Any) -> Any:
    """Coerce a raw agent-config dict to its registered subclass via the registry.

    Already-built config instances (and ``None``) pass through untouched.
    """
    if isinstance(value, dict):
        return parse_agent_config(**value)
    return value


# The canonical annotation for any field that holds a resolved agent config.
# BeforeValidator routes a dict through registry dispatch (so plugin kinds resolve
# to their subclass); SerializeAsAny keeps subclass-only fields on model_dump()
# instead of the base schema silently dropping them. Used by every persisted
# agent-config field (TaskDefinition.agent, EvaluationResult.agent_config) so the
# round-trip guarantee is uniform across the built-in union and plugin kinds.
type ResolvedAgentConfig = Annotated[SerializeAsAny[BaseAgentConfig], BeforeValidator(_coerce_agent_config)]
