"""Enumeration types for coder_eval."""

from enum import StrEnum
from typing import Final, Literal


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
    # No verdict to report. Distinct from FAILURE (criteria checked, did not pass)
    # and ERROR (something went wrong); only those two collapse into it.
    # Rationale: .claude/notes/orchestration.md § Execute vs. run: the grading switch
    NOT_GRADED = "NOT_GRADED"

    @property
    def category(self) -> Literal["succeeded", "failed", "error", "ungraded"]:
        """Classify this status into a reporting category (the SSOT for failed/succeeded/error)."""
        return _STATUS_CATEGORIES[self]

    @property
    def icon(self) -> str:
        """Single-character icon for reports and CLI output."""
        return _STATUS_ICONS[self]

    @property
    def is_execution_fact(self) -> bool:
        """True when this status records HOW THE RUN ENDED, not what grading decided.

        A detached grade (``evaluate <run_dir>`` / ``run --resume``) re-runs the
        criteria over a trajectory it did not produce, so it may only move a row
        between the three GRADING outcomes — ``NOT_GRADED`` -> ``SUCCESS`` /
        ``FAILURE``. It must never launder a run that timed out, crashed, or blew
        a budget into a pass: those statuses describe the agent phase, which the
        grading pass neither repeated nor observed.
        """
        return _EXECUTION_FACT_STATUSES[self]


# EXPLICIT, no catch-all default, so a newly-added status fails the assert below
# until it is classified. Mirrors the _STATUS_ICONS guard.
# Rationale: .claude/notes/orchestration.md § Rates need verdict evidence, not bucket counts
_STATUS_CATEGORIES: dict[FinalStatus, Literal["succeeded", "failed", "error", "ungraded"]] = {
    FinalStatus.SUCCESS: "succeeded",
    FinalStatus.FAILURE: "failed",
    FinalStatus.ERROR: "error",
    # An environment fault, not a task outcome the agent could have avoided.
    FinalStatus.BUILD_FAILED: "error",
    FinalStatus.TIMEOUT: "failed",
    FinalStatus.MAX_TURNS_EXHAUSTED: "failed",
    FinalStatus.TOKEN_BUDGET_EXCEEDED: "failed",
    FinalStatus.COST_BUDGET_EXCEEDED: "failed",
    # A FOURTH category, not a fold into one of the three. Excluded from both the
    # numerator and the denominator of a pass rate.
    FinalStatus.NOT_GRADED: "ungraded",
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
    FinalStatus.NOT_GRADED: "?",
}

assert set(_STATUS_ICONS) == set(FinalStatus), "Missing icon for FinalStatus member"


# Explicit, no catch-all: a new status is either "the agent phase ended this way"
# (True -- a detached grade preserves it) or "grading decided this" (False).
# Defaulting either way silently is how an ERROR row becomes a SUCCESS.
_EXECUTION_FACT_STATUSES: dict[FinalStatus, bool] = {
    FinalStatus.SUCCESS: False,
    FinalStatus.FAILURE: False,
    FinalStatus.NOT_GRADED: False,
    FinalStatus.ERROR: True,
    FinalStatus.BUILD_FAILED: True,
    FinalStatus.TIMEOUT: True,
    # HAZARD: False, and it must stay False. MAX_TURNS_EXHAUSTED is SUBORDINATE to
    # the verdict, not a fact that outranks it; the fact itself lives on
    # `EvaluationResult.max_turns_exhausted`, which the seeding carries.
    # Rationale: .claude/notes/orchestration.md § The terminal-status chain
    FinalStatus.MAX_TURNS_EXHAUSTED: False,
    FinalStatus.TOKEN_BUDGET_EXCEEDED: True,
    FinalStatus.COST_BUDGET_EXCEEDED: True,
}

assert set(_EXECUTION_FACT_STATUSES) == set(FinalStatus), "Unclassified FinalStatus member"


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


CANONICAL_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "Agent",
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "NotebookEdit",
        "Read",
        "Skill",
        "Task",
        "TodoWrite",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)
"""The tool names ``allowed_tools`` / ``disallowed_tools`` accept.

The coding tools the Claude Code CLI 2.1.216 (claude-agent-sdk 0.2.124) lists in its
``system/init`` message, plus ``Agent``, ``Glob``, ``Grep`` and ``TodoWrite``, which
that CLI does not list but the task corpus names. ``Task`` and ``Agent`` both name the
subagent tool.
"""

TOOL_NAME_ALIASES: Final[dict[str, str]] = {"Task": "Agent"}
"""Canonical tool name -> the canonical name of the same tool (an older spelling)."""

READ_ONLY_DENIED_TOOLS: Final[tuple[str, ...]] = ("Write", "Edit", "Bash")
"""The Claude tool names every harness that maps ``permission_mode: plan`` denies."""

assert set(READ_ONLY_DENIED_TOOLS) <= CANONICAL_TOOL_NAMES, "READ_ONLY_DENIED_TOOLS must be canonical tool names"


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
    OPENCODE = "opencode"
    PI = "pi"
    NONE = "none"  # Agentless / system task — no coding agent runs; success criteria do all the work.
    UNKNOWN = "unknown"  # Used when agent type cannot be determined (e.g., task loading failure)


class AgentState(StrEnum):
    """Possible states of the agent during execution."""

    WORKING = "working"
    WAITING_FOR_USER = "waiting_for_user"
    CODE_PROPOSAL = "code_proposal"
    FINISHED = "finished"
    ERROR = "error"
