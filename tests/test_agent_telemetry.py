"""Tests for command telemetry status tracking (V2 fix)."""

import time

import pytest

from coder_eval.models import AgentKind, AssistantMessage, parse_agent_config


def create_mock_sdk_messages():
    """Create mock SDK message classes matching real SDK structure.

    The SDK delivers tool results inside UserMessage.content as ToolResultBlock objects.
    - AssistantMessage: contains ToolUseBlock in content (duck-typed via 'content' + 'model')
    - UserMessage: contains ToolResultBlock in content (duck-typed via 'content' + 'tool_use_result')
    - ToolUseBlock: has 'id', 'name', 'input'
    - ToolResultBlock: has 'tool_use_id', 'is_error', 'content'
    - TextBlock: has 'text' attribute
    - ThinkingBlock: has 'thinking' and optional 'signature' attributes
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

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ThinkingBlock:
        def __init__(self, thinking, signature=None):
            self.thinking = thinking
            self.signature = signature

    class AssistantMessage:
        def __init__(self, content, usage=None):
            self.content = content
            self.model = "mock-model"
            self.usage = usage or {}
            self.stop_reason = "end_turn"

    class UserMessage:
        """Wraps ToolResultBlock(s) as the SDK does."""

        def __init__(self, tool_use_id, is_error, content):
            block = ToolResultBlock(tool_use_id, is_error, content)
            self.content = [block]
            self.tool_use_result = {"tool_use_id": tool_use_id, "is_error": is_error, "content": content}

    class ResultMessage:
        """SDK's final ResultMessage with usage/session info."""

        def __init__(self, session_id="mock-session", usage=None):
            self.session_id = session_id
            self.usage = usage or {}
            self.num_turns = 1
            self.total_cost_usd = None

    return ToolUseBlock, AssistantMessage, UserMessage, TextBlock, ThinkingBlock, ResultMessage


class TestCommandTelemetryStatus:
    """Tests for accurate command status tracking."""

    @pytest.mark.asyncio
    async def test_command_telemetry_captures_success(self, tmp_path):
        """Verify successful command gets result_status='success' and duration."""
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        # Simulate: ToolUseBlock(Bash) + UserMessage(tool result) + ResultMessage(success)
        tool_id = "toolu_test123"
        tool_block = tool_use_block_cls(tool_id, "Bash", {"command": "ls"})
        assistant_msg = assistant_message_cls([tool_block])
        user_msg = user_message_cls(tool_id, False, "file1.py\nfile2.py")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            time.sleep(0.01)  # 10ms delay
            yield user_msg
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
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        tool_id = "toolu_error456"
        tool_block = tool_use_block_cls(tool_id, "Read", {"file_path": "missing.txt"})
        assistant_msg = assistant_message_cls([tool_block])
        user_msg = user_message_cls(tool_id, True, "File not found: missing.txt")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            time.sleep(0.005)
            yield user_msg
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
        tool_use_block_cls, assistant_message_cls, _user_message_cls, _, _, _result_message_cls = (
            create_mock_sdk_messages()
        )

        tool_id = "toolu_orphan789"
        tool_block = tool_use_block_cls(tool_id, "Write", {"file_path": "test.py", "content": "code"})
        assistant_msg = assistant_message_cls([tool_block])

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

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
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        tool1 = tool_use_block_cls("tool1", "Read", {"file_path": "exists.py"})
        tool2 = tool_use_block_cls("tool2", "Read", {"file_path": "missing.py"})
        tool3 = tool_use_block_cls("tool3", "Bash", {"command": "ls"})

        assistant_msg = assistant_message_cls([tool1, tool2, tool3])
        user_msg1 = user_message_cls("tool1", False, "def foo(): pass")
        user_msg2 = user_message_cls("tool2", True, "File not found")
        user_msg3 = user_message_cls("tool3", False, "file.py")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            yield user_msg1
            yield user_msg2
            yield user_msg3
            yield result_msg

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
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        tool_id = "toolu_timing"
        tool_block = tool_use_block_cls(tool_id, "Bash", {"command": "sleep 0.05"})
        assistant_msg = assistant_message_cls([tool_block])
        user_msg = user_message_cls(tool_id, False, "")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            time.sleep(0.05)  # Simulate 50ms execution
            yield user_msg
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
        _tool_use_block_cls, _assistant_message_cls, _user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        result_msg = result_message_cls()

        import logging

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield result_msg  # Only result, no tool use!

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))

            with caplog.at_level(logging.WARNING):
                turn = await agent.communicate("Orphaned result")

                assert len(turn.commands) == 0
                assert any("Unhandled SDK message type" in record.message for record in caplog.records), (
                    f"Expected 'Unhandled SDK message type' warning. Records: {[r.message for r in caplog.records]}"
                )

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_command_telemetry_duplicate_result_logged(self, tmp_path, caplog):
        """Verify multiple results handled gracefully (last wins) and logged."""
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        tool_id = "toolu_dup"
        tool_block = tool_use_block_cls(tool_id, "Bash", {"command": "test"})
        assistant_msg = assistant_message_cls([tool_block])
        user_msg1 = user_message_cls(tool_id, False, "first result")
        user_msg2 = user_message_cls(tool_id, True, "second result (error)")
        result_msg = result_message_cls()

        import logging

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            yield user_msg1
            yield user_msg2  # Duplicate!
            yield result_msg

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
        tool_use_block_cls, assistant_message_cls, _user_message_cls, _, _, _result_message_cls = (
            create_mock_sdk_messages()
        )

        tool_id = "toolu_unknown"
        tool_block = tool_use_block_cls(tool_id, "Read", {"file_path": "test.py"})
        assistant_msg = assistant_message_cls([tool_block])

        import logging

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

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

    @pytest.mark.asyncio
    async def test_command_telemetry_non_dict_input(self, tmp_path):
        """Verify non-dict tool input doesn't crash telemetry capture.

        The SDK types block.input as object. If a non-dict value arrives,
        CommandTelemetry (which expects dict[str, Any]) should handle it
        gracefully by wrapping it rather than raising a validation error.
        """
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        tool_id = "toolu_nondict"
        # Simulate a tool with non-dict input (e.g., a string or list)
        tool_block = tool_use_block_cls(tool_id, "CustomTool", "just a string input")
        assistant_msg = assistant_message_cls([tool_block])
        user_msg = user_message_cls(tool_id, False, "result")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            yield user_msg
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Non-dict input")

            # Should capture the command without crashing
            assert len(turn.commands) == 1
            cmd = turn.commands[0]
            assert cmd.tool_name == "CustomTool"
            assert cmd.parameters == {"raw": "just a string input"}
        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_stream_event_non_dict_input_preserved(self, tmp_path):
        """Verify non-dict tool input is also preserved in ToolCallEvent (not discarded).

        Both CommandTelemetry and ToolCallEvent should wrap non-dict input consistently.
        """
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        tool_id = "toolu_stream_nondict"
        tool_block = tool_use_block_cls(tool_id, "CustomTool", ["a", "list", "input"])
        assistant_msg = assistant_message_cls([tool_block])
        user_msg = user_message_cls(tool_id, False, "ok")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        collected_events = []

        class CollectingCallback:
            def on_event(self, event):
                collected_events.append(event)

        async def mock_query(prompt, options):
            yield assistant_msg
            yield user_msg
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            await agent.communicate("Non-dict stream", stream_callback=CollectingCallback())

            from coder_eval.streaming.events import ToolCallEvent

            tool_call_events = [e for e in collected_events if isinstance(e, ToolCallEvent)]
            assert len(tool_call_events) == 1
            assert tool_call_events[0].parameters == {"raw": ["a", "list", "input"]}
        finally:
            agent_module.query = original_query


