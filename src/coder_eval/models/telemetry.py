"""Telemetry and command statistics models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    """Token usage from a single agent turn or aggregated across turns."""

    input_tokens: int = Field(default=0, description="Input prompt tokens")
    output_tokens: int = Field(default=0, description="Output completion tokens")
    cache_creation_input_tokens: int = Field(default=0, description="Tokens used to create prompt cache")
    cache_read_input_tokens: int = Field(default=0, description="Tokens read from prompt cache")
    total_cost_usd: float | None = Field(default=None, description="Total cost in USD (from SDK)")

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed (input + output)."""
        return self.input_tokens + self.output_tokens


class ContentBlock(BaseModel):
    """One content block within a message, in emission order.

    Can be a text block (user/assistant), thinking block (assistant),
    tool_use block (assistant), or tool_result block (user).
    """

    block_type: Literal["text", "thinking", "tool_use", "tool_result"] = Field(
        description="Block kind: text, thinking, tool_use, or tool_result."
    )
    sequence: int = Field(description="0-indexed position within the parent message.content_blocks list.")

    text: str | None = Field(default=None, description="Text content. Set when block_type='text' or 'tool_result'.")

    thinking: str | None = Field(
        default=None,
        description="Extended-thinking reasoning content. Set when block_type='thinking'.",
    )
    signature: str | None = Field(default=None, description="Anthropic-signed signature for the thinking block.")

    tool_use_id: str | None = Field(
        default=None,
        description="Tool invocation ID; joins to CommandTelemetry. Set for tool_use or tool_result blocks.",
    )
    is_error: bool = Field(
        default=False,
        description="True if tool_result block represents an error.",
    )


class UserMessage(BaseModel):
    """One user-side message (text or tool result) within a TurnRecord.

    For simulator-driven dialog mode: captures user utterances from the
    simulator plus orchestrator-side wall-clock timing. Populated in
    simulation runs only.

    For tool results: captured when a tool_result UserMessage arrives
    from the SDK, containing tool result blocks and error status.
    """

    role: Literal["user"] = "user"

    text: str = Field(description="Utterance sent to the agent (stop token stripped).")
    raw_text: str | None = Field(
        default=None,
        description="Simulator's raw output before stop-token stripping. None for pinned initial_prompt openers.",
    )
    stop_requested: bool | None = Field(
        default=None, description="Whether the simulator emitted its stop token. None for pinned openers."
    )

    started_at: datetime | None = Field(
        default=None, description="Wall-clock start of the simulator LLM call. None for pinned openers."
    )
    completed_at: datetime | None = Field(
        default=None, description="Wall-clock end of the simulator LLM call. None for pinned openers."
    )
    generation_duration_ms: float | None = Field(
        default=None, description="completed_at - started_at in milliseconds. None for pinned openers."
    )

    input_tokens: int = Field(default=0, description="Simulator prompt tokens (0 if SDK did not surface them).")
    output_tokens: int = Field(default=0, description="Simulator completion tokens (0 if SDK did not surface them).")
    model: str | None = Field(default=None, description="Simulator model identifier.")
    simulator_failed: bool = Field(
        default=False, description="True if the simulator call raised; text/raw_text reflect the fallback or are empty."
    )


class AssistantMessage(BaseModel):
    """One LLM response message (from SDK AssistantMessage) within a TurnRecord.

    Captures per-message generation timing, content blocks, and token usage so
    LLM time can be measured directly rather than inferred from gaps between
    command timestamps. Per-message token usage is per-generation and may not
    match the final aggregate from ResultMessage (which is the authoritative total).
    """

    role: Literal["assistant"] = "assistant"

    started_at: datetime = Field(description="Wall-clock start of generation (end of the previous SDK event).")
    completed_at: datetime = Field(description="Wall-clock arrival of the AssistantMessage from the SDK.")
    generation_duration_ms: float = Field(
        description="completed_at - started_at in milliseconds (wall-clock between SDK events).",
    )

    content_blocks: list[ContentBlock] = Field(
        default_factory=list,
        description="Content blocks Claude emitted in this turn, in emission order.",
    )
    tool_use_ids: list[str] = Field(
        default_factory=list,
        description="tool_use_id values from content_blocks, for joining with CommandTelemetry and tool_result blocks.",
    )

    input_tokens: int = Field(
        default=0,
        description=(
            "Input prompt tokens for this LLM call. Zero on follow-up emissions that "
            "share a message_id with an earlier AssistantMessage (the CLI splits one "
            "API call into multiple emissions); billing is recorded on the first only. "
            "Not authoritative for cost — use iteration.token_usage."
        ),
    )
    output_tokens: int = Field(
        default=0,
        description=(
            "Output tokens generated in this LLM call. Recovered from the "
            "message_delta stream event when include_partial_messages is on; falls "
            "back to the (often partial) SDK assistant-event value otherwise. "
            "Per-emission only — do NOT sum across emissions for billing; use "
            "iteration.token_usage as the authoritative aggregate."
        ),
    )
    cache_creation_tokens: int = Field(default=0, description="Tokens used to create prompt cache for this call.")
    cache_read_tokens: int = Field(default=0, description="Tokens read from prompt cache for this call.")
    reasoning_tokens: int = Field(default=0, description="Extended-thinking tokens; subset of output_tokens.")

    stop_reason: str | None = Field(default=None, description="SDK stop reason: 'tool_use', 'end_turn', etc.")
    model: str | None = Field(default=None, description="Model identifier that generated this turn.")

    message_id: str | None = Field(
        default=None,
        description=(
            "Anthropic API message_id. Multiple AssistantMessage records can share this id "
            "when the Claude Code CLI splits one API response into per-block-kind events. "
            "Downstream tooling can group by this id to recover one logical generation."
        ),
    )


