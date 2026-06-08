"""Tests for _format_messages block handling.

Per-SDK-message debug logging (the old ``_log_message_debug``) has been removed:
the agent now logs its trajectory solely through the standardized event stream
(``ToolEndEvent`` / ``AgentEndEvent`` → ``LoggingStreamRenderer``), so there is
no agent-specific debug-dump path left to test here.
"""

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


# --- _format_messages renders block-based AssistantMessage as readable text ---
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
