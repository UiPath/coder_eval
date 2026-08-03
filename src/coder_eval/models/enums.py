"""Enumeration types for coder_eval."""

from enum import StrEnum
from typing import Literal


class FinalStatus(StrEnum):
    """Final evaluation status for a task."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ERROR = "ERROR"
    BUILD_FAILED = "BUILD_FAILED"
    TIMEOUT = "TIMEOUT"
    MAX_TURNS_EXHAUSTED = "MAX_TURNS_EXHAUSTED"
    TOKEN_BUDGET_EXCEEDED = "TOKEN_BUDGET_EXCEEDED"
    COST_BUDGET_EXCEEDED = "COST_BUDGET_EXCEEDED"

    @property
    def category(self) -> Literal["succeeded", "failed", "error"]:
        """Classify this status into a reporting category (the SSOT for failed/succeeded/error)."""
        return _STATUS_CATEGORIES[self]

    @property
    def icon(self) -> str:
        """Single-character icon for reports and CLI output."""
        return _STATUS_ICONS[self]


# Every FinalStatus maps to exactly one reporting category, listed EXPLICITLY (no
# catch-all default) so a newly-added status fails the assert below until it is
# classified — rather than silently collapsing into "failed" (which would skew
# reports AND the telemetry Category dimension). Mirrors the _STATUS_ICONS guard.
_STATUS_CATEGORIES: dict[FinalStatus, Literal["succeeded", "failed", "error"]] = {
    FinalStatus.SUCCESS: "succeeded",
    FinalStatus.FAILURE: "failed",
    FinalStatus.ERROR: "error",
    # A failed image build is an environment/setup error, not a task outcome —
    # group it with ERROR so reports/telemetry don't read it as a legitimate
    # task failure the agent could have avoided.
    FinalStatus.BUILD_FAILED: "error",
    FinalStatus.TIMEOUT: "failed",
    FinalStatus.MAX_TURNS_EXHAUSTED: "failed",
    FinalStatus.TOKEN_BUDGET_EXCEEDED: "failed",
    FinalStatus.COST_BUDGET_EXCEEDED: "failed",
}

assert set(_STATUS_CATEGORIES) == set(FinalStatus), "Missing category for FinalStatus member"


_STATUS_ICONS: dict[FinalStatus, str] = {
    FinalStatus.SUCCESS: "+",
    FinalStatus.FAILURE: "-",
    FinalStatus.ERROR: "!",
    FinalStatus.BUILD_FAILED: "B",
    FinalStatus.TIMEOUT: "T",
    FinalStatus.MAX_TURNS_EXHAUSTED: "M",
    FinalStatus.TOKEN_BUDGET_EXCEEDED: "#",
    FinalStatus.COST_BUDGET_EXCEEDED: "$",
}

assert set(_STATUS_ICONS) == set(FinalStatus), "Missing icon for FinalStatus member"


class IntegrityMode(StrEnum):
    """How the run-integrity pass acts on what it finds (``INTEGRITY_MODE``).

    The kill switch for the integrity gate. ``DETECT`` is the default so a
    rollout can read real findings from ``run.json`` before any score is voided;
    ``VOID`` additionally flips a tainted ``SUCCESS`` to ``FAILURE``. ``OFF``
    skips the pass entirely (verdict ``SKIPPED``), for bisecting a suspected
    false positive without redeploying.
    """

    OFF = "off"
    DETECT = "detect"
    VOID = "void"


class ApiBackend(StrEnum):
    """API backend for LLM calls."""

    DIRECT = "direct"  # Anthropic API directly (ANTHROPIC_API_KEY)
    BEDROCK = "bedrock"  # AWS Bedrock (bearer token auth)
    LITELLM = "litellm"  # LiteLLM (Anthropic-compatible) endpoint, e.g. LiteLLM -> Bedrock


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
    ANTIGRAVITY = "antigravity"
    NONE = "none"  # Agentless / system task — no coding agent runs; success criteria do all the work.
    UNKNOWN = "unknown"  # Used when agent type cannot be determined (e.g., task loading failure)


class AgentState(StrEnum):
    """Possible states of the agent during execution."""

    WORKING = "working"
    WAITING_FOR_USER = "waiting_for_user"
    CODE_PROPOSAL = "code_proposal"
    FINISHED = "finished"
    ERROR = "error"
