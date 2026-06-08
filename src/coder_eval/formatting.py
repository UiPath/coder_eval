"""Pretty-print SDK payloads for display in logs and stream events."""

from __future__ import annotations

import json
import logging
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    SystemMessage,
    UserMessage,
)


logger = logging.getLogger(__name__)


def format_messages(
    messages: list[Message],
    *,
    warned_unknown_types: set[str],
    log: logging.Logger | logging.LoggerAdapter[Any] = logger,
) -> str:
    """Render a list of SDK messages as a readable transcript string.

    System and user messages are filtered out; assistant text, result status,
    and ``tool_use`` stream events are tagged. Unknown message types emit only a
    bare ``[TypeName]`` tag — never a truncated ``__repr__``, which could leak
    unmatched braces into the transcript persisted to ``task.json``.

    Uses ``isinstance`` (not type-name equality) so ``SystemMessage`` subclasses
    (e.g. ``TaskStartedMessage``) hit the ``SystemMessage`` arm per their drop-in
    contract.

    Args:
        messages: SDK message objects from a turn.
        warned_unknown_types: Mutable set used to deduplicate the
            "unhandled SDK message type" warning across calls; the caller owns
            it (e.g. one per agent instance) so the warning fires once per type.
        log: Logger (or adapter) for that warning; defaults to this module's logger.

    Returns:
        The formatted transcript, or ``"[No output]"`` when nothing was emitted.
    """
    formatted_parts = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            continue

        if isinstance(msg, UserMessage):
            continue

        if isinstance(msg, AssistantMessage):
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                for block in content:
                    text = getattr(block, "text", None)
                    if text:
                        formatted_parts.append(f"[ASSISTANT] {text}")
            elif content:
                formatted_parts.append(f"[ASSISTANT] {content}")
            continue

        if isinstance(msg, ResultMessage):
            result_text = getattr(msg, "result", "") or ""
            is_error = getattr(msg, "is_error", False)
            status = "ERROR" if is_error else "SUCCESS"
            formatted_parts.append(f"[RESULT - {status}] {result_text}")
            continue

        # StreamEvent isn't exported by the SDK — duck-type on ``type``.
        event_type = getattr(msg, "type", None)
        if event_type == "tool_use":
            tool_name = getattr(msg, "name", "unknown")
            formatted_parts.append(f"[TOOL USE] {tool_name}")
            continue

        type_name = type(msg).__name__
        # StreamEvent is a known SDK type used for token-delta capture
        # elsewhere; don't surface it as an "unhandled" warning here.
        if type_name == "StreamEvent":
            continue
        if type_name not in warned_unknown_types:
            warned_unknown_types.add(type_name)
            log.warning(
                "Unhandled SDK message type %s in format_messages — "
                "extend the isinstance chain when the SDK adds new types.",
                type_name,
            )
        formatted_parts.append(f"[{type_name}]")

    return "\n".join(formatted_parts) if formatted_parts else "[No output]"


def format_payload(value: Any, *, max_chars: int = 800) -> str:
    """Render a tool parameter or result payload as a display string.

    JSON-shaped payloads (dict, list, or a string/MCP-block whose first
    non-whitespace character is ``{`` or ``[``) are pretty-printed with
    ``indent=2``. Everything else — plain text, strings with prefix noise —
    falls through to ``str()``. Output is capped at ``max_chars`` with a
    visible ``…(N more chars)`` marker.

    A companion parser, ``ClaudeCodeAgent._try_parse_json_value``, handles
    the telemetry path (``result_data`` capture) with stricter heuristics
    for prefix noise; this helper is intentionally leaner since the fallback
    to ``str()`` is harmless for display.
    """
    parsed = _extract_json(value)
    if parsed is not None:
        return _truncate(json.dumps(parsed, indent=2, default=str), max_chars)
    if value is None:
        return ""
    return _truncate(str(value), max_chars)


def _extract_json(value: Any) -> dict[str, Any] | list[Any] | None:
    """Return the non-empty JSON dict/list inside ``value``, else None."""
    if isinstance(value, dict):
        return value or None
    if isinstance(value, list):
        if value and all(
            isinstance(v, dict) and v.get("type") == "text" and isinstance(v.get("text"), str) for v in value
        ):
            return _parse_json_string("".join(v["text"] for v in value))
        return value or None
    if isinstance(value, str):
        return _parse_json_string(value)
    return None


def _parse_json_string(text: str) -> dict[str, Any] | list[Any] | None:
    """Parse ``text`` as JSON when it starts (after ``lstrip``) with ``{`` or
    ``[``. Tolerates trailing garbage via ``raw_decode``. Rejects empty
    containers so accidental matches don't suppress the ``str()`` fallback.
    Does NOT strip leading non-whitespace prefix lines — strings like
    ``"Warning: foo\\n{...}"`` fall through to ``str()``.
    """
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list)) and parsed:
        return parsed
    return None


def _truncate(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars``, appending a visible marker."""
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return f"{text[:max_chars]}…({dropped} more chars)"


def format_token_usage(usage: Any) -> str:
    """Format TokenUsage into a short display string like 'in=1234, out=567'.

    Returns empty string if usage is None or has no token counts.
    Includes cache_read_input_tokens if non-zero.
    """
    if usage is None:
        return ""
    try:
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        if not input_tokens and not output_tokens:
            return ""
        parts = [f"in={input_tokens}", f"out={output_tokens}"]
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        if cached:
            parts.append(f"cached={cached}")
        return ", ".join(parts)
    except (AttributeError, TypeError):
        return ""
