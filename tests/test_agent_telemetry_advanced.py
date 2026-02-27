"""Advanced telemetry tests for ClaudeCodeAgent - orphaned messages, duplicates, and pending commands.

Tests ensure robust telemetry handling under SDK anomalies and interruptions.
"""

import logging

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import AgentConfig


def create_mock_sdk_messages():
    """Create mock SDK message classes matching real SDK structure.

    The SDK delivers tool results inside UserMessage.content as ToolResultBlock objects.
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


@pytest.mark.asyncio
async def test_orphaned_result_message_marks_unknown(tmp_path):
    """Test that commands without ResultMessage get 'unknown' status.

    Hypothesis: SDK interruption leaves commands without results.
    Expected: Command marked as 'unknown', warning logged.

    Context: Lines 187-197 in claude_code_agent.py handle finalization.
    """
    tool_use_block_cls, assistant_message_cls, _result_message_cls = create_mock_sdk_messages()

    tool_use = tool_use_block_cls("tool_123", "Bash", {"command": "echo test"})
    assistant_msg = assistant_message_cls([tool_use])

    config = AgentConfig(type="claude-code")
    agent = ClaudeCodeAgent(config)

    import coder_eval.agents.claude_code_agent as agent_module

    async def mock_query(prompt, options):
        yield assistant_msg
        # No ResultMessage - simulates SDK interruption

    original_query = agent_module.query
    agent_module.query = mock_query

    try:
        await agent.start(str(tmp_path))
        turn_record = await agent.communicate("test prompt")

        # Verify command has 'unknown' status
        assert len(turn_record.commands) == 1
        cmd = turn_record.commands[0]
        assert cmd.tool_name == "Bash"
        assert cmd.result_status == "unknown"
        assert cmd.duration_ms == 0.0  # Conservative estimate for unknown status

    finally:
        agent_module.query = original_query


@pytest.mark.asyncio
async def test_duplicate_result_message_last_wins(tmp_path, caplog):
    """Test that duplicate ResultMessages use last-wins strategy.

    Hypothesis: SDK may send multiple results for same tool_id.
    Expected: Last result overwrites, debug log emitted.

    Context: Lines 162-167 in claude_code_agent.py log duplicates.
    """
    tool_use_block_cls, assistant_message_cls, result_message_cls = create_mock_sdk_messages()

    tool_use = tool_use_block_cls("tool_456", "Read", {"file_path": "test.py"})
    assistant_msg = assistant_message_cls([tool_use])

    # First result: success
    result_1 = result_message_cls("tool_456", False, "File read successfully")

    # Second result: error (should overwrite)
    result_2 = result_message_cls("tool_456", True, "File not found")

    config = AgentConfig(type="claude-code")
    agent = ClaudeCodeAgent(config)

    import coder_eval.agents.claude_code_agent as agent_module

    async def mock_query(prompt, options):
        yield assistant_msg
        yield result_1
        yield result_2

    original_query = agent_module.query
    agent_module.query = mock_query

    try:
        await agent.start(str(tmp_path))

        with caplog.at_level(logging.DEBUG):
            turn_record = await agent.communicate("test prompt")

        # Verify last result wins
        assert len(turn_record.commands) == 1
        cmd = turn_record.commands[0]
        assert cmd.result_status == "error"  # Last result
        assert cmd.error_message == "File not found"

        # Verify debug log for duplicate
        assert any("Multiple results" in record.message for record in caplog.records)

    finally:
        agent_module.query = original_query


@pytest.mark.asyncio
async def test_pending_command_without_result_finalizes_unknown(tmp_path, caplog):
    """Test clean finalization of commands left pending after stream interruption.

    Hypothesis: Stream interruption leaves commands in pending state.
    Expected: Commands finalized with 'unknown' status, warning logged.

    Context: Lines 187-197 handle pending command cleanup.
    """
    tool_use_block_cls, assistant_message_cls, result_message_cls = create_mock_sdk_messages()

    # Create multiple tool uses, only some get results
    tool_use_1 = tool_use_block_cls("tool_001", "Write", {"file_path": "test.py", "content": "code"})
    tool_use_2 = tool_use_block_cls("tool_002", "Bash", {"command": "echo test"})
    tool_use_3 = tool_use_block_cls("tool_003", "Read", {"file_path": "test.py"})

    assistant_msg = assistant_message_cls([tool_use_1, tool_use_2, tool_use_3])

    # Only first tool gets result, others are interrupted
    result_1 = result_message_cls("tool_001", False, "File written successfully")

    config = AgentConfig(type="claude-code")
    agent = ClaudeCodeAgent(config)

    import coder_eval.agents.claude_code_agent as agent_module

    async def mock_query(prompt, options):
        yield assistant_msg
        yield result_1  # Missing results for tool_002 and tool_003

    original_query = agent_module.query
    agent_module.query = mock_query

    try:
        await agent.start(str(tmp_path))

        with caplog.at_level(logging.WARNING):
            turn_record = await agent.communicate("test prompt")

        # Verify all three commands recorded
        assert len(turn_record.commands) == 3

        # First command should have success status
        assert turn_record.commands[0].tool_name == "Write"
        assert turn_record.commands[0].result_status == "success"

        # Second and third commands should have unknown status
        assert turn_record.commands[1].tool_name == "Bash"
        assert turn_record.commands[1].result_status == "unknown"

        assert turn_record.commands[2].tool_name == "Read"
        assert turn_record.commands[2].result_status == "unknown"

        # Verify warning logged for unknown status
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("completed without tool result" in r.message for r in warnings)
        assert any("Status set to 'unknown'" in r.message for r in warnings)

    finally:
        agent_module.query = original_query


@pytest.mark.asyncio
async def test_orphaned_result_without_tool_use_logs_warning(tmp_path, caplog):
    """Test that ResultMessage without ToolUseBlock logs warning.

    Hypothesis: SDK may send results for non-existent tools.
    Expected: Warning logged, no command created.

    Context: Lines 168-173 handle orphaned ResultMessage.
    """
    _tool_use_block_cls, _assistant_message_cls, result_message_cls = create_mock_sdk_messages()

    # Only ResultMessage, no preceding ToolUseBlock
    orphan_result = result_message_cls("nonexistent_tool", False, "Unexpected result")

    config = AgentConfig(type="claude-code")
    agent = ClaudeCodeAgent(config)

    import coder_eval.agents.claude_code_agent as agent_module

    async def mock_query(prompt, options):
        yield orphan_result

    original_query = agent_module.query
    agent_module.query = mock_query

    try:
        await agent.start(str(tmp_path))

        with caplog.at_level(logging.WARNING):
            turn_record = await agent.communicate("test prompt")

        # No commands should be created
        assert len(turn_record.commands) == 0

        # Verify warning logged
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("unknown tool_use_id" in r.message for r in warnings)
        assert any("No matching ToolUseBlock found" in r.message for r in warnings)

    finally:
        agent_module.query = original_query


@pytest.mark.asyncio
async def test_multiple_tools_with_mixed_results(tmp_path):
    """Test handling of multiple commands with various result patterns.

    Hypothesis: Real-world scenarios have mix of success, error, and unknown.
    Expected: Each command tracked correctly with appropriate status.
    """
    tool_use_block_cls, assistant_message_cls, result_message_cls = create_mock_sdk_messages()

    # Create complex scenario
    tool_1 = tool_use_block_cls("tool_a", "Write", {"file_path": "a.py"})
    tool_2 = tool_use_block_cls("tool_b", "Bash", {"command": "ls"})
    tool_3 = tool_use_block_cls("tool_c", "Read", {"file_path": "b.py"})

    assistant_msg = assistant_message_cls([tool_1, tool_2, tool_3])

    result_1 = result_message_cls("tool_a", False, "Success")
    result_2 = result_message_cls("tool_b", True, "Error: command failed")
    # tool_c gets no result

    config = AgentConfig(type="claude-code")
    agent = ClaudeCodeAgent(config)

    import coder_eval.agents.claude_code_agent as agent_module

    async def mock_query(prompt, options):
        yield assistant_msg
        yield result_1
        yield result_2

    original_query = agent_module.query
    agent_module.query = mock_query

    try:
        await agent.start(str(tmp_path))
        turn_record = await agent.communicate("test prompt")

        assert len(turn_record.commands) == 3

        # Verify each command has correct status
        cmd_map = {cmd.tool_name: cmd for cmd in turn_record.commands}

        assert cmd_map["Write"].result_status == "success"
        assert cmd_map["Bash"].result_status == "error"
        assert cmd_map["Bash"].error_message == "Error: command failed"
        assert cmd_map["Read"].result_status == "unknown"

    finally:
        agent_module.query = original_query
