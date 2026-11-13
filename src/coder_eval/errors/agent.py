"""Agent-specific exceptions and crash-reason formatting helpers."""

from .categories import ErrorCategory


CRASH_REASON_MAX_CHARS = 200


def format_timeout_reason(timeout_seconds: float) -> str:
    """Canonical ``crash_reason`` string for an agent-turn timeout (integer seconds)."""
    return f"Agent turn timed out after {timeout_seconds:.0f}s"


def truncate_crash_message(message: str, *, limit: int = CRASH_REASON_MAX_CHARS) -> str:
    """Cap a crash-reason string with a single Unicode ellipsis on overflow."""
    return message if len(message) <= limit else message[:limit] + "…"


class AgentCrashError(RuntimeError):
    """Mid-turn failure with an optional adapter-reported category; defaults to AGENT_CRASH."""

    def __init__(self, message: str = "", *, category: ErrorCategory | None = None) -> None:
        super().__init__(message)
        self.category = category


class AgentConfigError(RuntimeError):
    """Agent prerequisite missing (env var, SDK path, build artifact). Non-retryable.

    Routed to ``AGENT_CONFIG_ERROR`` by isinstance — preferred over substring
    matching on a message, which can silently re-categorise a reworded
    RuntimeError as the retryable ``AGENT_API_ERROR``.
    """
