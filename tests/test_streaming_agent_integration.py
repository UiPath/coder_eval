"""Tests for streaming callback integration in ClaudeCodeAgent.

Migrated to the standardized event protocol: the agent is the *single* emitter
and produces a well-formed, Start/End-balanced event tree per ``communicate()``
call:

    AgentStartEvent -> (TurnStartEvent -> TextChunk/ToolStart/ToolEnd... -> TurnEndEvent)* -> AgentEndEvent

The ``EventCollector`` reduces that stream into a ``TurnRecord``. These tests
assert both the emission (well-formed tree) and the reduction.
"""

from unittest.mock import MagicMock, patch

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import AgentKind, parse_agent_config
from coder_eval.streaming.callbacks import TaskScopedCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnStartEvent,
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
    del block.thinking
    return block


def _make_assistant_message(content_blocks: list):
    """Create a mock AssistantMessage."""
    msg = MagicMock()
    msg.content = content_blocks
    msg.model = "claude-sonnet-4-20250514"
    del msg.tool_use_result
    del msg.session_id
    del msg.event
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
    del msg.event
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
    del msg.event
    return msg


def _assert_well_formed_tree(events: list[StreamEvent], *, expected_agent_status: AgentEndStatus) -> None:
    """Assert the event stream is a Start/End-balanced agent lifecycle tree.

    Exactly one AgentStart opens the stream and exactly one matching AgentEnd
    closes it (on every exit path); each TurnStart is balanced by a TurnEnd and
    each ToolStart by a ToolEnd.
    """
    assert events, "expected at least one event"
    assert isinstance(events[0], AgentStartEvent), f"first event is {type(events[0]).__name__}, expected AgentStart"
    assert isinstance(events[-1], AgentEndEvent), f"last event is {type(events[-1]).__name__}, expected AgentEnd"

    agent_starts = [e for e in events if isinstance(e, AgentStartEvent)]
    agent_ends = [e for e in events if isinstance(e, AgentEndEvent)]
    assert len(agent_starts) == 1
    assert len(agent_ends) == 1
    assert agent_ends[0].status == expected_agent_status

    turn_starts = [e for e in events if isinstance(e, TurnStartEvent)]
    turn_ends = [e for e in events if isinstance(e, TurnEndEvent)]
    assert len(turn_starts) == len(turn_ends), "every TurnStart needs a matching TurnEnd"

    tool_starts = [e for e in events if isinstance(e, ToolStartEvent)]
    tool_ends = [e for e in events if isinstance(e, ToolEndEvent)]
    assert len(tool_starts) == len(tool_ends), "every ToolStart needs a matching ToolEnd"


@pytest.mark.asyncio
async def test_communicate_emits_tool_start_event():
    """Agent emits ToolStartEvent (carrying CommandTelemetry) when processing a ToolUseBlock."""
    tool_block = _make_tool_use_block(name="Bash", tool_id="t1", input_data={"command": "ls"})
    result_block = _make_tool_result_block(tool_use_id="t1", content="file.txt")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=callback)

    _assert_well_formed_tree(callback.events, expected_agent_status=AgentEndStatus.COMPLETED)

    tool_start_events = [e for e in callback.events if isinstance(e, ToolStartEvent)]
    assert len(tool_start_events) == 1
    assert tool_start_events[0].tool.tool_name == "Bash"
    assert tool_start_events[0].tool.tool_id == "t1"
    # Agent uses config.type.value as task_id; orchestrator wraps with TaskScopedCallback to fix this
    assert tool_start_events[0].task_id == "claude-code"


@pytest.mark.asyncio
async def test_communicate_emits_tool_end_event():
    """Agent emits ToolEndEvent (status OK + resolved telemetry) when processing a ToolResultBlock."""
    tool_block = _make_tool_use_block(name="Read", tool_id="t2")
    result_block = _make_tool_result_block(tool_use_id="t2", content="file content here")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Read"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=callback)

    _assert_well_formed_tree(callback.events, expected_agent_status=AgentEndStatus.COMPLETED)

    end_events = [e for e in callback.events if isinstance(e, ToolEndEvent)]
    assert len(end_events) == 1
    assert end_events[0].status == ToolEndStatus.OK
    assert end_events[0].tool.tool_id == "t2"
    assert end_events[0].tool.result_status == "success"
    assert "file content" in (end_events[0].tool.result_summary or "")