class CommandTelemetry(BaseModel):
    """Telemetry for a single command execution.

    Captures detailed information about each tool use by the agent,
    enabling analysis, debugging, and optimization.

    **Deprecation notice:** This model duplicates data already available in
    TurnRecord.messages (as tool_use and tool_result blocks). In future versions,
    command telemetry should be derived from the messages array rather than
    maintained as a parallel structure. Kept for backward compatibility.
    """

    # Identity
    tool_name: str = Field(description="Tool name (Read, Write, Bash, Edit, Glob, etc.)")
    tool_id: str = Field(description="Unique ID from Claude SDK for this tool invocation")

    assistant_turn_index: int | None = Field(
        default=None,
        description=(
            "0-indexed position among AssistantMessage entries only. "
            "Filter turn_record.messages by role='assistant' before indexing."
        ),
    )

    # Timing: generation_completed_at is when Claude finished emitting the
    # tool_use block; execution_* bracket the actual tool run. `timestamp`
    # equals generation_completed_at and `duration_ms` equals
    # execution_completed_at - execution_started_at; both are retained for
    # downstream consumers that index on the original field names.
    timestamp: datetime = Field(description="Equal to generation_completed_at; when the tool_use block arrived.")
    duration_ms: float | None = Field(
        default=None,
        description="Command execution time in milliseconds (None = not complete, set in two-phase processing).",
    )
    generation_completed_at: datetime | None = Field(
        default=None,
        description="When Claude finished emitting the tool_use block.",
    )
    execution_started_at: datetime | None = Field(default=None, description="When tool execution began.")
    execution_completed_at: datetime | None = Field(
        default=None, description="When tool execution completed (success or error)."
    )

    # Parameters (structured)
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Structured command parameters (e.g., {'file_path': 'main.py'} for Read)"
    )

    # Results
    result_status: Literal["success", "error", "unknown"] | None = Field(
        default=None,
        description="Whether the command succeeded or failed (None = pending result, set during two-phase processing)",
    )
    result_summary: str | None = Field(
        default=None, description="Brief summary of result (e.g., 'File read: 245 bytes', 'Exit code: 0')"
    )
    error_message: str | None = Field(default=None, description="Error message if command failed")
    result_data: dict[str, Any] | list[Any] | None = Field(
        default=None,
        description=(
            "First parsed JSON object or array found in the tool result content. "
            "Prefix noise (e.g. CLI warning lines before the JSON body) and trailing "
            "garbage are tolerated. None for content without any parseable JSON object "
            "or array and for bare primitives (strings, numbers, booleans, null). "
            "Complements result_summary (a short text preview) by preserving the full "
            "structured payload so downstream consumers can extract specialised views "
            "without re-parsing the log."
        ),
    )

    # Metadata
    sequence_number: int = Field(default=0, description="Order within the turn (0-indexed)")


class SlowestCommandInfo(BaseModel):
    """Information about a slow command for performance analysis.

    Type-safe model for reporting slowest commands in statistics.
    """

    tool: str = Field(description="Tool name (e.g., 'Bash', 'Read')")
    duration_ms: float = Field(description="Execution duration in milliseconds")
    parameters: dict[str, Any] = Field(description="Command parameters")
    tool_id: str | None = Field(default=None, description="Optional: Unique tool invocation ID")


class CommandStatistics(BaseModel):
    """Aggregated statistics for command usage in an evaluation.

    Provides summary metrics for analysis and reporting.
    """

    total_commands: int = Field(description="Total number of commands executed")
    commands_by_tool: dict[str, int] = Field(
        default_factory=dict, description="Count of commands per tool (e.g., {'Bash': 45, 'Read': 12})"
    )

    # Timing
    total_command_time_ms: float = Field(default=0.0, description="Total time spent executing commands (milliseconds)")
    avg_command_time_ms: float | None = Field(default=None, description="Average command execution time")
    slowest_commands: list[SlowestCommandInfo] = Field(
        default_factory=list, description="Top 5 slowest commands with details (type-safe model)"
    )

    # Success/Failure
    successful_commands: int = Field(default=0, description="Commands that succeeded")
    failed_commands: int = Field(default=0, description="Commands that failed")
    unknown_commands: int = Field(
        default=0,
        description="Commands with unknown status (missing ResultMessage, indicates agent/SDK interruption)",
    )
    success_rate: float = Field(
        default=0.0,
        description="Percentage of known commands that succeeded: success / (success + failed), excludes unknown",
    )

    # Patterns
    most_common_sequence: str | None = Field(
        default=None, description="Most common 3-command sequence (e.g., 'Read -> Edit -> Bash')"
    )
