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


def test_claude_agent_disallowed_tools_passed_to_sdk_options():
    """Test that disallowed_tools from AgentConfig reaches ClaudeAgentOptions."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write", "Bash"],
        disallowed_tools=["TodoWrite", "Agent"],
    )

    agent = ClaudeCodeAgent(config)
    assert agent.config.disallowed_tools == ["TodoWrite", "Agent"]


def test_claude_agent_disallowed_tools_defaults_to_none():
    """Test that disallowed_tools defaults to None when not specified."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )

    agent = ClaudeCodeAgent(config)
    assert agent.config.disallowed_tools is None


async def _capture_sdk_options(agent: ClaudeCodeAgent) -> "list":
    """Run one communicate() turn with a mocked query() and return captured options list."""
    import tempfile

    captured_options: list = []

    class ResultMessage:
        def __init__(self, session_id: str = "s-1") -> None:
            self.session_id = session_id
            self.usage = {"input_tokens": 1, "output_tokens": 1}
            self.total_cost_usd = 0.0
            self.num_turns = 1
            self.is_error = False
            self.result = "Done"

    class AssistantMessage:
        def __init__(self) -> None:
            self.content = "ok"
            self.model = "mock-model"

    async def mock_query(prompt, options):
        captured_options.append(options)
        yield AssistantMessage()
        yield ResultMessage()

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)
        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            await agent.communicate("hello")

    return captured_options


@pytest.mark.asyncio
async def test_claude_agent_tool_search_always_disallowed_when_config_empty():
    """ToolSearch is always injected into disallowed_tools even when config specifies none."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].disallowed_tools == ["ToolSearch"]
    # Config itself must not be mutated.
    assert agent.config.disallowed_tools is None


@pytest.mark.asyncio
async def test_claude_agent_tool_search_appended_to_user_disallowed_tools():
    """User-specified disallowed_tools are preserved and ToolSearch is appended."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        disallowed_tools=["TodoWrite", "Agent"],
    )
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].disallowed_tools == ["TodoWrite", "Agent", "ToolSearch"]
    # Config itself must not be mutated.
    assert agent.config.disallowed_tools == ["TodoWrite", "Agent"]


@pytest.mark.asyncio
async def test_claude_agent_tool_search_not_duplicated():
    """If user already lists ToolSearch, it is not duplicated."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        disallowed_tools=["ToolSearch", "Agent"],
    )
    agent = ClaudeCodeAgent(config)

    captured_options = await _capture_sdk_options(agent)

    assert captured_options[0].disallowed_tools == ["ToolSearch", "Agent"]


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

    # Test ResultMessage (mock object with correct class name — SDK uses "result" attribute)
    class ResultMessage:
        def __init__(self, result, is_error=False):
            self.result = result
            self.is_error = is_error

    messages = [ResultMessage("File written successfully", is_error=False)]
    formatted = agent._format_messages(messages)
    assert "[RESULT - SUCCESS] File written successfully" in formatted

    # Test error result
    messages = [ResultMessage("File not found", is_error=True)]
    formatted = agent._format_messages(messages)
    assert "[RESULT - ERROR] File not found" in formatted


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
        def __init__(self, result, is_error=False):
            self.result = result
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


@pytest.mark.asyncio
async def test_claude_agent_session_resumption():
    """Test that session_id from first communicate() is passed as resume on subsequent calls."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        # Track options passed to query() across calls
        captured_options = []

        class ResultMessage:
            def __init__(self, session_id):
                self.session_id = session_id
                self.usage = {"input_tokens": 10, "output_tokens": 5}
                self.total_cost_usd = 0.001
                self.num_turns = 1
                self.is_error = False
                self.result = "Done"

        class AssistantMessage:
            def __init__(self):
                self.content = "I did the thing."
                self.model = "mock-model"

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield AssistantMessage()
            yield ResultMessage(session_id="test-session-abc")

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            # First call: no session_id yet
            await agent.communicate("first prompt")
            assert captured_options[0].resume is None
            assert agent._session_id == "test-session-abc"

            # Second call: should pass session_id as resume
            await agent.communicate("second prompt")
            assert captured_options[1].resume == "test-session-abc"


@pytest.mark.asyncio
async def test_claude_agent_session_resumption_none_degrades_gracefully():
    """When SDK returns session_id=None, agent should degrade to a fresh session."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        captured_options = []

        class ResultMessage:
            def __init__(self, session_id):
                self.session_id = session_id
                self.usage = {"input_tokens": 10, "output_tokens": 5}
                self.total_cost_usd = 0.001
                self.num_turns = 1
                self.is_error = False
                self.result = "Done"

        class AssistantMessage:
            def __init__(self):
                self.content = "I did the thing."
                self.model = "mock-model"

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield AssistantMessage()
            yield ResultMessage(session_id=None)

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            await agent.communicate("first prompt")
            assert agent._session_id is None

            # Second call: resume should be None (fresh session)
            await agent.communicate("second prompt")
            assert captured_options[1].resume is None


@pytest.mark.asyncio
async def test_claude_agent_session_rotation():
    """When SDK returns a different session_id on second call, agent should use the new one."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        captured_options = []
        call_count = 0

        class ResultMessage:
            def __init__(self, session_id):
                self.session_id = session_id
                self.usage = {"input_tokens": 10, "output_tokens": 5}
                self.total_cost_usd = 0.001
                self.num_turns = 1
                self.is_error = False
                self.result = "Done"

        class AssistantMessage:
            def __init__(self):
                self.content = "I did the thing."
                self.model = "mock-model"

        async def mock_query(prompt, options):
            nonlocal call_count
            captured_options.append(options)
            yield AssistantMessage()
            # Return different session_id on each call
            call_count += 1
            yield ResultMessage(session_id=f"session-{call_count}")

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            await agent.communicate("first prompt")
            assert agent._session_id == "session-1"

            await agent.communicate("second prompt")
            assert captured_options[1].resume == "session-1"
            assert agent._session_id == "session-2"

            # Third call should use the rotated session_id
            await agent.communicate("third prompt")
            assert captured_options[2].resume == "session-2"


@pytest.mark.asyncio
async def test_claude_agent_session_retained_on_error():
    """On error, _session_id should retain its value from the last successful result."""
    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
    )
    agent = ClaudeCodeAgent(config)

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        class ResultMessage:
            def __init__(self, session_id):
                self.session_id = session_id
                self.usage = {"input_tokens": 10, "output_tokens": 5}
                self.total_cost_usd = 0.001
                self.num_turns = 1
                self.is_error = False
                self.result = "Done"

        class AssistantMessage:
            def __init__(self):
                self.content = "I did the thing."
                self.model = "mock-model"

        # First call succeeds and sets session_id
        async def mock_query_ok(prompt, options):
            yield AssistantMessage()
            yield ResultMessage(session_id="good-session")

        with patch("coder_eval.agents.claude_code_agent.query", mock_query_ok):
            await agent.communicate("first prompt")
            assert agent._session_id == "good-session"

        # Second call raises an error mid-stream
        async def mock_query_error(prompt, options):
            raise RuntimeError("SDK connection lost")
            yield

        with (
            patch("coder_eval.agents.claude_code_agent.query", mock_query_error),
            pytest.raises(RuntimeError, match="SDK connection lost"),
        ):
            await agent.communicate("second prompt")

        # session_id should still be the value from the successful call
        assert agent._session_id == "good-session"