@pytest.mark.asyncio
async def test_communicate_emits_tool_end_error_status():
    """An errored tool result is reported via ToolEndStatus.ERROR (not a separate event type)."""
    tool_block = _make_tool_use_block(name="Bash", tool_id="t_err", input_data={"command": "false"})
    result_block = _make_tool_result_block(tool_use_id="t_err", is_error=True, content="boom")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=callback)

    end_events = [e for e in callback.events if isinstance(e, ToolEndEvent)]
    assert len(end_events) == 1
    assert end_events[0].status == ToolEndStatus.ERROR
    assert end_events[0].tool.result_status == "error"


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

    config = parse_agent_config(
        type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Write"]
    )
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=callback)

    _assert_well_formed_tree(callback.events, expected_agent_status=AgentEndStatus.COMPLETED)

    text_events = [e for e in callback.events if isinstance(e, TextChunkEvent)]
    assert len(text_events) == 1
    assert "create the file" in text_events[0].text


@pytest.mark.asyncio
async def test_agent_start_carries_prompt_and_iteration():
    """The AgentStartEvent opens the stream with the prompt + iteration metadata."""
    tool_block = _make_tool_use_block(name="Bash", tool_id="ts")
    result_block = _make_tool_result_block(tool_use_id="ts", content="ok")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("hello world", stream_callback=callback)

    start = callback.events[0]
    assert isinstance(start, AgentStartEvent)
    assert start.prompt == "hello world"
    assert start.iteration == 1


@pytest.mark.asyncio
async def test_collector_reduces_stream_into_turn_record():
    """A standalone EventCollector fed the agent's stream rebuilds the same TurnRecord."""
    tool_block = _make_tool_use_block(name="Bash", tool_id="tc", input_data={"command": "ls"})
    result_block = _make_tool_result_block(tool_use_id="tc", content="file.txt")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    callback = CollectingCallback()

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        returned = await agent.communicate("test prompt", stream_callback=callback)

    # Replay the captured stream through a fresh collector and confirm it
    # reduces to a record consistent with what communicate() returned.
    collector = EventCollector()
    for event in callback.events:
        collector.on_event(event)
    rebuilt = collector.build_turn_record()

    assert rebuilt.user_input == "test prompt"
    assert len(rebuilt.commands) == 1
    assert rebuilt.commands[0].tool_id == "tc"
    assert len(returned.commands) == len(rebuilt.commands)


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

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
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
    """TaskScopedCallback overrides agent's task_id with the real task ID on every event."""
    tool_block = _make_tool_use_block(name="Bash", tool_id="t5", input_data={"command": "pwd"})
    result_block = _make_tool_result_block(tool_use_id="t5", content="/workspace")

    messages = [
        _make_assistant_message([tool_block]),
        _make_user_message([result_block]),
        _make_result_message(),
    ]

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions", allowed_tools=["Bash"])
    agent = ClaudeCodeAgent(config)
    agent.working_directory = MagicMock()

    inner_callback = CollectingCallback()
    scoped_callback = TaskScopedCallback(inner_callback, task_id="real-eval-task-42")

    async def fake_query(**kwargs):
        for msg in messages:
            yield msg

    with patch("coder_eval.agents.claude_code_agent.query", side_effect=fake_query):
        await agent.communicate("test prompt", stream_callback=scoped_callback)

    # All events (AgentStart -> ... -> AgentEnd) should now carry the real task ID.
    assert len(inner_callback.events) > 0
    for event in inner_callback.events:
        assert event.task_id == "real-eval-task-42", f"Event {type(event).__name__} has wrong task_id: {event.task_id}"
