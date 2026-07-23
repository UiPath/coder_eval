"""Vendored ATIF (Agent Trajectory Interchange Format) models, v1.7.

Mirrors the schema in ``harbor.models.trajectories`` (harbor 0.20.0) so
coder_eval can emit and parse ATIF trajectories with ZERO runtime dependency
on the ``harbor`` pip package. Fidelity is guarded by a frozen fixture in
``tests/fixtures/atif/`` that was validated once against the real harbor
models (see ``tests/test_atif_models.py`` for the reproducible procedure).

Deliberate deviations from harbor's models:

- ``schema_version`` is a pattern-validated ``str`` (``^ATIF-v1\\.\\d+$``)
  instead of a closed Literal, so trajectories written by a FUTURE harbor
  minor version (e.g. ``ATIF-v1.9``) still parse — harbor 0.20.0 itself
  would reject them. Major-version bumps (``ATIF-v2.0``) are rejected.
- harbor's ``Agent`` model is named :class:`AtifAgent` here to avoid clashing
  with ``coder_eval.agent.Agent``.
- ``ContentPart.source`` (image payloads) is an untyped dict — coder_eval
  emits text-only content and only needs to *tolerate* image parts on read.

Deliberate deviation from the repo convention "all models importable from
``coder_eval.models``": these are interchange-format models for Harbor
interop, not evaluation models — they are exported from ``coder_eval.harbor``
to keep the core model namespace clean.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


ATIF_SCHEMA_VERSION = "ATIF-v1.7"
"""The ATIF version coder_eval emits (the version the vendored schema mirrors)."""


class ContentPart(BaseModel):
    """One part of a multimodal message (text or image).

    coder_eval emits text-only content; the image variant exists so ATIF
    documents produced by other agents still parse.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "image"] = Field(description="Content part kind.")
    text: str | None = Field(default=None, description="Text content. Required when type='text'.")
    source: dict[str, Any] | None = Field(
        default=None,
        description="Image payload (media type + data). Only meaningful when type='image'; untyped by design.",
    )

    @model_validator(mode="after")
    def _validate_by_type(self) -> Self:
        if self.type == "text":
            if self.text is None:
                raise ValueError("'text' field is required when type='text'")
            if self.source is not None:
                raise ValueError("'source' field is not allowed when type='text'")
        return self


class ToolCall(BaseModel):
    """A tool call within a step."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(description="Unique identifier for this specific tool call.")
    function_name: str = Field(description="The name of the function or tool being invoked.")
    arguments: dict[str, Any] = Field(description="Arguments passed to the function (can be empty dict).")
    extra: dict[str, Any] | None = Field(default=None, description="Custom tool-call-level metadata.")


class SubagentTrajectoryRef(BaseModel):
    """Reference to a delegated subagent trajectory."""

    model_config = ConfigDict(extra="forbid")

    trajectory_id: str = Field(description="trajectory_id of the referenced subagent trajectory.")
    trajectory_path: str | None = Field(
        default=None,
        description=(
            "Path to an external trajectory file. Null means embedded resolution: the id matches an "
            "entry in the root trajectory's subagent_trajectories array."
        ),
    )


class ObservationResult(BaseModel):
    """The result of one tool call or action within a step's observation."""

    model_config = ConfigDict(extra="forbid")

    source_call_id: str | None = Field(
        default=None,
        description=(
            "The tool_call_id from this step's tool_calls that this result corresponds to. Null for "
            "results from actions that don't use the standard tool-calling format."
        ),
    )
    content: str | list[ContentPart] | None = Field(
        default=None,
        description="The output from the tool execution (string, or ContentPart list for multimodal).",
    )
    subagent_trajectory_ref: list[SubagentTrajectoryRef] | None = Field(
        default=None,
        description="References to delegated subagent trajectories spawned by this call.",
    )
    extra: dict[str, Any] | None = Field(default=None, description="Custom observation-result-level metadata.")


class Observation(BaseModel):
    """The environment feedback for one step."""

    model_config = ConfigDict(extra="forbid")

    results: list[ObservationResult] = Field(description="Result objects from tool calls or actions.")


class Metrics(BaseModel):
    """Per-step LLM metrics."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = Field(default=None, description="Prompt tokens for this LLM call (full prompt).")
    completion_tokens: int | None = Field(default=None, description="Completion tokens generated by this call.")
    cached_tokens: int | None = Field(default=None, description="Prompt tokens served from cache.")
    cost_usd: float | None = Field(default=None, description="Cost of this call in USD.")
    prompt_token_ids: list[int] | None = Field(default=None, description="Token ids of the prompt (RL pipelines).")
    completion_token_ids: list[int] | None = Field(
        default=None, description="Token ids of the completion (RL pipelines)."
    )
    logprobs: list[float] | None = Field(default=None, description="Per-token logprobs of the completion.")
    extra: dict[str, Any] | None = Field(default=None, description="Custom metrics-level metadata.")


class FinalMetrics(BaseModel):
    """Summary metrics for the entire trajectory."""

    model_config = ConfigDict(extra="forbid")

    total_prompt_tokens: int | None = Field(default=None, description="Total prompt tokens across the trajectory.")
    total_completion_tokens: int | None = Field(default=None, description="Total completion tokens.")
    total_cached_tokens: int | None = Field(default=None, description="Total cached prompt tokens.")
    total_cost_usd: float | None = Field(default=None, description="Total cost in USD.")
    total_steps: int | None = Field(default=None, description="Number of steps in the trajectory.")
    extra: dict[str, Any] | None = Field(default=None, description="Custom final-metrics metadata.")


class AtifAgent(BaseModel):
    """The agent configuration that produced the trajectory.

    harbor names this model ``Agent``; renamed here to avoid clashing with
    ``coder_eval.agent.Agent``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Agent name (e.g. the registered agent kind).")
    version: str = Field(description="Agent version string (REQUIRED by the ATIF spec).")
    model_name: str | None = Field(default=None, description="Default model for the trajectory's steps.")
    tool_definitions: list[dict[str, Any]] | None = Field(
        default=None,
        description="Tool/function definitions available to the agent (OpenAI function-calling schema).",
    )
    extra: dict[str, Any] | None = Field(default=None, description="Custom agent-level metadata.")


