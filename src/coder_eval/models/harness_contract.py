"""What an agent harness honors of the uniform ``BaseAgentConfig`` fields."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coder_eval.models.agent_config import SystemPromptMode


class Enforcement(StrEnum):
    """Whether a harness honors a uniform agent field."""

    ENFORCED = "enforced"
    UNSUPPORTED = "unsupported"


class HarnessContract(BaseModel):
    """The per-agent declaration of which uniform fields reach the harness.

    A task that sets a field this contract marks ``UNSUPPORTED`` is rejected at
    resolution. Every registered agent class declares one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_prompt: Enforcement = Field(description="Whether agent.system_prompt reaches the harness.")
    system_prompt_semantics: SystemPromptMode | None = Field(
        default=None,
        description="How the prompt combines with the harness's own prompt. None iff system_prompt is unsupported.",
    )
    plugin_skills: Enforcement = Field(description="Whether the skills of agent.plugins reach the harness.")
    permission_mode: Enforcement = Field(description="Whether agent.permission_mode is honored.")
    allowed_tools: Enforcement = Field(description="Whether agent.allowed_tools restricts the harness's tools.")
    disallowed_tools: Enforcement = Field(description="Whether agent.disallowed_tools denies the harness's tools.")
    cooperative_stop: bool = Field(description="Whether communicate() honors the should_stop poll.")

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