class TestAssistantMessageTelemetry:
    """Tests for assistant turn telemetry capture (generation timing + content blocks)."""

    @pytest.mark.asyncio
    async def test_assistant_turn_capture_basic(self, tmp_path):
        """Verify assistant turns are captured with basic telemetry."""
        tool_use_block_cls, assistant_message_cls, user_message_cls, text_block_cls, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        # Create a message with text and a tool call
        text_block = text_block_cls("I'll read the file for you.")
        tool_block = tool_use_block_cls("toolu_turn_test", "Read", {"file_path": "test.py"})
        assistant_msg = assistant_message_cls([text_block, tool_block])
        user_msg = user_message_cls("toolu_turn_test", False, "def foo(): pass")
        result_msg = result_message_cls(usage={"input_tokens": 100, "output_tokens": 50})

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            yield user_msg
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Read a file")

            # Verify assistant_turns list is populated
            assert len(turn.messages) == 1
            aturn = turn.messages[0]
            assert isinstance(aturn, AssistantMessage)

            # Verify basic fields
            assert aturn.role == "assistant"
            assert aturn.generation_duration_ms >= 0
            assert aturn.input_tokens == 100
            assert aturn.output_tokens == 50
            assert aturn.stop_reason == "end_turn"
            assert aturn.model == "mock-model"

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_content_blocks_ordering(self, tmp_path):
        """Verify content blocks are ordered: text before tool_use."""
        tool_use_block_cls, assistant_message_cls, user_message_cls, text_block_cls, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        text_block = text_block_cls("I'm reading the file...")
        tool_block = tool_use_block_cls("toolu_content", "Read", {"file_path": "file.txt"})
        assistant_msg = assistant_message_cls([text_block, tool_block])
        user_msg = user_message_cls("toolu_content", False, "content")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            yield user_msg
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Test ordering")

            assert len(turn.messages) == 1
            aturn = turn.messages[0]
            assert isinstance(aturn, AssistantMessage)

            # Verify content blocks are in order
            assert len(aturn.content_blocks) == 2
            assert aturn.content_blocks[0].block_type == "text"
            assert aturn.content_blocks[0].text == "I'm reading the file..."
            assert aturn.content_blocks[0].sequence == 0

            assert aturn.content_blocks[1].block_type == "tool_use"
            assert aturn.content_blocks[1].tool_use_id == "toolu_content"
            assert aturn.content_blocks[1].sequence == 1

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_thinking_block_capture(self, tmp_path):
        """Verify extended-thinking blocks are captured with thinking content."""
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, thinking_block_cls, result_message_cls = (
            create_mock_sdk_messages()
        )

        thinking_block = thinking_block_cls("Let me think about this...", signature="sig_123")
        tool_block = tool_use_block_cls("toolu_think", "Read", {"file_path": "file.py"})
        assistant_msg = assistant_message_cls([thinking_block, tool_block])
        user_msg = user_message_cls("toolu_think", False, "result")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            yield user_msg
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Test thinking")

            assert len(turn.messages) == 1
            aturn = turn.messages[0]
            assert isinstance(aturn, AssistantMessage)

            # Verify thinking block
            assert len(aturn.content_blocks) == 2
            assert aturn.content_blocks[0].block_type == "thinking"
            assert aturn.content_blocks[0].thinking == "Let me think about this..."
            assert aturn.content_blocks[0].signature == "sig_123"

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_multiple_assistant_turns(self, tmp_path):
        """Verify multiple assistant turns are tracked separately."""
        tool_use_block_cls, assistant_message_cls, user_message_cls, text_block_cls, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        # First turn
        text1 = text_block_cls("First response")
        tool1 = tool_use_block_cls("toolu_turn1", "Read", {"file_path": "file1.txt"})
        msg1 = assistant_message_cls([text1, tool1])
        user_msg1 = user_message_cls("toolu_turn1", False, "content1")
        result1 = result_message_cls(usage={"input_tokens": 50, "output_tokens": 30})

        # Second turn
        text2 = text_block_cls("Second response")
        tool2 = tool_use_block_cls("toolu_turn2", "Write", {"file_path": "file2.txt", "content": "new content"})
        msg2 = assistant_message_cls([text2, tool2])
        user_msg2 = user_message_cls("toolu_turn2", False, "ok")
        result2 = result_message_cls(usage={"input_tokens": 60, "output_tokens": 40})

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield msg1
            yield user_msg1
            yield result1
            yield msg2
            yield user_msg2
            yield result2

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Multiple turns")

            # Verify both turns captured
            assert len(turn.messages) == 2

            # First turn
            aturn1 = turn.messages[0]
            assert isinstance(aturn1, AssistantMessage)
            assert aturn1.role == "assistant"
            assert aturn1.input_tokens == 50
            assert aturn1.output_tokens == 30
            assert len(aturn1.content_blocks) == 2

            # Second turn
            aturn2 = turn.messages[1]
            assert isinstance(aturn2, AssistantMessage)
            assert aturn2.role == "assistant"
            assert aturn2.input_tokens == 60
            assert aturn2.output_tokens == 40
            assert len(aturn2.content_blocks) == 2

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_execution_timing_fields(self, tmp_path):
        """Verify execution timing fields are populated correctly."""
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        tool_block = tool_use_block_cls("toolu_timing", "Read", {"file_path": "file.txt"})
        assistant_msg = assistant_message_cls([tool_block])
        user_msg = user_message_cls("toolu_timing", False, "content")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield assistant_msg
            time.sleep(0.01)  # 10ms execution delay
            yield user_msg
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Test execution timing")

            assert len(turn.commands) == 1
            cmd = turn.commands[0]

            # Verify execution timing fields are set
            assert cmd.generation_completed_at is not None
            assert cmd.execution_started_at is not None
            assert cmd.execution_completed_at is not None

            # Verify time ordering
            assert cmd.execution_started_at <= cmd.execution_completed_at
            assert (
                cmd.generation_completed_at <= cmd.execution_started_at
                or cmd.generation_completed_at == cmd.execution_started_at
            )

        finally:
            agent_module.query = original_query

    @pytest.mark.asyncio
    async def test_assistant_turn_index_on_commands(self, tmp_path):
        """Verify commands capture which assistant turn they came from."""
        tool_use_block_cls, assistant_message_cls, user_message_cls, _, _, result_message_cls = (
            create_mock_sdk_messages()
        )

        tool1 = tool_use_block_cls("toolu_cmd1", "Read", {"file_path": "file1.txt"})
        tool2 = tool_use_block_cls("toolu_cmd2", "Write", {"file_path": "file2.txt", "content": "data"})
        msg1 = assistant_message_cls([tool1, tool2])
        user_msg1 = user_message_cls("toolu_cmd1", False, "content1")
        user_msg2 = user_message_cls("toolu_cmd2", False, "ok")
        result_msg = result_message_cls()

        import coder_eval.agents.claude_code_agent as agent_module

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        agent = agent_module.ClaudeCodeAgent(config)

        async def mock_query(prompt, options):
            yield msg1
            yield user_msg1
            yield user_msg2
            yield result_msg

        original_query = agent_module.query
        agent_module.query = mock_query

        try:
            await agent.start(str(tmp_path))
            turn = await agent.communicate("Test command indexing")

            # Both commands should reference assistant turn index 0
            assert len(turn.commands) == 2
            assert turn.commands[0].assistant_turn_index == 0
            assert turn.commands[1].assistant_turn_index == 0

        finally:
            agent_module.query = original_query
