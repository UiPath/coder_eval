"""Tests for streaming callback integration in ClaudeCodeAgent."""

from unittest.mock import MagicMock, patch

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import AgentConfig, AgentKind
from coder_eval.streaming.callbacks import TaskScopedCallback
from coder_eval.streaming.events import (
    StreamEvent,
    TextChunkEvent,
    ToolCallEvent,
    ToolResultEvent,
)


class CollectingCallback:
    """Collects streaming events for assertion."""

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)


def _make_tool_use_block(name: str = "Bash", tool_id: str = "tool_1", input_data: dict | None = None):
    """Create a mock ToolUseBlock."""
    block = MagicMock()
    block.name = name
    block.id = tool_id
    block.input = input_data or {"command": "echo hello"}
    return block


def _make_text_block(text: str = "I'll help"):
    """Create a mock TextBlock."""
    block = MagicMock()
    block.text = text
    del block.name
    del block.id
    del block.input
    return block


def _make_assistant_message(content_blocks: list):
    """Create a mock AssistantMessage."""
    msg = MagicMock()
    msg.content = content_blocks
    msg.model = "claude-sonnet-4-20250514"
    del msg.tool_use_result
    del msg.session_id
    return msg


def _make_tool_result_block(tool_use_id: str = "tool_1", is_error: bool = False, content: str = "hello"):
    """Create a mock ToolResultBlock."""
    block = MagicMock()
    block.tool_use_id = tool_use_id
    block.is_error = is_error
    block.content = content
    return block


def _make_user_message(content_blocks: list):
    """Create a mock UserMessage with tool results."""
    msg = MagicMock()
    msg.content = content_blocks
    msg.tool_use_result = True
    del msg.model
    del msg.session_id
    return msg


def _make_result_message():
    """Create a mock ResultMessage."""
    msg = MagicMock()
    msg.session_id = "session_1"
    msg.usage = {"input_tokens": 100, "output_tokens": 50}
    msg.total_cost_usd = 0.01
    msg.is_error = False
    msg.result = "done"
    del msg.model
    del msg.tool_use_result
    return msg


@pytest.mark.asyncio
async def test_communicate_emits_tool_call_event():
    """Agent emits ToolCallEvent when processing a ToolUseBlock."""
    tool_block = _make_tool_use_block(name="Bash", tool_id="t1", input_data={"command": "ls"})
    result_block = _make_tool_result_block(tool_use_id="t1", content="file.txt")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=callback)

    tool_call_events = [e for e in callback.events if isinstance(e, ToolCallEvent)]
    assert len(tool_call_events) == 1
    assert tool_call_events[0].tool_name == "Bash"
    assert tool_call_events[0].tool_id == "t1"
    # Agent uses config.type.value as task_id; orchestrator wraps with TaskScopedCallback to fix this
    assert tool_call_events[0].task_id == "claude-code"


@pytest.mark.asyncio
async def test_communicate_emits_tool_result_event():
    """Agent emits ToolResultEvent when processing a ToolResultBlock."""
    tool_block = _make_tool_use_block(name="Read", tool_id="t2")
    result_block = _make_tool_result_block(tool_use_id="t2", content="file content here")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Read"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=callback)

    result_events = [e for e in callback.events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].success is True
    assert "file content" in result_events[0].result_preview


@pytest.mark.asyncio
async def test_communicate_emits_text_chunk_event():
    """Agent emits TextChunkEvent for assistant text blocks."""
    text_block = _make_text_block("I will create the file now.")
    tool_block = _make_tool_use_block(name="Write", tool_id="t3")
    result_block = _make_tool_result_block(tool_use_id="t3", content="ok")

    messages = [
        _make_assistant_message([text_block, tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Write"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=callback)

    text_events = [e for e in callback.events if isinstance(e, TextChunkEvent)]
    assert len(text_events) == 1
    assert "create the file" in text_events[0].text


@pytest.mark.asyncio
async def test_communicate_works_without_callback():
    """Agent works normally when stream_callback is None (no regression)."""
    tool_block = _make_tool_use_block(name="Bash", tool_id="t4")
    result_block = _make_tool_result_block(tool_use_id="t4", content="ok")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        turn = await agent.communicate("test prompt")  # No callback

    assert turn is not None
    assert len(turn.commands) == 1


@pytest.mark.asyncio
async def test_task_scoped_callback_fixes_agent_task_id():
    """TaskScopedCallback overrides agent's task_id with the real task ID."""
    tool_block = _make_tool_use_block(name="Bash", tool_id="t5", input_data={"command": "pwd"})
    result_block = _make_tool_result_block(tool_use_id="t5", content="/workspace")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    inner_callback = CollectingCallback()
    scoped_callback = TaskScopedCallback(inner_callback, task_id="real-eval-task-42")

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=scoped_callback)

    # All events should now carry the real task ID, not 'claude-code'
    assert len(inner_callback.events) > 0
    for event in inner_callback.events:
        assert event.task_id == "real-eval-task-42", f"Event {type(event).__name__} has wrong task_id: {event.task_id}"
