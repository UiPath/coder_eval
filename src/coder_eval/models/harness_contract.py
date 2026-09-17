"""What an agent harness honors of the uniform ``BaseAgentConfig`` fields."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from coder_eval.models.agent_config import SystemPromptMode
from coder_eval.models.enums import CANONICAL_TOOL_NAMES, TOOL_NAME_ALIASES, PermissionMode


class Enforcement(StrEnum):
    """Whether a harness honors a uniform agent field."""

    ENFORCED = "enforced"
    UNSUPPORTED = "unsupported"


class UsageGranularity(StrEnum):
    """How often a harness reports token usage on the event stream, which bounds a budget's overshoot."""

    GENERATION = "generation"
    STEP = "step"
    TURN = "turn"


class TimingBasis(StrEnum):
    """Where a harness's recorded stamps come from, which decides who stamps a tool and a window."""

    TURN_CLOCK = "turn_clock"
    CLI_EPOCH_MS = "cli_epoch_ms"


class HarnessContract(BaseModel):
    """The per-agent declaration of which uniform fields reach the harness.

    A task that sets a field this contract marks ``UNSUPPORTED``, or a
    ``permission_mode`` value outside ``permission_modes``, is rejected at
    resolution. Every registered agent class declares one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_prompt: Enforcement = Field(description="Whether agent.system_prompt reaches the harness.")
    system_prompt_semantics: SystemPromptMode | None = Field(
        default=None,
        description=(
            "How the prompt combines with the harness's own system prompt, through the system or developer "
            "instruction channel of the model request, never the user turn. None iff system_prompt is unsupported."
        ),
    )
    plugin_skills: Enforcement = Field(description="Whether the skills of agent.plugins reach the harness.")
    permission_mode: Enforcement = Field(description="Whether agent.permission_mode is honored.")
    allowed_tools: Enforcement = Field(description="Whether agent.allowed_tools restricts the harness's tools.")
    disallowed_tools: Enforcement = Field(description="Whether agent.disallowed_tools denies the harness's tools.")
    cooperative_stop: bool = Field(description="Whether communicate() honors the should_stop poll.")
    usage_granularity: UsageGranularity = Field(
        description=(
            "How often TurnEndEvent.tokens reports usage: per model generation, per agent-loop step, or once "
            "per communicate() call. A budget can overshoot by one such report."
        )
    )
    timing_basis: TimingBasis = Field(
        description=(
            "Where recorded stamps come from: turn_clock (the TurnEmitter stamps the turn bracket, every tool "
            "and every window from one TurnClock) or cli_epoch_ms (the adapter passes the CLI's own stamps for "
            "windows and main-thread tools)."
        )
    )
    reports_cost: bool = Field(
        default=False,
        description=(
            "Whether every finished turn with usage carries a cost the harness computed for any model it can "
            "run. When False, run_limits.max_usd requires an agent.model that pricing.py prices."
        ),
    )
    permission_modes: frozenset[PermissionMode] | None = Field(
        default=None,
        description=(
            "The permission_mode values honored, each with its Claude Code meaning. None iff permission_mode "
            "is unsupported."
        ),
    )

    @property
    def counts_model_turns(self) -> bool:
        """Whether the stream opens one inner turn per model response, so run_limits counts model turns."""
        return self.cooperative_stop and self.usage_granularity is not UsageGranularity.TURN

    @model_validator(mode="after")
    def check_semantics_matches_prompt_support(self) -> Self:
        """Require a semantics value exactly when the system prompt is enforced."""
        if (self.system_prompt is Enforcement.ENFORCED) != (self.system_prompt_semantics is not None):
            raise ValueError(
                "system_prompt_semantics must be set when system_prompt is 'enforced' and must be None "
                + f"when it is 'unsupported' (got system_prompt={self.system_prompt.value!r}, "
                + f"system_prompt_semantics={self.system_prompt_semantics!r})"
            )
        return self

    @model_validator(mode="after")
    def check_modes_match_permission_support(self) -> Self:
        """Require a non-empty value set exactly when permission_mode is enforced."""
        enforced = self.permission_mode is Enforcement.ENFORCED
        if enforced != (self.permission_modes is not None) or self.permission_modes == frozenset():
            raise ValueError(
                "permission_modes must be a non-empty set when permission_mode is 'enforced' and None when it "
                + f"is 'unsupported' (got permission_mode={self.permission_mode.value!r}, "
                + f"permission_modes={self.permission_modes!r})"
            )
        return self

    @field_serializer("permission_modes")
    def _sorted_modes(self, modes: frozenset[PermissionMode] | None) -> list[str] | None:
        return None if modes is None else sorted(str(mode) for mode in modes)


class ToolNameMap(BaseModel):
    """Each canonical tool name -> the harness's native tools it stands for.

    Total and closed over ``CANONICAL_TOOL_NAMES``: an empty tuple means the harness
    has no such tool, so no name is ever silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    names: dict[str, tuple[str, ...]] = Field(description="Canonical tool name -> native tool names.")
    mcp_names: bool = Field(default=False, description="Whether mcp__<server>__<tool> names reach the harness.")

    @model_validator(mode="after")
    def check_total_over_canonical_names(self) -> Self:
        """Require exactly one row per canonical tool name."""
        missing = sorted(CANONICAL_TOOL_NAMES - set(self.names))
        extra = sorted(set(self.names) - CANONICAL_TOOL_NAMES)
        if missing or extra:
            raise ValueError(
                f"ToolNameMap must map every canonical tool name exactly (missing={missing}, extra={extra})"
            )
        return self

    @classmethod
    def from_inverse(
        cls,
        forward: Mapping[str, str],
        *,
        no_equivalent: frozenset[str],
        mcp_names: bool = False,
    ) -> ToolNameMap:
        """Invert an adapter's native -> canonical telemetry map.

        Args:
            forward: Native tool name -> canonical name. Values outside
                ``CANONICAL_TOOL_NAMES`` are telemetry-only and dropped. An alias in
                ``TOOL_NAME_ALIASES`` shares its target's natives.
            no_equivalent: The canonical names the harness has no tool for.
            mcp_names: Whether MCP tool names reach the harness natively.

        Raises:
            ValueError: a canonical name is both mapped and in ``no_equivalent``, or in neither.
        """
        mapped = {
            canonical: tuple(sorted(native for native, name in forward.items() if name == canonical))
            for canonical in set(forward.values()) & CANONICAL_TOOL_NAMES
        }
        mapped |= {alias: mapped[target] for alias, target in TOOL_NAME_ALIASES.items() if target in mapped}
        both = sorted(set(mapped) & no_equivalent)
        neither = sorted(CANONICAL_TOOL_NAMES - set(mapped) - no_equivalent)
        if both or neither:
            raise ValueError(
                "each canonical tool name must be mapped or listed in no_equivalent, exactly once "
                + f"(in both: {both}; in neither: {neither})"
            )
        return cls(names={**mapped, **dict.fromkeys(no_equivalent, ())}, mcp_names=mcp_names)
