"""Task definition and configuration models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from coder_eval.models.criteria import SuccessCriterion
from coder_eval.models.enums import AgentKind
from coder_eval.models.sandbox import SandboxConfig


class AgentConfig(BaseModel):
    """Configuration for the coding agent."""

    type: AgentKind = Field(description="The type of agent to use (claude-code, aider, etc.)")
    permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions"] = Field(
        default="acceptEdits", description="Permission mode for agent actions"
    )
    allowed_tools: list[str] | None = Field(
        default=None, description="List of allowed tools (e.g., ['Read', 'Write', 'Bash'])"
    )
    model: str | None = Field(default=None, description="Specific model to use (if applicable)")

    # Customizable ignore patterns for file tracking
    additional_ignore_patterns: list[str] = Field(
        default_factory=list,
        description="Additional patterns to ignore during file change detection (beyond defaults)",
    )


class LLMReviewerConfig(BaseModel):
    """Configuration for the LLM-based qualitative reviewer.

    All models are accessed through UiPath LLM Gateway using LangChain integration.
    Use Gateway model naming convention (e.g., anthropic.claude-3-5-sonnet-20240620-v1:0, gpt-4o-2024-08-06).
    """

    enabled: bool = Field(default=False, description="Whether to enable LLM review")
    model: str = Field(
        default="anthropic.claude-3-5-sonnet-20240620-v1:0",
        description="Gateway model name (e.g., anthropic.claude-3-5-sonnet-20240620-v1:0, gpt-4o-2024-08-06)",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="Temperature for LLM sampling")
    max_tokens: int = Field(default=1000, gt=0, description="Maximum tokens in response")


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
    def check_exclusive_source(self):
        """Ensure exactly one source is provided."""
        if self.code is not None and self.file is not None:
            raise ValueError("Only one of 'code' or 'file' can be provided for reference code.")
        if self.code is None and self.file is None:
            raise ValueError("One of 'code' or 'file' must be provided for reference code.")
        return self


class TaskDefinition(BaseModel):
    """Complete definition of an evaluation task."""

    task_id: str = Field(description="Unique identifier for this task")
    description: str = Field(description="Human-readable description of what the task is testing")
    initial_prompt: str = Field(description="The initial prompt to send to the agent")
    max_iterations: int = Field(default=3, description="Maximum number of agent turns")
    agent: AgentConfig = Field(description="Agent configuration")
    sandbox: SandboxConfig = Field(description="Sandbox configuration")
    success_criteria: list[SuccessCriterion] = Field(description="List of criteria that must all pass for task success")
    llm_reviewer: LLMReviewerConfig = Field(
        default_factory=LLMReviewerConfig, description="Optional LLM reviewer configuration"
    )
    reference: ReferenceSource | None = Field(
        default=None,
        description=(
            "Reference solution for LLM review and code comparison. HIDDEN from the agent - never included in prompts."
        ),
    )

    @field_validator("success_criteria")
    @classmethod
    def validate_success_criteria(cls, v):
        """Ensure at least one success criterion is defined."""
        if not v:
            raise ValueError("At least one success criterion must be defined")
        return v
