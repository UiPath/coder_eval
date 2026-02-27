"""Tests for command telemetry status tracking (V2 fix)."""

import time

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import AgentConfig


def create_mock_sdk_messages():
    """Create mock SDK message classes matching real SDK structure.

    The SDK delivers tool results inside UserMessage.content as ToolResultBlock objects.
    - AssistantMessage: contains ToolUseBlock in content (duck-typed via 'content' + 'model')
    - UserMessage: contains ToolResultBlock in content (duck-typed via 'content' + 'tool_use_result')
    - ToolUseBlock: has 'id', 'name', 'input'
    - ToolResultBlock: has 'tool_use_id', 'is_error', 'content'
    """

    class ToolUseBlock:
        def __init__(self, tool_id, name, input_params):
            self.id = tool_id
            self.name = name
            self.input = input_params

    class ToolResultBlock:
        def __init__(self, tool_use_id, is_error, content):
            self.tool_use_id = tool_use_id
            self.is_error = is_error
            self.content = content

    class AssistantMessage:
        def __init__(self, content):
            self.content = content
            self.model = "mock-model"

    class UserMessage:
        """Wraps ToolResultBlock(s) as the SDK does."""

        def __init__(self, tool_use_id, is_error, content):
            block = ToolResultBlock(tool_use_id, is_error, content)
            self.content = [block]
            self.tool_use_result = {"tool_use_id": tool_use_id, "is_error": is_error, "content": content}

    return ToolUseBlock, AssistantMessage, UserMessage


