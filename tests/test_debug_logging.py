"""Tests for debug logging in ClaudeCodeAgent._log_message_debug and _format_messages block handling."""

import logging

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import AgentConfig, AgentKind


@pytest.fixture
def agent():
    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    return ClaudeCodeAgent(config)


# --- Issue #2: Empty-string tool result content shows "(empty)" ---


class _ToolResultBlock:
    """Mock ToolResultBlock with tool_use_id and is_error attributes."""

    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class _UserMessage:
    """Mock UserMessage with content list and tool_use_result attribute."""

    def __init__(self, content):
        self.content = content
        self.tool_use_result = True  # marks as UserMessage for duck typing


def test_log_debug_tool_result_empty_string(caplog):
    """Empty-string tool result content should NOT be logged as '(empty)'."""
    block = _ToolResultBlock(tool_use_id="t1", content="", is_error=False)
    msg = _UserMessage(content=[block])

    with caplog.at_level(logging.DEBUG, logger="coder_eval.agents.claude_code_agent"):
        ClaudeCodeAgent._log_message_debug(msg, "UserMessage")

    assert len(caplog.records) == 1
    assert "(empty)" not in caplog.records[0].message


def test_log_debug_tool_result_none_content(caplog):
    """None tool result content should be logged as '(empty)'."""
    block = _ToolResultBlock(tool_use_id="t1", content=None, is_error=False)
    msg = _UserMessage(content=[block])

    with caplog.at_level(logging.DEBUG, logger="coder_eval.agents.claude_code_agent"):
        ClaudeCodeAgent._log_message_debug(msg, "UserMessage")

    assert len(caplog.records) == 1
    assert "(empty)" in caplog.records[0].message


# --- Issue #3: cost=$None in debug output ---


class _ResultMessage:
    """Mock ResultMessage with session_id, usage, total_cost_usd."""

    def __init__(self, usage=None, total_cost_usd=None, is_error=False, result=None):
        self.session_id = "sess_1"
        self.usage = usage
        self.total_cost_usd = total_cost_usd
        self.is_error = is_error
        self.result = result


def test_log_debug_result_cost_none(caplog):
    """When cost is None, debug log should NOT contain '$None'."""
    msg = _ResultMessage(usage={"input_tokens": 100}, total_cost_usd=None)

    with caplog.at_level(logging.DEBUG, logger="coder_eval.agents.claude_code_agent"):
        ClaudeCodeAgent._log_message_debug(msg, "ResultMessage")

    assert len(caplog.records) == 1
    assert "$None" not in caplog.records[0].message


def test_log_debug_result_cost_present(caplog):
    """When cost is present, debug log should show the dollar amount."""
    msg = _ResultMessage(usage={"input_tokens": 100}, total_cost_usd=0.05)

    with caplog.at_level(logging.DEBUG, logger="coder_eval.agents.claude_code_agent"):
        ClaudeCodeAgent._log_message_debug(msg, "ResultMessage")

    assert len(caplog.records) == 1
    assert "$0.05" in caplog.records[0].message


# --- Issue #4: _format_messages renders block-based AssistantMessage as raw list ---


class _TextBlock:
    def __init__(self, text):
        self.text = text
        self.type = "text"


class _ToolUseBlock:
    def __init__(self, name, tool_id, tool_input):
        self.name = name
        self.id = tool_id
        self.input = tool_input
        self.type = "tool_use"


def _make_assistant_message(content, model="test-model"):
    """Create a mock AssistantMessage with the correct class name for dispatch."""

    class AssistantMessage:
        def __init__(self, content, model):
            self.content = content
            self.model = model

    return AssistantMessage(content, model)


def test_format_messages_block_based_assistant(agent):
    """AssistantMessage with block-based content (list of TextBlocks) should extract text properly."""
    msg = _make_assistant_message(content=[_TextBlock("Hello from Claude")])

    formatted = agent._format_messages([msg])

    # Should show the actual text, not a raw list representation
    assert "[ASSISTANT] Hello from Claude" in formatted
    assert "TextBlock" not in formatted


def test_format_messages_block_based_mixed(agent):
    """AssistantMessage with mixed blocks should extract text and show tool uses."""
    msg = _make_assistant_message(
        content=[
            _TextBlock("Let me read that file."),
            _ToolUseBlock("Read", "tool_1", {"path": "/tmp/test.py"}),
        ]
    )

    formatted = agent._format_messages([msg])

    assert "Let me read that file." in formatted


def test_format_messages_string_content_still_works(agent):
    """AssistantMessage with plain string content should still work."""
    msg = _make_assistant_message(content="Simple string response")
    formatted = agent._format_messages([msg])

    assert "[ASSISTANT] Simple string response" in formatted
