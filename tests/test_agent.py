"""Tests for the agent implementations."""

from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import ProcessError

from coder_eval.agent import AgentState
from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import AgentConfig, AgentKind


def test_claude_agent_initialization():
    """Test that Claude agent can be initialized."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write", "Bash"],
    )

    agent = ClaudeCodeAgent(config)

    assert agent.config == config
    assert agent.client is None
    assert agent.get_state() == AgentState.WORKING


@pytest.mark.asyncio
async def test_claude_agent_start():
    """Test that Claude agent can be started."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write"],
    )

    agent = ClaudeCodeAgent(config)

    # Create a temporary working directory
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        assert agent.working_directory == Path(tmpdir)
        # Client is created per-communicate call, not stored
        assert agent.get_state() == AgentState.WORKING

        # Clean up
        await agent.stop()


def test_claude_agent_file_change_detection():
    """Test file change detection logic."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)

    # Test detecting created files
    before = {}
    after = {"test.py": 1234567890.0}
    changes = agent._detect_file_changes(before, after)

    assert len(changes) == 1
    assert changes[0].path == "test.py"
    assert changes[0].operation == "created"

    # Test detecting modified files
    before = {"test.py": 1234567890.0}
    after = {"test.py": 1234567891.0}
    changes = agent._detect_file_changes(before, after)

    assert len(changes) == 1
    assert changes[0].path == "test.py"
    assert changes[0].operation == "modified"

    # Test detecting deleted files
    before = {"test.py": 1234567890.0}
    after = {}
    changes = agent._detect_file_changes(before, after)

    assert len(changes) == 1
    assert changes[0].path == "test.py"
    assert changes[0].operation == "deleted"


def test_claude_agent_message_formatting():
    """Test message formatting logic with SDK message objects."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)

    # Test AssistantMessage (mock object with correct class name)
    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    messages = [AssistantMessage("Hello, world!")]
    formatted = agent._format_messages(messages)
    assert "[ASSISTANT] Hello, world!" in formatted

    # Test ResultMessage (mock object with correct class name)
    class ResultMessage:
        def __init__(self, content, is_error=False):
            self.content = content
            self.is_error = is_error

    messages = [ResultMessage("File written successfully", is_error=False)]
    formatted = agent._format_messages(messages)
    assert "[RESULT - SUCCESS]" in formatted

    # Test error result
    messages = [ResultMessage("File not found", is_error=True)]
    formatted = agent._format_messages(messages)
    assert "[RESULT - ERROR]" in formatted


def test_claude_agent_should_ignore_path():
    """Test path ignoring logic."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)

    # Should ignore these paths
    assert agent._should_ignore_path(Path(".venv/bin/python"))
    assert agent._should_ignore_path(Path("__pycache__/module.pyc"))
    assert agent._should_ignore_path(Path(".git/config"))

    # Should not ignore these paths
    assert not agent._should_ignore_path(Path("src/main.py"))
    assert not agent._should_ignore_path(Path("tests/test_main.py"))


@pytest.mark.asyncio
async def test_claude_agent_lifecycle():
    """Test agent lifecycle (start -> stop)."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Start
        await agent.start(tmpdir)
        # Client is created per-communicate call, not stored during lifecycle
        assert agent.get_state() == AgentState.WORKING

        # Stop
        await agent.stop()
        assert agent.client is None
        assert agent.get_state() == AgentState.FINISHED


def test_claude_agent_message_formatting_edge_cases():
    """Test message formatting with various SDK message types and edge cases."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    # Test 1: Empty messages list
    formatted = agent._format_messages([])
    assert formatted == "[No output]"

    # Test 2: SystemMessage (should be filtered out)
    class SystemMessage:
        pass

    formatted = agent._format_messages([SystemMessage()])
    assert formatted == "[No output]"

    # Test 3: UserMessage (should be filtered out)
    class UserMessage:
        def __init__(self, content):
            self.content = content

    formatted = agent._format_messages([UserMessage("test")])
    assert formatted == "[No output]"

    # Test 4: StreamEvent with tool_use
    class StreamEvent:
        def __init__(self, event_type, name=None):
            self.type = event_type
            self.name = name

    formatted = agent._format_messages([StreamEvent("tool_use", "Read")])
    assert "[TOOL USE] Read" in formatted

    # Test 5: StreamEvent without tool_use (should be filtered)
    formatted = agent._format_messages([StreamEvent("thinking")])
    assert formatted == "[No output]"

    # Test 6: Unknown message type
    class CustomMessage:
        def __str__(self):
            return "custom content here"

    formatted = agent._format_messages([CustomMessage()])
    assert "[CustomMessage]" in formatted
    assert "custom content" in formatted

    # Test 7: Message without expected attributes (defensive getattr)
    class BareMessage:
        pass

    # Should not raise exception - uses defensive getattr
    formatted = agent._format_messages([BareMessage()])
    assert "[BareMessage]" in formatted

    # Test 8: Multiple message types in sequence
    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self, content, is_error=False):
            self.content = content
            self.is_error = is_error

    messages = [
        SystemMessage(),  # Filtered
        UserMessage("user input"),  # Filtered
        AssistantMessage("Hello from assistant"),
        ResultMessage("Operation successful", is_error=False),
        StreamEvent("tool_use", "Bash"),
        StreamEvent("thinking"),  # Filtered
    ]
    formatted = agent._format_messages(messages)

    assert "[ASSISTANT] Hello from assistant" in formatted
    assert "[RESULT - SUCCESS] Operation successful" in formatted
    assert "[TOOL USE] Bash" in formatted
    assert "SystemMessage" not in formatted
    assert "user input" not in formatted
    assert "thinking" not in formatted

    # Test 9: ResultMessage with error
    formatted = agent._format_messages([ResultMessage("File not found", is_error=True)])
    assert "[RESULT - ERROR] File not found" in formatted

    # Test 10: AssistantMessage with empty content
    formatted = agent._format_messages([AssistantMessage("")])
    # Empty content should be skipped
    assert formatted == "[No output]"

    # Test 11: Multiple AssistantMessages
    messages = [
        AssistantMessage("First response"),
        AssistantMessage("Second response"),
    ]
    formatted = agent._format_messages(messages)
    assert "[ASSISTANT] First response" in formatted
    assert "[ASSISTANT] Second response" in formatted
    assert formatted.count("[ASSISTANT]") == 2


@pytest.mark.asyncio
async def test_claude_agent_process_error_includes_stderr():
    """Test that ProcessError is caught and its stderr is included in RuntimeError."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        # query() returns an async generator, so mock must be one too
        async def mock_query(*args, **kwargs):
            raise ProcessError("process failed", exit_code=1, stderr="Error: invalid config")
            yield  # makes this an async generator

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            pytest.raises(RuntimeError, match=r"CLI process failed \(exit code 1\): Error: invalid config"),
        ):
            await agent.communicate("do something")

        assert agent.get_state() == AgentState.ERROR


@pytest.mark.asyncio
async def test_claude_agent_process_error_no_stderr_at_all():
    """Test that ProcessError with no stderr and no stderr_lines shows sentinel message."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def mock_query(*args, **kwargs):
            raise ProcessError("process failed", exit_code=None, stderr=None)
            yield  # makes this an async generator

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            pytest.raises(RuntimeError, match=r"CLI process failed \(exit code None\): No stderr captured"),
        ):
            await agent.communicate("do something")
