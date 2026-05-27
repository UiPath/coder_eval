"""Tests for debug logging in agent._log_message_debug and _format_messages block handling."""

import logging

import pytest
from claude_agent_sdk import AssistantMessage
from claude_agent_sdk.types import TextBlock, ToolUseBlock

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import AgentKind, parse_agent_config
from tests._path_helpers import tmp_subdir


@pytest.fixture
def agent():
    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    return ClaudeCodeAgent(config)


# --- Issue #2: Empty-string tool result content shows "(empty)" ---
# NOTE: Tool result logging is now handled by ToolResultEvent and LoggingStreamRenderer
# This test now verifies that _log_message_debug no longer directly logs UserMessages


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


def test_log_debug_tool_result_empty_string(agent, caplog):
    """Tool result logging is now handled by events, not direct logs."""
    block = _ToolResultBlock(tool_use_id="t1", content="", is_error=False)
    msg = _UserMessage(content=[block])

    with caplog.at_level(logging.DEBUG, logger="coder_eval.agents.claude_code_agent"):
        agent._log_message_debug(msg, "UserMessage")

    # No direct logging - handled by ToolResultEvent instead
    assert len(caplog.records) == 0


def test_log_debug_tool_result_none_content(agent, caplog):
    """Tool result logging is now handled by events, not direct logs."""
    block = _ToolResultBlock(tool_use_id="t1", content=None, is_error=False)
    msg = _UserMessage(content=[block])

    with caplog.at_level(logging.DEBUG, logger="coder_eval.agents.claude_code_agent"):
        agent._log_message_debug(msg, "UserMessage")

    # No direct logging - handled by ToolResultEvent instead
    assert len(caplog.records) == 0


# --- Issue #3: cost=$None in debug output ---
# NOTE: Result message logging is now handled by TurnCompleteEvent and LoggingStreamRenderer
# This test now verifies that _log_message_debug no longer directly logs ResultMessages


class _ResultMessage:
    """Mock ResultMessage with session_id, usage, total_cost_usd."""

    def __init__(self, usage=None, total_cost_usd=None, is_error=False, result=None):
        self.session_id = "sess_1"
        self.usage = usage
        self.total_cost_usd = total_cost_usd
        self.is_error = is_error
        self.result = result


def test_log_debug_result_cost_none(agent, caplog):
    """Result message logging is now handled by events, not direct logs."""
    msg = _ResultMessage(usage={"input_tokens": 100}, total_cost_usd=None)

    with caplog.at_level(logging.DEBUG, logger="coder_eval.agents.claude_code_agent"):
        agent._log_message_debug(msg, "ResultMessage")

    # No direct logging - handled by TurnCompleteEvent instead
    assert len(caplog.records) == 0


def test_log_debug_result_cost_present(agent, caplog):
    """Result message logging is now handled by events, not direct logs."""
    msg = _ResultMessage(usage={"input_tokens": 100}, total_cost_usd=0.05)

    with caplog.at_level(logging.DEBUG, logger="coder_eval.agents.claude_code_agent"):
        agent._log_message_debug(msg, "ResultMessage")

    # No direct logging - handled by TurnCompleteEvent instead
    assert len(caplog.records) == 0


# --- Issue #4: _format_messages renders block-based AssistantMessage as raw list ---
#
# Uses real claude_agent_sdk classes — _format_messages dispatches via
# ``isinstance`` against the SDK's AssistantMessage, so locally-defined mock
# classes with the same name would fall through to the unknown-type branch.


def _make_assistant_message(content, model="test-model"):
    """Build a real SDK AssistantMessage for isinstance-based dispatch."""
    return AssistantMessage(content=content, model=model)


def test_format_messages_block_based_assistant(agent):
    """AssistantMessage with block-based content (list of TextBlocks) should extract text properly."""
    msg = _make_assistant_message(content=[TextBlock(text="Hello from Claude")])

    formatted = agent._format_messages([msg])

    # Should show the actual text, not a raw list representation
    assert "[ASSISTANT] Hello from Claude" in formatted
    assert "TextBlock" not in formatted


def test_format_messages_block_based_mixed(agent):
    """AssistantMessage with mixed blocks should extract text and show tool uses."""
    msg = _make_assistant_message(
        content=[
            TextBlock(text="Let me read that file."),
            ToolUseBlock(id="tool_1", name="Read", input={"path": str(tmp_subdir("test.py"))}),
        ]
    )

    formatted = agent._format_messages([msg])

    assert "Let me read that file." in formatted


def test_format_messages_string_content_still_works(agent):
    """AssistantMessage with plain string content should still work."""
    msg = _make_assistant_message(content="Simple string response")
    formatted = agent._format_messages([msg])

    assert "[ASSISTANT] Simple string response" in formatted
