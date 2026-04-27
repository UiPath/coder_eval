"""Agent-specific exceptions and crash-reason formatting helpers."""

CRASH_REASON_MAX_CHARS = 200


def format_timeout_reason(timeout_seconds: float) -> str:
    """Canonical ``crash_reason`` string for an agent-turn timeout (integer seconds)."""
    return f"Agent turn timed out after {timeout_seconds:.0f}s"


def truncate_crash_message(message: str, *, limit: int = CRASH_REASON_MAX_CHARS) -> str:
    """Cap a crash-reason string with a single Unicode ellipsis on overflow."""
    return message if len(message) <= limit else message[:limit] + "…"


class AgentCrashError(RuntimeError):
    """Mid-turn agent failure; routed to AGENT_CRASH by isinstance."""
