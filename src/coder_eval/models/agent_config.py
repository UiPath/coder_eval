"""Agent configuration model and SDK pass-through validation."""

from __future__ import annotations

import dataclasses
from typing import Any, Literal, Self

from claude_agent_sdk import ClaudeAgentOptions, SdkPluginConfig, SettingSource
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from coder_eval.models.enums import AgentKind


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
        "setting_sources",
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
    }
)
# Precomputed user-visible allowlist (= valid SDK fields minus framework-owned).
# Pre-sorted once at module load so error paths don't recompute it.
_USER_VISIBLE_SDK_FIELDS: tuple[str, ...] = tuple(sorted(_VALID_SDK_OPTION_FIELDS - _FRAMEWORK_OWNED_SDK_FIELDS))


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
    sdk_options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Pass-through dict of Claude Code SDK ClaudeAgentOptions fields that coder_eval "
            "does not own directly (e.g. 'effort'). Keys must be valid ClaudeAgentOptions "
            "fields and must NOT be framework-managed (model/allowed_tools/permission_mode/...). "
            "Validated at YAML load; values are forwarded verbatim to the SDK."
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
                    f"(valid keys: {list(_USER_VISIBLE_SDK_FIELDS)})"
                )
            if key in _FRAMEWORK_OWNED_SDK_FIELDS:
                raise ValueError(
                    f"sdk_options key {key!r} is framework-managed; set it as a top-level AgentConfig field instead"
                )
        return v

    @model_validator(mode="after")
    def check_prompt_exclusivity(self) -> Self:
        """Ensure system_prompt and system_prompt_file are mutually exclusive."""
        if self.system_prompt is not None and self.system_prompt_file is not None:
            raise ValueError("Only one of 'system_prompt' or 'system_prompt_file' can be provided, not both")
        return self
