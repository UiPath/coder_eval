"""Claude Code agent implementation using the Claude Agent SDK."""

import dataclasses
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Message, ProcessError, query

from coder_eval.agent import Agent, AgentState
from coder_eval.models import AgentConfig, CommandTelemetry, FileChange, FileChanges, FileTree, TokenUsage, TurnRecord
from coder_eval.resources import get_ignore_patterns, should_ignore_path
from coder_eval.streaming.callbacks import StreamCallback, safe_emit
from coder_eval.streaming.events import TextChunkEvent, ToolCallEvent, ToolResultEvent


logger = logging.getLogger(__name__)


# Type guards for SDK message types (using duck typing for robustness)
def _is_assistant_message(message: Any) -> bool:
    """Check if message is an AssistantMessage using duck typing."""
    return hasattr(message, "content") and hasattr(message, "model")


def _is_tool_use_block(block: Any) -> bool:
    """Check if block is a ToolUseBlock using duck typing."""
    return hasattr(block, "name") and hasattr(block, "id") and hasattr(block, "input")


def _is_user_message(message: Any) -> bool:
    """Check if message is a UserMessage (which may contain tool results) using duck typing."""
    return hasattr(message, "content") and hasattr(message, "tool_use_result")


def _is_tool_result_block(block: Any) -> bool:
    """Check if block is a ToolResultBlock using duck typing."""
    return hasattr(block, "tool_use_id") and hasattr(block, "is_error")


def _is_sdk_result_message(message: Any) -> bool:
    """Check if message is the SDK's final ResultMessage (with usage/cost data).

    Distinct from ToolResultBlock which has tool_use_id.
    """
    return hasattr(message, "session_id") and hasattr(message, "usage")


_SKIP = object()  # Sentinel for values that should be excluded from the dump


