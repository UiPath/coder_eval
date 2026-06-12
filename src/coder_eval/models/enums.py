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


class PermissionMode(StrEnum):
    """Permission modes for agent tool access."""

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS_PERMISSIONS = "bypassPermissions"


class PreservationMode(StrEnum):
    """How a task's sandbox is persisted (or not) after execution.

    The run-level CLI default is *driver-derived* (resolved at the dispatch
    seam in ``orchestration.batch``): ``docker`` → ``DIRECT_WRITE`` (the
    container is isolated, and writing straight to the bind-mounted artifacts
    dir avoids a cross-mount copy), every other driver → ``MOVE_ON_WRITE``
    (running under ``run_dir/artifacts`` on a shared host would let parent-dir
    ``node_modules`` contaminate Node tool resolution — see MST-9795/PR #257).
    An explicit ``--preservation-mode`` always wins over that default.
    """

    NONE = "NONE"  # Run in a tempdir, delete it on cleanup (no artifacts kept).
    MOVE_ON_WRITE = "MOVE_ON_WRITE"  # Run in a tempdir, shutil.move into run_dir/artifacts at the end.
    DIRECT_WRITE = "DIRECT_WRITE"  # Run directly in run_dir/artifacts; nothing to move (left in place).


class AgentKind(StrEnum):
    """Named constants for the built-in agent kinds.

    These are NOT the closed set of valid agent types: ``agent.type`` is an open
    string validated against the ``AgentRegistry`` (which plugins extend via the
    ``coder_eval.plugins`` entry point). This enum just gives the framework's own
    code stable references to the built-ins; the registry is authoritative.
    """

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    NONE = "none"  # Agentless / system task — no coding agent runs; success criteria do all the work.
    UNKNOWN = "unknown"  # Used when agent type cannot be determined (e.g., task loading failure)


class AgentState(StrEnum):
    """Possible states of the agent during execution."""

    WORKING = "working"
    WAITING_FOR_USER = "waiting_for_user"
    CODE_PROPOSAL = "code_proposal"
    FINISHED = "finished"
    ERROR = "error"