class TestCommandTelemetryStatus:
    """Tests for accurate command status tracking."""

    @pytest.mark.asyncio
    async def test_command_telemetry_captures_success(self, tmp_path):
        """Verify successful command gets result_status='success' and duration."""
        tool_use_block_cls, assistant_message_cls, result_message_cls = create_mock_sdk_messages()

        # Simulate: ToolUseBlock(Bash) + ResultMessage(success)
        tool_id = "toolu_test123"
        tool_block = tool_use_block_cls(tool_id, "Bash", {"command": "ls"})
        assistant_msg = assistant_message_cls([tool_block])
        result_msg = result_message_cls(tool_id, False, "file1.py\nfile2.py")

        config = AgentConfig(type="claude-code")
        agent = ClaudeCodeAgent(config)

        import coder_eval.agents.claude_code_agent as agent_module

        async def mock_query(prompt, options):
            yield assistant_msg
            time.sleep(0.01)  # 10ms delay
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("List files")

            # Verify command telemetry
            assert len(turn.commands) == 1
            cmd = turn.commands[0]

            assert cmd.tool_name == "Bash"
            assert cmd.result_status == "success"
            assert cmd.error_message is None
            assert cmd.duration_ms is not None
            assert cmd.duration_ms > 0
            assert "file1.py" in cmd.result_summary or "file2.py" in cmd.result_summary

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_command_telemetry_captures_error(self, tmp_path):
        """Verify failed command gets result_status='error' and error message."""
        tool_use_block_cls, assistant_message_cls, result_message_cls = create_mock_sdk_messages()

        tool_id = "toolu_error456"
        tool_block = tool_use_block_cls(tool_id, "Read", {"file_path": "missing.txt"})
        assistant_msg = assistant_message_cls([tool_block])
        result_msg = result_message_cls(tool_id, True, "File not found: missing.txt")

        config = AgentConfig(type="claude-code")
        agent = ClaudeCodeAgent(config)

        import coder_eval.agents.claude_code_agent as agent_module

        async def mock_query(prompt, options):
            yield assistant_msg
            time.sleep(0.005)
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Read file")

            assert len(turn.commands) == 1
            cmd = turn.commands[0]

            assert cmd.tool_name == "Read"
            assert cmd.result_status == "error"
            assert cmd.error_message == "File not found: missing.txt"
            assert cmd.duration_ms is not None
            assert cmd.duration_ms > 0

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_command_telemetry_handles_missing_result(self, tmp_path):
        """Verify missing result sets status='unknown' and duration=0.0."""
        tool_use_block_cls, assistant_message_cls, _result_message_cls = create_mock_sdk_messages()

        tool_id = "toolu_orphan789"
        tool_block = tool_use_block_cls(tool_id, "Write", {"file_path": "test.py", "content": "code"})
        assistant_msg = assistant_message_cls([tool_block])

        config = AgentConfig(type="claude-code")
        agent = ClaudeCodeAgent(config)

        import coder_eval.agents.claude_code_agent as agent_module

        async def mock_query(prompt, options):
            yield assistant_msg
            # No result - agent interrupted!

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Write file")

            assert len(turn.commands) == 1
            cmd = turn.commands[0]

            assert cmd.tool_name == "Write"
            assert cmd.result_status == "unknown"
            assert cmd.error_message is None
            assert cmd.duration_ms == 0.0

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_command_telemetry_mixed_results(self, tmp_path):
        """Verify multiple commands with different results are tracked correctly."""
        tool_use_block_cls, assistant_message_cls, result_message_cls = create_mock_sdk_messages()

        tool1 = tool_use_block_cls("tool1", "Read", {"file_path": "exists.py"})
        tool2 = tool_use_block_cls("tool2", "Read", {"file_path": "missing.py"})
        tool3 = tool_use_block_cls("tool3", "Bash", {"command": "ls"})

        assistant_msg = assistant_message_cls([tool1, tool2, tool3])
        result1 = result_message_cls("tool1", False, "def foo(): pass")
        result2 = result_message_cls("tool2", True, "File not found")
        result3 = result_message_cls("tool3", False, "file.py")

        config = AgentConfig(type="claude-code")
        agent = ClaudeCodeAgent(config)

        import coder_eval.agents.claude_code_agent as agent_module

        async def mock_query(prompt, options):
            yield assistant_msg
            yield result1
            yield result2
            yield result3

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Multiple commands")

            assert len(turn.commands) == 3

            # Tool1: Success
            assert turn.commands[0].tool_name == "Read"
            assert turn.commands[0].result_status == "success"
            assert turn.commands[0].error_message is None

            # Tool2: Error
            assert turn.commands[1].tool_name == "Read"
            assert turn.commands[1].result_status == "error"
            assert turn.commands[1].error_message == "File not found"

            # Tool3: Success
            assert turn.commands[2].tool_name == "Bash"
            assert turn.commands[2].result_status == "success"
            assert turn.commands[2].error_message is None

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_command_telemetry_duration_accuracy(self, tmp_path):
        """Verify duration is calculated precisely using monotonic clock."""
        tool_use_block_cls, assistant_message_cls, result_message_cls = create_mock_sdk_messages()

        tool_id = "toolu_timing"
        tool_block = tool_use_block_cls(tool_id, "Bash", {"command": "sleep 0.05"})
        assistant_msg = assistant_message_cls([tool_block])
        result_msg = result_message_cls(tool_id, False, "")

        config = AgentConfig(type="claude-code")
        agent = ClaudeCodeAgent(config)

        import coder_eval.agents.claude_code_agent as agent_module

        async def mock_query(prompt, options):
            yield assistant_msg
            time.sleep(0.05)  # Simulate 50ms execution
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Timed command")

            assert len(turn.commands) == 1
            cmd = turn.commands[0]

            assert cmd.duration_ms is not None
            # Allow 10ms tolerance for processing overhead
            assert 40 <= cmd.duration_ms <= 70, f"Duration {cmd.duration_ms}ms outside expected range"

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_command_telemetry_orphaned_result_logged(self, tmp_path, caplog):
        """Verify orphaned ResultMessage is logged but doesn't crash."""
        _tool_use_block_cls, _assistant_message_cls, result_message_cls = create_mock_sdk_messages()

        result_msg = result_message_cls("toolu_orphan", False, "orphaned result")

        config = AgentConfig(type="claude-code")
        agent = ClaudeCodeAgent(config)

        import logging

        import coder_eval.agents.claude_code_agent as agent_module

        async def mock_query(prompt, options):
            yield result_msg  # Only result, no tool use!

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))

            with caplog.at_level(logging.WARNING):
                turn = await agent.communicate("Orphaned result")

                assert len(turn.commands) == 0
                assert any("unknown tool_use_id" in record.message for record in caplog.records), (
                    f"Expected 'unknown tool_use_id' warning. Records: {[r.message for r in caplog.records]}"
                )

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_command_telemetry_duplicate_result_logged(self, tmp_path, caplog):
        """Verify multiple results handled gracefully (last wins) and logged."""
        tool_use_block_cls, assistant_message_cls, result_message_cls = create_mock_sdk_messages()

        tool_id = "toolu_dup"
        tool_block = tool_use_block_cls(tool_id, "Bash", {"command": "test"})
        assistant_msg = assistant_message_cls([tool_block])
        result1 = result_message_cls(tool_id, False, "first result")
        result2 = result_message_cls(tool_id, True, "second result (error)")

        config = AgentConfig(type="claude-code")
        agent = ClaudeCodeAgent(config)

        import logging

        import coder_eval.agents.claude_code_agent as agent_module

        async def mock_query(prompt, options):
            yield assistant_msg
            yield result1
            yield result2  # Duplicate!

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))

            with caplog.at_level(logging.DEBUG):
                turn = await agent.communicate("Duplicate results")

                assert len(turn.commands) == 1
                cmd = turn.commands[0]

                assert cmd.result_status == "error"  # From result2
                assert cmd.error_message == "second result (error)"

                # Should log debug message
                assert any("Multiple results" in record.message for record in caplog.records)

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_command_telemetry_unknown_status_logging(self, tmp_path, caplog):
        """Verify unknown status triggers appropriate logging."""
        tool_use_block_cls, assistant_message_cls, _result_message_cls = create_mock_sdk_messages()

        tool_id = "toolu_unknown"
        tool_block = tool_use_block_cls(tool_id, "Read", {"file_path": "test.py"})
        assistant_msg = assistant_message_cls([tool_block])

        config = AgentConfig(type="claude-code")
        agent = ClaudeCodeAgent(config)

        import logging

        import coder_eval.agents.claude_code_agent as agent_module

        async def mock_query(prompt, options):
            yield assistant_msg
            # No result!

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))

            # Capture both INFO and WARNING levels
            with caplog.at_level(logging.INFO):
                turn = await agent.communicate("Missing result")

                assert len(turn.commands) == 1
                assert turn.commands[0].result_status == "unknown"

                # Should have warning about missing result
                warnings = [r for r in caplog.records if r.levelname == "WARNING"]
                assert any("completed without tool result" in r.message for r in warnings)

                # Should have warning summary about unknown statuses with message types
                assert any("'unknown' status" in r.message for r in warnings)

        finally:
            agent_module.query = original_query
