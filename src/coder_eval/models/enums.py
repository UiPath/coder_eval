"""Enumeration types for coder_eval."""

from enum import StrEnum


class AgentKind(StrEnum):
    """Supported agent types."""

    CLAUDE_CODE = "claude-code"
    AIDER = "aider"
    UNKNOWN = "unknown"  # Used when agent type cannot be determined (e.g., task loading failure)


class AgentState(StrEnum):
    """Possible states of the agent during execution."""

    WORKING = "working"
    WAITING_FOR_USER = "waiting_for_user"
    CODE_PROPOSAL = "code_proposal"
    FINISHED = "finished"
    ERROR = "error"


class SnapshotMode(StrEnum):
    """Snapshot mode for iteration snapshots.

    Note: No separate 'enabled' flag needed - use DISABLED to disable snapshots.
    """

    DISABLED = "disabled"  # No snapshots
    FULL = "full"  # Copy entire sandbox every iteration
    INCREMENTAL = "incremental"  # Only changed files
    HYBRID = "hybrid"  # Full at checkpoints, incremental otherwise