class Step(BaseModel):
    """A single step in the trajectory (one message event)."""

    model_config = ConfigDict(extra="forbid")

    step_id: int = Field(ge=1, description="Ordinal index of the step (sequential, starting from 1).")
    timestamp: str | None = Field(default=None, description="ISO 8601 timestamp of when this step occurred.")
    source: Literal["system", "user", "agent"] = Field(description="The originator of this step.")
    model_name: str | None = Field(
        default=None,
        description="LLM model used for this step. Omission implies the root agent config's model.",
    )
    reasoning_effort: str | float | None = Field(
        default=None, description="Qualitative or quantitative measure of effort."
    )
    message: str | list[ContentPart] = Field(
        description="The dialogue message (string, or ContentPart list for multimodal)."
    )
    reasoning_content: str | None = Field(default=None, description="The agent's explicit internal reasoning.")
    tool_calls: list[ToolCall] | None = Field(default=None, description="Tool calls issued in this step.")
    observation: Observation | None = Field(default=None, description="Environment feedback for this step.")
    metrics: Metrics | None = Field(default=None, description="LLM metrics for this step.")
    is_copied_context: bool | None = Field(
        default=None, description="True when the step's content was copied from a prior context (compaction)."
    )
    llm_call_count: int | None = Field(default=None, description="Number of LLM calls this step represents.")
    extra: dict[str, Any] | None = Field(default=None, description="Custom step-level metadata.")


class Trajectory(BaseModel):
    """Agent Trajectory in ATIF (Agent Trajectory Interchange Format)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default=ATIF_SCHEMA_VERSION,
        pattern=r"^ATIF-v1\.\d+$",
        description=(
            "ATIF compatibility version. Any v1.x parses (forward-tolerant read, unlike harbor's closed "
            "Literal); v2+ is rejected."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description="Run-scoped identifier; may be shared by a parent trajectory and its embedded subagents.",
    )
    trajectory_id: str | None = Field(
        default=None,
        description=(
            "Per-trajectory-document unique identifier. Optional on standalone trajectories; REQUIRED and "
            "unique on trajectories embedded in a parent's subagent_trajectories array."
        ),
    )
    agent: AtifAgent = Field(description="The agent configuration that produced this trajectory.")
    steps: list[Step] = Field(min_length=1, description="The complete interaction history.")
    notes: str | None = Field(default=None, description="Custom information, design notes, or explanations.")
    final_metrics: FinalMetrics | None = Field(default=None, description="Summary metrics for the trajectory.")
    continued_trajectory_ref: str | None = Field(
        default=None, description="Reference to the continuation trajectory file, if continued elsewhere."
    )
    extra: dict[str, Any] | None = Field(default=None, description="Custom root-level metadata.")
    subagent_trajectories: list[Trajectory] | None = Field(
        default=None,
        description="Embedded subagent trajectories; each must carry a unique, non-null trajectory_id.",
    )

    @model_validator(mode="after")
    def validate_step_ids(self) -> Self:
        """step_ids must be sequential starting from 1 (mirrors harbor's validator)."""
        for i, step in enumerate(self.steps):
            expected = i + 1
            if step.step_id != expected:
                raise ValueError(f"steps[{i}].step_id: expected {expected} (sequential from 1), got {step.step_id}")
        return self

    @model_validator(mode="after")
    def validate_tool_call_references(self) -> Self:
        """Every observation source_call_id must reference a tool_call_id in the SAME step."""
        for step in self.steps:
            if step.observation is None:
                continue
            tool_call_ids = {tc.tool_call_id for tc in step.tool_calls} if step.tool_calls else set()
            for result in step.observation.results:
                if result.source_call_id is not None and result.source_call_id not in tool_call_ids:
                    raise ValueError(
                        f"Observation result references source_call_id '{result.source_call_id}' "
                        + f"which is not found in step {step.step_id}'s tool_calls"
                    )
        return self

    @model_validator(mode="after")
    def validate_embedded_subagent_trajectory_ids(self) -> Self:
        """Embedded subagents must carry a unique, non-null trajectory_id (resolution key)."""
        if not self.subagent_trajectories:
            return self
        seen: set[str] = set()
        for i, sub in enumerate(self.subagent_trajectories):
            if sub.trajectory_id is None:
                raise ValueError(
                    f"subagent_trajectories[{i}].trajectory_id is required for embedded subagents "
                    + f"(agent.name={sub.agent.name!r}, session_id={sub.session_id!r})"
                )
            if sub.trajectory_id in seen:
                raise ValueError(
                    f"subagent_trajectories[{i}].trajectory_id {sub.trajectory_id!r} is not unique "
                    + "within subagent_trajectories"
                )
            seen.add(sub.trajectory_id)
        return self