def _serialize_value(value: Any) -> Any:
    """Recursively serialize a value to JSON-safe types.

    Returns _SKIP sentinel for non-serializable values (callables, file-like objects).
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if callable(value):
        return _SKIP
    if hasattr(value, "write") and hasattr(value, "read"):
        return _SKIP
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        serialized: dict[str, Any] = {}
        for field in dataclasses.fields(value):
            field_value = _serialize_value(getattr(value, field.name))
            if field_value is not _SKIP:
                serialized[field.name] = field_value
        return serialized
    if isinstance(value, dict):
        serialized_dict: dict[str, Any] = {}
        for k, v in value.items():
            v_serialized = _serialize_value(v)
            if v_serialized is not _SKIP:
                serialized_dict[str(k)] = v_serialized
        return serialized_dict
    if isinstance(value, (list, tuple)):
        serialized_list: list[Any] = []
        for item in value:
            item_serialized = _serialize_value(item)
            if item_serialized is not _SKIP:
                serialized_list.append(item_serialized)
        return serialized_list
    # Fallback: convert unknown types to string representation
    return str(value)


def _dump_sdk_options(opts: ClaudeAgentOptions) -> dict[str, Any]:
    """Dump ClaudeAgentOptions to a plain dict, skipping non-serializable values.

    Recursively traverses dataclass fields and nested structures (dicts, lists,
    dataclasses). Skips callables and file-like objects. Converts Path to str.

    Args:
        opts: ClaudeAgentOptions dataclass instance

    Returns:
        Dictionary of field names to JSON-serializable values
    """
    result: dict[str, Any] = {}
    for field in dataclasses.fields(opts):
        value = _serialize_value(getattr(opts, field.name))
        if value is not _SKIP:
            result[field.name] = value
    return result


class ClaudeCodeAgent(Agent):
    """Implementation of the Agent interface for Claude Code using the SDK."""

    def __init__(self, config: AgentConfig, proxy_port: int | None = None):
        """Initialize the Claude Code agent.

        Args:
            config: Agent configuration
            proxy_port: If set, route API traffic through the local LLM Gateway proxy on this port
        """
        self.config = config
        self.proxy_port = proxy_port
        self.client: ClaudeSDKClient | None = None
        self.working_directory: Path | None = None
        self._state = AgentState.WORKING
        self._iteration = 0
        self._sdk_options_dump: dict[str, Any] | None = None

    async def start(self, working_directory: str) -> None:
        """Initialize and start the Claude Code agent.

        Args:
            working_directory: Path to the working directory
        """
        self.working_directory = Path(working_directory)
        self._state = AgentState.WORKING
        # Note: Client is created per-communication to avoid transport issues

    async def communicate(self, user_input: str, *, stream_callback: StreamCallback | None = None) -> TurnRecord:
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

        # SDK ResultMessage token usage (captured from final message)
        sdk_result_usage: dict[str, Any] | None = None
        sdk_result_cost: float | None = None

        # Model identifier from AssistantMessage (last one wins)
        sdk_model_used: str | None = None

        # Count of AssistantMessage objects in this turn
        assistant_turn_count = 0

        # Capture stderr for debugging
        stderr_lines = []

        def capture_stderr(line: str) -> None:
            stderr_lines.append(line)

        try:
            # Process plugins: copy from config and replace env vars in paths
            plugins = self._process_plugins(self.config.plugins or [])  # type: ignore[arg-type]

            # Build env overrides for proxy routing
            env: dict[str, str] = {}
            if self.proxy_port is not None:
                env = {
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.proxy_port}",
                    "ANTHROPIC_API_KEY": "llmgw-proxy",  # Dummy key, required by CLI
                }

            options = ClaudeAgentOptions(
                cwd=str(self.working_directory),
                permission_mode=self.config.permission_mode,
                allowed_tools=self.config.allowed_tools or [],
                model=self.config.model,
                max_turns=self.config.max_turns,
                plugins=plugins,  # type: ignore[arg-type]
                stderr=capture_stderr,  # Capture stderr for better error messages
                env=env,
                setting_sources=["project"],  # Load CLAUDE.md and .claude/ settings from cwd
            )

            # Dump SDK options for later inspection (captures all 37+ fields including defaults)
            self._sdk_options_dump = _dump_sdk_options(options)

            # Use the query function for one-shot interaction
            logger.debug("Starting agent query stream...")
            async for message in query(prompt=user_input, options=options):
                messages.append(message)
                msg_type = type(message).__name__

                # Stream debug logging for real-time visibility
                self._log_message_debug(message, msg_type)

                # Two-phase command telemetry capture using type guards

                # PHASE 1: Capture ToolUseBlock and create pending command
                if _is_assistant_message(message):
                    assistant_turn_count += 1
                    model_attr = getattr(message, "model", None)
                    if isinstance(model_attr, str):
                        sdk_model_used = model_attr
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
                                    parameters=block.input if isinstance(block.input, dict) else {"raw": block.input},
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

                                safe_emit(
                                    stream_callback,
                                    ToolCallEvent(
                                        task_id=self.config.type.value,
                                        tool_name=block.name,
                                        tool_id=block.id,
                                        parameters=block.input
                                        if isinstance(block.input, dict)
                                        else {"raw": block.input},
                                        sequence_number=sequence_number - 1,
                                    ),
                                )
                            elif hasattr(block, "text"):
                                safe_emit(
                                    stream_callback,
                                    TextChunkEvent(
                                        task_id=self.config.type.value,
                                        text=str(block.text),
                                    ),
                                )

                # Capture SDK ResultMessage with token usage (check BEFORE tool results
                # to avoid misclassification if SDK message also has tool_use_id/is_error)
                elif _is_sdk_result_message(message):
                    sdk_result_usage = getattr(message, "usage", None)
                    sdk_result_cost = getattr(message, "total_cost_usd", None)

                # PHASE 2: Process tool results from UserMessage content blocks.
                # The SDK delivers tool results as UserMessage objects containing
                # ToolResultBlock in their content list (not as standalone messages).
                elif _is_user_message(message):
                    content = getattr(message, "content", None)
                    if content and isinstance(content, list):
                        for block in content:
                            if _is_tool_result_block(block):
                                # Extract tool_name before resolve (defensive: resolve could remove entries)
                                tool_name = ""
                                if block.tool_use_id in pending_commands:
                                    tool_name = pending_commands[block.tool_use_id]["telemetry"].tool_name
                                self._resolve_pending_command(
                                    block.tool_use_id,
                                    getattr(block, "is_error", False) or False,
                                    block.content,
                                    pending_commands,
                                    processed_results,
                                )
                                is_error_flag = getattr(block, "is_error", False) or False
                                result_content = str(block.content) if block.content is not None else ""
                                safe_emit(
                                    stream_callback,
                                    ToolResultEvent(
                                        task_id=self.config.type.value,
                                        tool_id=block.tool_use_id,
                                        tool_name=tool_name,
                                        success=not is_error_flag,
                                        result_preview=result_content[:200],
                                    ),
                                )

            logger.debug("Agent query stream ended")

        except ProcessError as e:
            self._state = AgentState.ERROR
            stderr = self._build_stderr_message(e.stderr, stderr_lines)
            error_info = self._extract_error_from_messages(messages)
            detail = error_info or stderr
            raise RuntimeError(f"CLI process failed (exit code {e.exit_code}): {detail}") from e
        except Exception as e:
            self._state = AgentState.ERROR
            # The SDK wraps ProcessError as a generic Exception via the message stream.
            # Extract useful info from collected messages and stderr.
            error_info = self._extract_error_from_messages(messages)
            cause_stderr = self._extract_cause_stderr(e)
            stderr = self._build_stderr_message(cause_stderr, stderr_lines)
            error_details = self._clean_error_message(str(e))
            if error_info:
                error_details += f"\nDetails: {error_info}"
            elif stderr:
                error_details += f"\nStderr output:\n{stderr}"
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
                    f"Command {cmd.tool_name}:{tool_id} completed without tool result. "
                    + "Status set to 'unknown'. This may indicate agent interruption or SDK issue."
                )

            # Estimate duration for commands without results
            if cmd.duration_ms is None:
                cmd.duration_ms = 0.0  # Conservative estimate

            commands.append(cmd)

        # Log summary of unknown statuses with message type breakdown for debugging
        if unknown_status_count > 0:
            msg_type_counts: dict[str, int] = {}
            for msg in messages:
                type_name = type(msg).__name__
                msg_type_counts[type_name] = msg_type_counts.get(type_name, 0) + 1
            type_summary = ", ".join(f"{k}={v}" for k, v in sorted(msg_type_counts.items()))
            logger.warning(
                f"Turn completed with {unknown_status_count} command(s) in 'unknown' status. "
                + f"Messages received: [{type_summary}]. "
                + "This may indicate an SDK message type mismatch or agent interruption."
            )

        # Commands are in sequence order as they were captured

        # Build TokenUsage from SDK ResultMessage data
        token_usage: TokenUsage | None = None
        if sdk_result_usage:
            token_usage = TokenUsage(
                input_tokens=sdk_result_usage.get("input_tokens", 0),
                output_tokens=sdk_result_usage.get("output_tokens", 0),
                cache_creation_input_tokens=sdk_result_usage.get("cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=sdk_result_usage.get("cache_read_input_tokens", 0) or 0,
                total_cost_usd=sdk_result_cost,
            )

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
            token_usage=token_usage,
            model_used=sdk_model_used,
            assistant_turn_count=assistant_turn_count,
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

    def get_sdk_options(self) -> dict[str, Any] | None:
        """Get the raw SDK options used for the last agent query.

        Returns:
            Dictionary of SDK option field names to values, or None if communicate() hasn't been called.
        """
        return self._sdk_options_dump

    @staticmethod
    def _process_plugins(plugins: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Process plugins by expanding environment variable placeholders in paths.

        Expands any $VAR or ${VAR} patterns in plugin paths using environment variables.
        Logs a warning if a path contains an env var reference that is not set.

        Args:
            plugins: List of plugin configuration dictionaries (with optional 'path' keys)

        Returns:
            List of processed plugin configurations with env vars expanded
        """
        if not plugins:
            return []

        processed = []

        for plugin in plugins:
            # Create a copy to avoid modifying the original
            processed_plugin = dict(plugin)

            # Expand env vars in path if present
            if "path" in processed_plugin:
                path = processed_plugin["path"]
                # Check for unset env vars before expansion (for better error messages)
                # Matches $VAR or ${VAR}
                var_pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
                for match in var_pattern.finditer(path):
                    # group(1) is ${VAR}, group(2) is $VAR
                    var_name = match.group(1) or match.group(2)
                    if var_name not in os.environ:
                        logger.warning(f"Plugin path contains undefined environment variable ${var_name}: {path}")

                # Expand all env vars in the path
                processed_plugin["path"] = os.path.expandvars(path)

            processed.append(processed_plugin)

        return processed

    @staticmethod
    def _log_message_debug(message: Any, msg_type: str) -> None:
        """Log agent message details at DEBUG level for real-time streaming visibility.

        Args:
            message: SDK message object
            msg_type: Type name of the message
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return

        if msg_type == "AssistantMessage":
            content = getattr(message, "content", None)
            if content and isinstance(content, list):
                for block in content:
                    if _is_tool_use_block(block):
                        params_str = str(block.input)[:300]
                        logger.debug(f">>> TOOL CALL: {block.name} | id={block.id} | params={params_str}")
                    elif hasattr(block, "text"):
                        text = str(block.text)[:500]
                        logger.debug(f">>> ASSISTANT: {text}")
                    else:
                        block_type = type(block).__name__
                        logger.debug(f">>> ASSISTANT BLOCK ({block_type}): {str(block)[:200]}")
            elif content and isinstance(content, str):
                logger.debug(f">>> ASSISTANT: {content[:500]}")

        elif msg_type == "UserMessage":
            content = getattr(message, "content", None)
            if content and isinstance(content, list):
                for block in content:
                    if _is_tool_result_block(block):
                        is_error = getattr(block, "is_error", False) or False
                        status = "ERROR" if is_error else "OK"
                        result_preview = str(block.content)[:300] if block.content is not None else "(empty)"
                        logger.debug(f"<<< TOOL RESULT [{status}]: id={block.tool_use_id} | {result_preview}")

        elif msg_type == "ResultMessage":
            usage = getattr(message, "usage", None)
            cost = getattr(message, "total_cost_usd", None)
            is_error = getattr(message, "is_error", False)
            result = getattr(message, "result", None)
            if is_error:
                logger.debug(f"<<< RESULT [ERROR]: {str(result)[:300]}")
            else:
                usage_str = str(usage)[:200] if usage else "n/a"
                cost_str = f"${cost}" if cost is not None else "n/a"
                logger.debug(f"<<< RESULT: cost={cost_str}, usage={usage_str}")

        elif msg_type == "SystemMessage":
            subtype = getattr(message, "subtype", None)
            data = getattr(message, "data", None)
            logger.debug(f"--- SYSTEM ({subtype}): {str(data)[:200]}")

        else:
            logger.debug(f"--- {msg_type}: {str(message)[:200]}")

    @staticmethod
    def _resolve_pending_command(
        tool_use_id: str,
        is_error: bool,
        content: Any,
        pending_commands: dict[str, dict[str, Any]],
        processed_results: set[str],
    ) -> None:
        """Match a tool result back to its pending command and update status/duration.

        Args:
            tool_use_id: The tool use ID from the result
            is_error: Whether the tool execution resulted in an error
            content: The result content (string or structured)
            pending_commands: Map of tool_id -> {telemetry, command_start_time}
            processed_results: Set of already-processed tool IDs (for duplicate detection)
        """
        # Normalize content to string for storage
        content_str = str(content) if content is not None else ""

        if tool_use_id in pending_commands:
            cmd_data = pending_commands[tool_use_id]
            cmd = cmd_data["telemetry"]
            command_start_time = cmd_data["command_start_time"]

            # Calculate precise duration
            command_end_time = time.monotonic()
            duration_ms = (command_end_time - command_start_time) * 1000

            # Update command with actual results
            cmd.result_status = "error" if is_error else "success"
            cmd.duration_ms = duration_ms
            cmd.result_summary = content_str[:200] if content_str else None

            if is_error:
                cmd.error_message = content_str

                # Detect permission-blocked tool use and log at INFO level
                content_lower = content_str.lower()
                if any(
                    phrase in content_lower
                    for phrase in ("permission", "not allowed", "requires approval", "denied", "blocked")
                ):
                    logger.info(
                        f"Tool use blocked: {cmd.tool_name} (id={tool_use_id}) "
                        + f"- permission denied. Error: {content_str[:200]}"
                    )

            if tool_use_id in processed_results:
                logger.debug(f"Multiple results for tool_id={tool_use_id}. Last result wins.")
            processed_results.add(tool_use_id)
        else:
            logger.warning(
                f"Tool result received for unknown tool_use_id={tool_use_id}. No matching ToolUseBlock found."
            )

    @staticmethod
    def _build_stderr_message(sdk_stderr: str | None, stderr_lines: list[str]) -> str:
        """Combine SDK stderr with captured stderr lines, filtering out placeholder text.

        The SDK often returns a hardcoded placeholder like "Check stderr output for details"
        instead of actual error content. The real error details are in stderr_lines captured
        via the stderr callback.

        Args:
            sdk_stderr: The stderr string from ProcessError (may be a placeholder)
            stderr_lines: Lines captured via the stderr callback during execution

        Returns:
            Combined stderr message with real content, or "No stderr captured"
        """
        parts = []

        # Include SDK stderr only if it's not the hardcoded placeholder
        if sdk_stderr and "check stderr output" not in sdk_stderr.lower():
            parts.append(sdk_stderr)

        # Always include captured stderr lines (these contain the real error details)
        if stderr_lines:
            parts.append("\n".join(stderr_lines[-20:]))

        return "\n".join(parts) if parts else "No stderr captured"

    @staticmethod
    def _extract_cause_stderr(error: Exception) -> str | None:
        """Walk the exception __cause__ chain looking for a ProcessError with stderr.

        The SDK re-raises ProcessError as a generic Exception via the Query message stream.
        This method recovers the original stderr from the cause chain.

        Args:
            error: The caught exception

        Returns:
            stderr string from the original ProcessError, or None
        """
        cause = error.__cause__
        depth = 0
        while cause and depth < 5:
            if isinstance(cause, ProcessError):
                return cause.stderr
            cause = cause.__cause__
            depth += 1
        return None

    @staticmethod
    def _clean_error_message(message: str) -> str:
        """Remove unhelpful SDK placeholder text from error messages.

        Args:
            message: Raw error message string

        Returns:
            Cleaned error message
        """
        # Remove the hardcoded placeholder that the SDK injects
        cleaned = message.replace("\nError output: Check stderr output for details", "")
        cleaned = cleaned.replace("Error output: Check stderr output for details", "")
        return cleaned.strip()

    @staticmethod
    def _extract_error_from_messages(messages: list[Message]) -> str | None:
        """Extract error details from messages received before a crash.

        When the CLI process crashes, it may have sent a ResultMessage with
        is_error=True and error details in the 'result' field before exiting.
        This method scans collected messages for such error information.

        Args:
            messages: Messages collected during the turn before the error

        Returns:
            Error description if found, None otherwise
        """
        for msg in reversed(messages):
            # Check for ResultMessage with error info
            if _is_sdk_result_message(msg) and getattr(msg, "is_error", False):
                result = getattr(msg, "result", None)
                if result:
                    return str(result)[:500]

            # Check for SystemMessage with error subtype
            if type(msg).__name__ == "SystemMessage":
                subtype = getattr(msg, "subtype", None)
                data = getattr(msg, "data", None)
                if subtype == "error" and data:
                    return str(data)[:500]

        return None

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
        patterns = get_ignore_patterns(self.config.ignore_patterns)
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
                # Assistant text responses — content may be a string or list of blocks
                content = getattr(msg, "content", "")
                if isinstance(content, list):
                    for block in content:
                        text = getattr(block, "text", None)
                        if text:
                            formatted_parts.append(f"[ASSISTANT] {text}")
                elif content:
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
