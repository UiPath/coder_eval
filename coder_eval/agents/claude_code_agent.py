"""Claude Code agent implementation using the Claude Agent SDK."""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Message, query

from coder_eval.agent import Agent, AgentState
from coder_eval.models import AgentConfig, CommandTelemetry, FileChange, FileChanges, FileTree, TurnRecord
from coder_eval.resources import get_ignore_patterns, should_ignore_path


logger = logging.getLogger(__name__)


# Type guards for SDK message types (using duck typing for robustness)
def _is_assistant_message(message: Any) -> bool:
    """Check if message is an AssistantMessage using duck typing."""
    return hasattr(message, "content") and hasattr(message, "role")


def _is_tool_use_block(block: Any) -> bool:
    """Check if block is a ToolUseBlock using duck typing."""
    return hasattr(block, "name") and hasattr(block, "id") and hasattr(block, "input")


def _is_result_message(message: Any) -> bool:
    """Check if message is a ResultMessage using duck typing."""
    return hasattr(message, "tool_use_id") and hasattr(message, "is_error")


class ClaudeCodeAgent(Agent):
    """Implementation of the Agent interface for Claude Code using the SDK."""

    def __init__(self, config: AgentConfig):
        """Initialize the Claude Code agent.

        Args:
            config: Agent configuration
        """
        self.config = config
        self.client: ClaudeSDKClient | None = None
        self.working_directory: Path | None = None
        self._state = AgentState.WORKING
        self._iteration = 0

    async def start(self, working_directory: str) -> None:
        """Initialize and start the Claude Code agent.

        Args:
            working_directory: Path to the working directory
        """
        self.working_directory = Path(working_directory)
        self._state = AgentState.WORKING
        # Note: Client is created per-communication to avoid transport issues

    async def communicate(self, user_input: str) -> TurnRecord:
        """Send a message to Claude and receive its response.

        Args:
            user_input: The message/prompt to send

        Returns:
            TurnRecord containing the complete interaction

        Raises:
            RuntimeError: If agent is not started
        """
        if not self.working_directory:
            raise RuntimeError("Agent not started. Call start() first.")

        self._iteration += 1
        turn_start_time = time.monotonic()

        # Capture file state before the turn
        files_before = self._capture_file_tree()

        # Collect all messages from the turn
        messages = []

        # NEW: Two-phase command tracking with precise duration measurement
        # Phase 1: Store pending commands keyed by tool_id with start time
        # Phase 2: Update status and duration when ResultMessage arrives
        pending_commands: dict[str, dict[str, Any]] = {}  # tool_id -> {telemetry, command_start_time}
        processed_results: set[str] = set()  # Track duplicate ResultMessages
        sequence_number = 0

        # Capture stderr for debugging
        stderr_lines = []

        def capture_stderr(line: str) -> None:
            stderr_lines.append(line)

        try:
            # Create options for this query
            options = ClaudeAgentOptions(
                cwd=str(self.working_directory),
                permission_mode=self.config.permission_mode,
                allowed_tools=self.config.allowed_tools or [],
                model=self.config.model,
                stderr=capture_stderr,  # Capture stderr for better error messages
            )

            # Use the query function for one-shot interaction
            async for message in query(prompt=user_input, options=options):
                messages.append(message)

                # NEW: Two-phase command telemetry capture using type guards

                # PHASE 1: Capture ToolUseBlock and create pending command
                if _is_assistant_message(message):
                    content = getattr(message, "content", None)
                    # Content can be a list of blocks (text, tool_use, etc.)
                    if content and isinstance(content, list):
                        for block in content:
                            if _is_tool_use_block(block):
                                command_start_time = time.monotonic()  # Precise command start time

                                telemetry = CommandTelemetry(
                                    tool_name=block.name,
                                    tool_id=block.id,
                                    timestamp=datetime.now(),
                                    parameters=block.input,
                                    sequence_number=sequence_number,
                                    result_status=None,  # Pending result
                                    duration_ms=None,  # Not complete yet
                                )

                                # Store command with start time for duration calculation
                                pending_commands[block.id] = {
                                    "telemetry": telemetry,
                                    "command_start_time": command_start_time,
                                }
                                sequence_number += 1

                # PHASE 2: Process ResultMessage and update command status/duration
                elif _is_result_message(message):
                    tool_use_id = getattr(message, "tool_use_id", None)
                    is_error = getattr(message, "is_error", False)
                    content = getattr(message, "content", "")

                    if tool_use_id and tool_use_id in pending_commands:
                        cmd_data = pending_commands[tool_use_id]
                        cmd = cmd_data["telemetry"]
                        command_start_time = cmd_data["command_start_time"]

                        # Calculate precise duration
                        command_end_time = time.monotonic()
                        duration_ms = (command_end_time - command_start_time) * 1000

                        # Update command with actual results
                        cmd.result_status = "error" if is_error else "success"
                        cmd.duration_ms = duration_ms
                        cmd.result_summary = content[:200] if content else None  # Truncate long results

                        if is_error:
                            cmd.error_message = content

                        # Log duplicate results for debugging
                        if tool_use_id in processed_results:
                            logger.debug(
                                f"Multiple ResultMessages for tool_id={tool_use_id}. Last result wins strategy applied."
                            )
                        processed_results.add(tool_use_id)
                    else:
                        # Orphaned result - log warning
                        logger.warning(
                            f"ResultMessage received for unknown tool_use_id={tool_use_id}. "
                            + "No matching ToolUseBlock found. This may indicate an SDK issue."
                        )

        except Exception as e:
            self._state = AgentState.ERROR
            # Include stderr in error message for debugging
            error_details = str(e)
            if stderr_lines:
                error_details += "\nStderr output:\n" + "\n".join(stderr_lines[-20:])  # Last 20 lines
            raise RuntimeError(f"Communication with agent failed: {error_details}") from e

        # PHASE 3: Finalize commands - convert pending to list and handle missing results
        commands: list[CommandTelemetry] = []
        unknown_status_count = 0

        for tool_id, cmd_data in pending_commands.items():
            cmd = cmd_data["telemetry"]

            # Mark commands without results as "unknown"
            if cmd.result_status is None:
                cmd.result_status = "unknown"
                unknown_status_count += 1
                logger.warning(
                    f"Command {cmd.tool_name}:{tool_id} completed without ResultMessage. "
                    + "Status set to 'unknown'. This may indicate agent interruption or SDK issue."
                )

            # Estimate duration for commands without results
            if cmd.duration_ms is None:
                cmd.duration_ms = 0.0  # Conservative estimate

            commands.append(cmd)

        # Log summary of unknown statuses
        if unknown_status_count > 0:
            logger.info(
                f"Turn completed with {unknown_status_count} command(s) in 'unknown' status. "
                + "This may indicate agent/SDK communication issues."
            )

        # Commands are in sequence order as they were captured

        # Capture file state after the turn
        files_after = self._capture_file_tree()

        # Detect file changes
        file_changes = self._detect_file_changes(files_before, files_after)

        # Format agent output from messages
        agent_output = self._format_messages(messages)

        # Determine agent state from messages
        self._update_state_from_messages(messages)

        duration = time.monotonic() - turn_start_time

        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output=agent_output,
            commands=commands,  # NEW: Structured telemetry
            files_changed=file_changes,
            timestamp=datetime.now(),
            duration_seconds=duration,
        )

    async def stop(self) -> None:
        """Stop the agent and clean up resources."""
        # Clean up any resources
        # Note: Client is created per-communication using async context manager
        self.client = None
        self._state = AgentState.FINISHED

    def get_state(self) -> AgentState:
        """Get the current state of the agent.

        Returns:
            Current agent state
        """
        return self._state

    def _capture_file_tree(self) -> FileTree:
        """Capture the current state of files in the working directory.

        Returns:
            Dictionary mapping file paths to modification times
        """
        if not self.working_directory:
            return {}

        file_tree = {}
        for path in self.working_directory.rglob("*"):
            if path.is_file() and not self._should_ignore_path(path):
                try:
                    rel_path = path.relative_to(self.working_directory)
                    file_tree[str(rel_path)] = path.stat().st_mtime
                except (OSError, ValueError):
                    # Skip files that can't be accessed
                    continue

        return file_tree

    def _should_ignore_path(self, path: Path) -> bool:
        """Check if a path should be ignored in file tracking.

        Args:
            path: Path to check

        Returns:
            True if path should be ignored
        """
        patterns = get_ignore_patterns(self.config.additional_ignore_patterns)
        return should_ignore_path(path, patterns)

    def _detect_file_changes(
        self,
        before: FileTree,
        after: FileTree,
    ) -> FileChanges:
        """Detect changes between two file trees.

        Args:
            before: File tree before the operation
            after: File tree after the operation

        Returns:
            List of file changes
        """
        changes = []

        # Find created and modified files
        for path, mtime in after.items():
            if path not in before:
                changes.append(FileChange(path=path, operation="created"))
            elif before[path] != mtime:
                changes.append(FileChange(path=path, operation="modified"))

        # Find deleted files
        for path in before:
            if path not in after:
                changes.append(FileChange(path=path, operation="deleted"))

        return changes

    def _format_messages(self, messages: list[Message]) -> str:
        """Format agent messages into a readable string.

        Args:
            messages: List of messages from the agent (SDK message objects)

        Returns:
            Formatted string representation
        """
        formatted_parts = []

        for msg in messages:
            # Handle SDK message objects (they have a type attribute, not a dict)
            msg_type_name = type(msg).__name__

            if msg_type_name == "SystemMessage":
                # System messages (less interesting for output)
                continue

            elif msg_type_name == "UserMessage":
                # User messages (we already know what we sent)
                continue

            elif msg_type_name == "AssistantMessage":
                # Assistant text responses
                content = getattr(msg, "content", "")
                if content:
                    formatted_parts.append(f"[ASSISTANT] {content}")

            elif msg_type_name == "ResultMessage":
                # Tool results
                content = getattr(msg, "content", "")
                is_error = getattr(msg, "is_error", False)
                status = "ERROR" if is_error else "SUCCESS"
                formatted_parts.append(f"[RESULT - {status}] {content}")

            elif msg_type_name == "StreamEvent":
                # Stream events (tool use, thinking, etc.)
                event_type = getattr(msg, "type", "unknown")
                if event_type == "tool_use":
                    tool_name = getattr(msg, "name", "unknown")
                    formatted_parts.append(f"[TOOL USE] {tool_name}")
                # Skip other stream events like thinking

            else:
                # Unknown message type - include for debugging
                formatted_parts.append(f"[{msg_type_name}] {str(msg)[:100]}")

        return "\n".join(formatted_parts) if formatted_parts else "[No output]"

    def _update_state_from_messages(self, messages: list[Message]) -> None:
        """Update agent state based on received messages.

        Args:
            messages: List of messages from the agent (SDK objects)
        """
        # Check for error indicators
        for msg in messages:
            msg_type_name = type(msg).__name__

            if msg_type_name == "ResultMessage":
                is_error = getattr(msg, "is_error", False)
                if is_error:
                    # Don't change state on tool errors - agent might recover
                    pass

            # Check for explicit error messages (use getattr for safe access)
            if getattr(msg, "error", None):
                self._state = AgentState.ERROR
                return

        # If no errors, agent is working normally
        self._state = AgentState.WORKING
