"""Tests for coder_eval.formatting.format_payload."""

from __future__ import annotations

import json

from coder_eval.formatting import format_payload


def test_format_payload_pretty_prints_json_string() -> None:
    raw = '{"Result":"OK","Data":{"count":3,"items":["a","b","c"]}}'
    out = format_payload(raw)
    assert out.startswith("{\n")
    assert '"Result": "OK"' in out
    # Nested structure indented
    assert '  "Data": {' in out


def test_format_payload_pretty_prints_dict() -> None:
    out = format_payload({"file_path": "x.py", "offset": 10})
    parsed = json.loads(out)
    assert parsed == {"file_path": "x.py", "offset": 10}
    # indent=2 produces a leading newline inside the dict
    assert "\n" in out


def test_format_payload_pretty_prints_list() -> None:
    out = format_payload([{"id": 1}, {"id": 2}])
    assert out.startswith("[\n")
    assert '"id": 1' in out
    assert '"id": 2' in out


def test_format_payload_handles_mcp_content_blocks() -> None:
    """MCP-style `[{"type": "text", "text": "..."}]` should be joined and parsed."""
    content = [{"type": "text", "text": '{"Status":"OK","Rows":[1,2,3]}'}]
    out = format_payload(content)
    parsed = json.loads(out)
    assert parsed == {"Status": "OK", "Rows": [1, 2, 3]}


def test_format_payload_falls_back_to_str_for_plain_text() -> None:
    out = format_payload("file saved to /tmp/x.txt")
    assert out == "file saved to /tmp/x.txt"


def test_format_payload_tolerates_trailing_garbage() -> None:
    """uip CLI sometimes appends e.g. ` [0.42s]` after the JSON body."""
    raw = '{"ok": true} [0.42s]'
    out = format_payload(raw)
    assert out.startswith("{\n")
    assert '"ok": true' in out


def test_format_payload_rejects_empty_containers() -> None:
    """Bare `{}` or `[]` aren't evidence of structured output — pass through as-is."""
    assert format_payload("{}") == "{}"
    assert format_payload("[]") == "[]"
    assert format_payload({}) == "{}"
    assert format_payload([]) == "[]"


def test_format_payload_none() -> None:
    assert format_payload(None) == ""


def test_format_payload_truncates_long_output() -> None:
    raw = json.dumps({f"k{i}": f"v{i}" for i in range(200)})
    out = format_payload(raw, max_chars=200)
    assert len(out) > 200  # the truncation marker is appended
    assert "more chars)" in out
    # The rendered prefix is the pretty-printed JSON, capped at 200 chars
    assert out.startswith("{\n")


def test_format_payload_leaves_short_output_untouched() -> None:
    raw = "hello"
    assert format_payload(raw, max_chars=100) == "hello"


def test_format_payload_json_with_prefix_noise_not_parsed() -> None:
    """A valid JSON body on a later line should NOT be picked up — this is the
    intentional display-path trade-off (versus the stricter telemetry helper
    ``ClaudeCodeAgent._try_parse_json_value`` which does tolerate prefix lines)."""
    raw = 'Warning: Tool factory registered\n{"ok": true}'
    assert format_payload(raw) == raw


def test_format_payload_truncation_marker_survives_renderer_path() -> None:
    """Once the helper has appended its marker, callers must not re-truncate.

    The streaming renderer relies on ``result_preview`` already being capped,
    so the ``…(N more chars)`` suffix reaches the user intact. This test locks
    in the marker's presence on output that exceeded the budget.
    """
    raw = "x" * 2000
    out = format_payload(raw, max_chars=800)
    assert out.endswith("more chars)")
    # Marker must be in the *tail* — if a caller re-truncates to ``[:800]``
    # it would cut just before the marker and lose the (1200 more chars) hint.
    assert "…(1200 more chars)" in out


def test_format_payload_non_serializable_object() -> None:
    """Objects that aren't JSON/dict/list/str fall through to ``str()``."""

    class Custom:
        def __str__(self) -> str:
            return "custom-repr"

    assert format_payload(Custom()) == "custom-repr"
