"""Agent-specific exceptions and crash-reason formatting helpers."""

CRASH_REASON_MAX_CHARS = 200


def format_timeout_reason(timeout_seconds: float) -> str:
    """Canonical ``crash_reason`` string for an agent-turn timeout (integer seconds)."""
    return f"Agent turn timed out after {timeout_seconds:.0f}s"


def truncate_crash_message(message: str, *, limit: int = CRASH_REASON_MAX_CHARS) -> str:
    """Cap a crash-reason string with a single Unicode ellipsis on overflow."""
    return message if len(message) <= limit else message[:limit] + "…"


class AgentCrashError(RuntimeError):
    """Mid-turn agent failure; routed to AGENT_CRASH by isinstance.

    ``tool_calls`` counts the tool calls the crashed attempt made. An ``AGENT_CRASH``
    with one or more is not retried, because a retry would run on a changed sandbox.
    """

    def __init__(self, message: str = "", tool_calls: int = 0) -> None:
        super().__init__(message)
        self.tool_calls = tool_calls


class AgentConfigError(RuntimeError):
    """Agent prerequisite missing (env var, SDK path, build artifact). Non-retryable.

    Routed to ``AGENT_CONFIG_ERROR`` by isinstance — preferred over substring
    matching on a message, which can silently re-categorise a reworded
    RuntimeError as the retryable ``AGENT_API_ERROR``.
    """
