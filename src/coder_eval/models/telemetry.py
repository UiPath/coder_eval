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


class CommandTelemetry(BaseModel):
    """Telemetry for a single command execution.

    Captures detailed information about each tool use by the agent,
    enabling analysis, debugging, and optimization.
    """

    # Identity
    tool_name: str = Field(description="Tool name (Read, Write, Bash, Edit, Glob, etc.)")
    tool_id: str = Field(description="Unique ID from Claude SDK for this tool invocation")

    # Timing
    timestamp: datetime = Field(description="When the command was executed")
    duration_ms: float | None = Field(
        default=None,
        description="Command execution time in milliseconds (None = not complete, calculated in two-phase processing)",
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
