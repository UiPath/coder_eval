"""Tests for CommandTelemetry.result_data capture of structured tool results."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent, _ClaudeDecoder
from coder_eval.models import AgentKind, CommandTelemetry, TimingBasis, parse_agent_config
from coder_eval.streaming.emitter import TurnEmitter
from coder_eval.streaming.events import AgentEndStatus
from coder_eval.testing import ScriptedClock
from tests._fixtures.golden_streams.claude_fixtures import AssistantMessage, ToolUseBlock, UserMessage


def _resolve(tool_id: str, content: Any, tool_name: str = "Bash") -> CommandTelemetry:
    """Open one tool through the Claude decoder, resolve it with ``content``, and return its record."""
    emitter = TurnEmitter(
        task_id="t",
        iteration=1,
        prompt="go",
        model="m",
        basis=TimingBasis.TURN_CLOCK,
        clock=ScriptedClock(datetime(2026, 1, 1)),
        sinks=[],
    )
    emitter.begin()
    agent = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE))
    decoder = _ClaudeDecoder(agent, emitter, effective_model="m")
    decoder(AssistantMessage([ToolUseBlock(tool_id, tool_name, {})], message_id="m1"))
    decoder(UserMessage(tool_id, False, content))
    (cmd,) = decoder.end(AgentEndStatus.COMPLETED).record.commands
    return cmd


def test_command_telemetry_result_data_defaults_to_none() -> None:
    cmd = CommandTelemetry(
        tool_name="Bash",
        tool_id="toolu_1",
        timestamp=datetime.now(),
    )
    assert cmd.result_data is None


def test_try_parse_json_value_returns_dict_for_json_object() -> None:
    parsed = ClaudeCodeAgent._try_parse_json_value('{"a":1,"b":"x"}')
    assert parsed == {"a": 1, "b": "x"}
    assert isinstance(parsed, dict)


def test_try_parse_json_value_returns_list_for_json_array() -> None:
    parsed = ClaudeCodeAgent._try_parse_json_value("[1,2,3]")
    assert parsed == [1, 2, 3]
    assert isinstance(parsed, list)


def test_try_parse_json_value_returns_list_for_array_of_objects() -> None:
    parsed = ClaudeCodeAgent._try_parse_json_value('[{"a":1},{"a":2}]')
    assert parsed == [{"a": 1}, {"a": 2}]


def test_try_parse_json_value_returns_none_for_primitive() -> None:
    assert ClaudeCodeAgent._try_parse_json_value('"hello"') is None


def test_try_parse_json_value_returns_none_for_plain_text() -> None:
    assert ClaudeCodeAgent._try_parse_json_value("hello world") is None


def test_try_parse_json_value_returns_none_for_malformed_json_object() -> None:
    assert ClaudeCodeAgent._try_parse_json_value('{"a":') is None


def test_try_parse_json_value_returns_none_for_malformed_json_array() -> None:
    assert ClaudeCodeAgent._try_parse_json_value("[1,2,") is None


def test_try_parse_json_value_returns_none_for_empty() -> None:
    assert ClaudeCodeAgent._try_parse_json_value("") is None


def test_try_parse_json_value_tolerates_leading_whitespace_for_object() -> None:
    assert ClaudeCodeAgent._try_parse_json_value('   {"a":1}') == {"a": 1}


def test_try_parse_json_value_tolerates_leading_whitespace_for_array() -> None:
    assert ClaudeCodeAgent._try_parse_json_value("   [1,2]") == [1, 2]


def test_tool_result_populates_result_data_for_json_object() -> None:
    tool_id = "toolu_json_obj"
    content = '{"a":1,"b":"x"}'

    cmd = _resolve(tool_id, content)
    assert cmd.result_data == {"a": 1, "b": "x"}
    assert cmd.result_summary == content


def test_tool_result_does_not_truncate_long_result_summary() -> None:
    tool_id = "toolu_long"
    content = "x" * 5000  # well past the old 200-char cap

    cmd = _resolve(tool_id, content)
    assert cmd.result_summary == content
    assert len(cmd.result_summary) == 5000


def test_tool_result_populates_result_data_for_json_array() -> None:
    tool_id = "toolu_json_arr"
    content = '[{"a":1}]'

    cmd = _resolve(tool_id, content)
    assert cmd.result_data == [{"a": 1}]


def test_tool_result_leaves_result_data_none_for_plain_text() -> None:
    tool_id = "toolu_plain"
    content = "hello world"

    cmd = _resolve(tool_id, content)
    assert cmd.result_data is None
    assert cmd.result_summary == "hello world"


def test_tool_result_populates_result_data_for_flow_debug_fixture() -> None:
    tool_id = "toolu_flow_debug"
    content = (
        '{"Code":"FlowDebug","Data":{"finalStatus":"Completed",'
        '"elementExecutions":[{"elementId":"e1","status":"Completed"}]}}'
    )

    cmd = _resolve(tool_id, content, tool_name="mcp__maestro__run_flow")
    assert cmd.result_data is not None
    assert isinstance(cmd.result_data, dict)
    assert cmd.result_data["Code"] == "FlowDebug"
    assert cmd.result_data["Data"]["elementExecutions"][0]["elementId"] == "e1"


def test_tool_result_handles_sdk_list_content_shape() -> None:
    """MCP tool results arrive as list[{'type': 'text', 'text': '...'}]; extract and parse."""
    tool_id = "toolu_mcp_flow_debug"
    content = [
        {"type": "text", "text": '{"Code":"FlowDebug","Data":{"finalStatus":"Completed"}}'},
    ]

    cmd = _resolve(tool_id, content, tool_name="mcp__maestro__run_flow")
    assert cmd.result_data == {"Code": "FlowDebug", "Data": {"finalStatus": "Completed"}}


def test_tool_result_concatenates_multiple_text_blocks() -> None:
    tool_id = "toolu_mcp_multi_text"
    content = [
        {"type": "text", "text": '{"a":'},
        {"type": "text", "text": '1,"b":2}'},
    ]

    cmd = _resolve(tool_id, content)
    assert cmd.result_data == {"a": 1, "b": 2}


def test_tool_result_list_without_text_blocks_yields_none() -> None:
    """An SDK list of only non-text blocks (e.g., images) produces no JSON."""
    tool_id = "toolu_mcp_image"
    content = [{"type": "image", "source": {"data": "..."}}]

    cmd = _resolve(tool_id, content)
    assert cmd.result_data is None


def test_tool_result_none_content_yields_none() -> None:
    tool_id = "toolu_none"

    cmd = _resolve(tool_id, None)
    assert cmd.result_data is None
    assert cmd.result_summary is None


def test_try_parse_json_value_extracts_json_after_prefix_line() -> None:
    """uip CLI prints a warning line before the JSON body — capture the JSON anyway."""
    content = (
        'Tool factory already registered for project type \'Flow\', skipping.\n{"Result": "Success", "Code": "Help"}'
    )
    assert ClaudeCodeAgent._try_parse_json_value(content) == {
        "Result": "Success",
        "Code": "Help",
    }


def test_try_parse_json_value_extracts_json_after_exit_code_prefix() -> None:
    content = (
        "Exit code 3\n"
        "Tool factory already registered for project type 'Flow', skipping.\n"
        '{"Result": "ValidationError", "Message": "missing folder-key"}'
    )
    assert ClaudeCodeAgent._try_parse_json_value(content) == {
        "Result": "ValidationError",
        "Message": "missing folder-key",
    }


def test_try_parse_json_value_tolerates_trailing_garbage() -> None:
    assert ClaudeCodeAgent._try_parse_json_value('{"a":1} [0.42s]') == {"a": 1}


def test_try_parse_json_value_stops_at_first_candidate_and_returns_none_if_it_fails() -> None:
    """Conservative: don't fall back to inner fragments after a failed first candidate.

    This protects against truncated payloads (e.g. `uip ... | head -60`) where
    a later `{...}` or `[]` inside the cut-off document would otherwise produce
    a misleading partial capture.
    """
    content = 'error at {bad token}, but later: {"ok": true}'
    assert ClaudeCodeAgent._try_parse_json_value(content) is None


def test_try_parse_json_value_ignores_inline_brackets_in_text() -> None:
    """Incidental `[]` mid-line (e.g. `items: list = []` in Python source) must not match."""
    content = (
        "     1\u2192from dotenv import load_dotenv\n"
        "     2\u2192from pydantic import BaseModel\n"
        "    15\u2192    items: list = []\n"
    )
    assert ClaudeCodeAgent._try_parse_json_value(content) is None


def test_try_parse_json_value_ignores_inline_braces_in_text() -> None:
    content = "status: {ok} — here's the dict: {'a': 1}"
    assert ClaudeCodeAgent._try_parse_json_value(content) is None


def test_try_parse_json_value_accepts_json_on_indented_line() -> None:
    """Leading whitespace on the JSON line is fine (raw_decode handles it)."""
    content = 'prefix line\n    {"ok": 1}'
    assert ClaudeCodeAgent._try_parse_json_value(content) == {"ok": 1}


def test_try_parse_json_value_rejects_empty_dict() -> None:
    assert ClaudeCodeAgent._try_parse_json_value("{}") is None


def test_try_parse_json_value_rejects_empty_list() -> None:
    assert ClaudeCodeAgent._try_parse_json_value("[]") is None


def test_try_parse_json_value_accepts_list_containing_empty_container() -> None:
    """`[{}]` or `[[]]` at the top level has one element — that counts as non-empty."""
    assert ClaudeCodeAgent._try_parse_json_value("[{}]") == [{}]
    assert ClaudeCodeAgent._try_parse_json_value("[[]]") == [[]]


def test_try_parse_json_value_ignores_json_beyond_first_200_chars() -> None:
    """If the JSON start is buried past the 200-char prefix window, skip it."""
    prefix = "x" * 250 + "\n"
    content = prefix + '{"ok": 1}'
    assert ClaudeCodeAgent._try_parse_json_value(content) is None


def test_try_parse_json_value_accepts_json_whose_body_extends_past_200_chars() -> None:
    """Only the START position must be within 200 chars; the body can be much larger."""
    long_value = "x" * 5000
    content = '{"data": "' + long_value + '"}'
    parsed = ClaudeCodeAgent._try_parse_json_value(content)
    assert isinstance(parsed, dict)
    assert parsed["data"] == long_value


def test_try_parse_json_value_returns_none_for_truncated_json_document() -> None:
    """A truncated outer doc with an intact inner fragment should NOT surface the fragment."""
    content = '{"Result":"Success","Data":{"Arguments":[],"Options":[{"Flags":"--help"'
    assert ClaudeCodeAgent._try_parse_json_value(content) is None


def test_try_parse_json_value_returns_first_valid_when_multiple_json_docs_present() -> None:
    content = '{"first": 1} --- {"second": 2}'
    assert ClaudeCodeAgent._try_parse_json_value(content) == {"first": 1}


def test_try_parse_json_value_returns_none_when_only_invalid_braces() -> None:
    assert ClaudeCodeAgent._try_parse_json_value("error: unexpected {token here}") is None


def test_command_telemetry_round_trips_through_json_dict() -> None:
    cmd = CommandTelemetry(
        tool_name="Bash",
        tool_id="toolu_rt_1",
        timestamp=datetime.now(),
        result_data={"Code": "FlowDebug", "Data": {"nested": [1, 2, 3]}},
    )
    dumped = cmd.model_dump_json()
    restored = CommandTelemetry.model_validate_json(dumped)
    assert restored.result_data == cmd.result_data


def test_command_telemetry_round_trips_through_json_list() -> None:
    cmd = CommandTelemetry(
        tool_name="Bash",
        tool_id="toolu_rt_2",
        timestamp=datetime.now(),
        result_data=[{"a": 1}, {"b": 2}],
    )
    dumped = cmd.model_dump_json()
    restored = CommandTelemetry.model_validate_json(dumped)
    assert restored.result_data == cmd.result_data
