"""Enumeration types for coder_eval."""

from enum import StrEnum
from typing import Literal


class FinalStatus(StrEnum):
    """Final evaluation status for a task."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    MAX_TURNS_EXHAUSTED = "MAX_TURNS_EXHAUSTED"
    TOKEN_BUDGET_EXCEEDED = "TOKEN_BUDGET_EXCEEDED"
    COST_BUDGET_EXCEEDED = "COST_BUDGET_EXCEEDED"

    @property
    def category(self) -> Literal["succeeded", "failed", "error"]:
        """Classify this status into a reporting category."""
        if self == FinalStatus.SUCCESS:
            return "succeeded"
        if self == FinalStatus.ERROR:
            return "error"
        return "failed"

    @property
    def icon(self) -> str:
        """Single-character icon for reports and CLI output."""
        return _STATUS_ICONS[self]


_STATUS_ICONS: dict[FinalStatus, str] = {
    FinalStatus.SUCCESS: "+",
    FinalStatus.FAILURE: "-",
    FinalStatus.ERROR: "!",
    FinalStatus.TIMEOUT: "T",
    FinalStatus.MAX_TURNS_EXHAUSTED: "M",
    FinalStatus.TOKEN_BUDGET_EXCEEDED: "#",
    FinalStatus.COST_BUDGET_EXCEEDED: "$",
}

assert set(_STATUS_ICONS) == set(FinalStatus), "Missing icon for FinalStatus member"


class ApiBackend(StrEnum):
    """API backend for LLM calls."""

    DIRECT = "direct"  # Anthropic API directly (ANTHROPIC_API_KEY)
    BEDROCK = "bedrock"  # AWS Bedrock (bearer token auth)
    PROXY = "proxy"  # Local LLM Gateway proxy (OAuth2 S2S)


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
