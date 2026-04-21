"""Pretty-print SDK payloads for display in logs and stream events."""

from __future__ import annotations

import json
from typing import Any


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
