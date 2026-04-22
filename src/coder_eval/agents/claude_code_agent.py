"""Claude Code agent implementation using the Claude Agent SDK."""

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Message, ProcessError, query

# Private SDK import — the public `query()` API doesn't expose the subprocess
# handle, but we need it to hard-kill on timeout (the SDK's anyio task groups
# swallow asyncio cancellation, so wait_for alone doesn't preempt a stuck CLI).
# If this import breaks on an SDK upgrade, fall back to the wait_for-only path.
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from coder_eval.agent import Agent, AgentState
from coder_eval.errors.timeout import TurnTimeoutError
from coder_eval.formatting import format_payload
from coder_eval.models import (
    AgentConfig,
    ApiRoute,
    BedrockRoute,
    CommandTelemetry,
    DirectRoute,
    FileChange,
    FileChanges,
    FileTree,
    ProxyRoute,
    TokenUsage,
    TurnRecord,
)
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


_SENSITIVE_ENV_KEYWORDS = {"TOKEN", "KEY", "SECRET"}

_JSON_START_SEARCH_LIMIT = 200


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    """Redact sensitive values from an environment variable dict.

    Keys containing TOKEN, KEY, or SECRET (case-insensitive) are replaced with ***REDACTED***.
    """
    return {
        k: "***REDACTED***" if any(kw in k.upper() for kw in _SENSITIVE_ENV_KEYWORDS) else v for k, v in env.items()
    }


def _dump_sdk_options(opts: ClaudeAgentOptions) -> dict[str, Any]:
    """Dump ClaudeAgentOptions to a plain dict, skipping non-serializable values.

    Recursively traverses dataclass fields and nested structures (dicts, lists,
    dataclasses). Skips callables and file-like objects. Converts Path to str.
    Redacts sensitive environment variables (tokens, keys, secrets).

    Args:
        opts: ClaudeAgentOptions dataclass instance

    Returns:
        Dictionary of field names to JSON-serializable values
    """
    result: dict[str, Any] = {}
    for field in dataclasses.fields(opts):
        value = _serialize_value(getattr(opts, field.name))
        if value is not _SKIP:
            if field.name == "env" and isinstance(value, dict):
                value = _redact_env(value)
            result[field.name] = value
    return result


class ClaudeCodeAgent(Agent):
    """Implementation of the Agent interface for Claude Code using the SDK."""

    def __init__(self, config: AgentConfig, route: ApiRoute | None = None):
        """Initialize the Claude Code agent.

        Args:
            config: Agent configuration
            route: API routing configuration. If None, uses DirectRoute.
        """
        self.config = config
        self.route = route or DirectRoute()
        self.client: ClaudeSDKClient | None = None
        self.working_directory: Path | None = None
        self._state = AgentState.WORKING
        self._iteration = 0
        self._sdk_options_dump: dict[str, Any] | None = None
        self._session_id: str | None = None
        # Transport reference held only while a communicate() call is in flight,
        # so kill() can reach into the CLI subprocess when the SDK swallows
        # asyncio cancellation.
        self._active_transport: SubprocessCLITransport | None = None

    async def start(self, working_directory: str) -> None:
        """Initialize and start the Claude Code agent.

        Args:
            working_directory: Path to the working directory
        """
        self.working_directory = Path(working_directory)
        self._state = AgentState.WORKING
        # Note: Client is created per-communication to avoid transport issues

    @staticmethod
    def _build_sdk_env(route: ApiRoute) -> tuple[dict[str, str], str | None]:
        """Build SDK environment variables and resolve effective model for the given route.

        Returns:
            Tuple of (env_vars_dict, model_override_or_None).
        """
        # Start with PATH from parent environment to ensure agent can locate executables
        base_env: dict[str, str] = {}
        if path := os.environ.get("PATH"):
            base_env["PATH"] = path

        match route:
            case BedrockRoute() as br:
                env: dict[str, str] = {
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "AWS_BEARER_TOKEN_BEDROCK": br.bearer_token,
                    "AWS_REGION": br.region,
                }
                if br.disable_attribution_header:
                    # FIXME(SDK#24168): Remove when SDK no longer injects reserved header
                    env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"
                if br.model:
                    env["ANTHROPIC_MODEL"] = br.model
                if br.small_model:
                    env["ANTHROPIC_SMALL_FAST_MODEL"] = br.small_model
                return {**base_env, **env}, br.model

            case ProxyRoute() as pr:
                return {
                    **base_env,
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{pr.port}",
                    "ANTHROPIC_API_KEY": "llmgw-proxy",
                }, None

            case DirectRoute():
                return base_env, None

        raise AssertionError(f"Unhandled route type: {type(route).__name__}")

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
    ) -> TurnRecord:
        """Send a message to Claude and receive its response.

        Args:
            user_input: The message/prompt to send
            stream_callback: Optional callback for real-time event streaming
            timeout: Hard wall-clock deadline in seconds. When exceeded, a
                watchdog task force-kills the CLI subprocess (the SDK's anyio
                task groups suppress cooperative cancellation, so a graceful
                asyncio.wait_for is not sufficient).

        Returns:
            TurnRecord containing the complete interaction

        Raises:
            RuntimeError: If agent is not started
            TurnTimeoutError: If timeout elapsed before the turn completed
        """
        if not self.working_directory:
            raise RuntimeError("Agent not started. Call start() first.")

        self._iteration += 1
        turn_start_time = time.monotonic()
        deadline = turn_start_time + timeout if timeout is not None else None
        timeout_hit = False

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
        sdk_num_turns: int | None = None

        # Model identifier from AssistantMessage (last one wins)
        sdk_model_used: str | None = None

        # Count of AssistantMessage objects in this turn
        assistant_turn_count = 0

        # Capture stderr for debugging
        stderr_lines = []

        def capture_stderr(line: str) -> None:
            stderr_lines.append(line)

        # Hoisted out of the try so the finally can reference them on any
        # exit path (including an exception raised before the loop starts).
        watchdog_task: asyncio.Task[None] | None = None

        try:
            # Process plugins: copy from config and replace env vars in paths
            plugins = self._process_plugins(self.config.plugins or [])  # type: ignore[arg-type]

            # Build env overrides and resolve model for the configured API route
            env, route_model = self._build_sdk_env(self.route)
            effective_model = route_model or self.config.model

            disallowed_tools = list(self.config.disallowed_tools or [])
            # Do not allow ToolSearch. This is required to keep Bedrock backend in sync with the other backends.
            if "ToolSearch" not in disallowed_tools:
                disallowed_tools.append("ToolSearch")

            options = ClaudeAgentOptions(
                cwd=str(self.working_directory),
                permission_mode=self.config.permission_mode,
                allowed_tools=self.config.allowed_tools or [],
                disallowed_tools=disallowed_tools,
                model=effective_model,
                max_turns=self.config.max_turns,
                plugins=plugins,  # type: ignore[arg-type]
                stderr=capture_stderr,  # Capture stderr for better error messages
                env=env,
                system_prompt=self.config.system_prompt,
                setting_sources=self.config.setting_sources if self.config.setting_sources is not None else ["project"],
                resume=self._session_id,
            )

            # Dump SDK options for later inspection (captures all 37+ fields including defaults)
            self._sdk_options_dump = _dump_sdk_options(options)

            # When a timeout is set, pre-construct the transport ourselves and
            # hand it to query() so we retain a reference to the subprocess
            # for hard-kill. The SDK's default path creates this internally
            # and never exposes it. When no timeout is set we pass
            # transport=None so the SDK uses its own default (keeps the door
            # open for tests that mock query() without needing a real CLI).
            transport: SubprocessCLITransport | None = None
            if timeout is not None:
                transport = SubprocessCLITransport(prompt=user_input, options=options)
                self._active_transport = transport

                # Watchdog: the only reliable way to preempt a stuck SDK call.
                # Runs independently of the message loop, so even if the CLI
                # stops streaming (no messages to check against the deadline)
                # the kill still fires after `timeout` seconds.
                # IMPORTANT: the transport is captured in the closure (not
                # read from self._active_transport) so a stale watchdog from
                # an earlier turn cannot kill a subsequent turn's subprocess.
                watchdog_target = transport

                async def _watchdog(duration_s: float) -> None:
                    nonlocal timeout_hit
                    await asyncio.sleep(duration_s)
                    timeout_hit = True
                    logger.warning(
                        "Turn timeout (%.0fs) watchdog firing — hard-killing Claude CLI subprocess", duration_s
                    )
                    self._kill_transport(watchdog_target)

                watchdog_task = asyncio.create_task(_watchdog(timeout))

            # Use the query function for one-shot interaction. Only forward
            # the transport kwarg when we actually built one — otherwise keep
            # the call shape identical to the pre-timeout code path so mocks
            # with strict signatures (prompt, options) keep working.
            query_kwargs: dict[str, Any] = {"prompt": user_input, "options": options}
            if transport is not None:
                query_kwargs["transport"] = transport
            logger.debug("Starting agent query stream...")
            async for message in query(**query_kwargs):
                # Wall-clock guard inside the loop. Triggers the cooperative
                # exit path when messages are still flowing; the watchdog is
                # the fallback when they aren't.
                if deadline is not None and time.monotonic() > deadline:
                    timeout_hit = True
                    logger.warning("Turn timeout reached mid-stream; breaking out of message loop")
                    break

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
                    sdk_num_turns = getattr(message, "num_turns", None)
                    # Capture session_id so subsequent communicate() calls resume this conversation
                    new_session_id = getattr(message, "session_id", None)
                    if new_session_id != self._session_id:
                        logger.debug("session_id changed: %s -> %s", self._session_id, new_session_id)
                    self._session_id = new_session_id

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
                                safe_emit(
                                    stream_callback,
                                    ToolResultEvent(
                                        task_id=self.config.type.value,
                                        tool_id=block.tool_use_id,
                                        tool_name=tool_name,
                                        success=not is_error_flag,
                                        result_preview=format_payload(block.content),
                                    ),
                                )

            logger.debug("Agent query stream ended")

        except ProcessError as e:
            # When the watchdog SIGKILLs the subprocess, the SDK surfaces it
            # as a ProcessError (exit code -9). Classify as a timeout so the
            # retry system doesn't treat it as a transient AGENT_CRASH.
            if self._timed_out(timeout_hit, deadline):
                self._state = AgentState.ERROR
                raise TurnTimeoutError(timeout if timeout is not None else 0.0, iteration=self._iteration) from e
            self._state = AgentState.ERROR
            stderr = self._build_stderr_message(e.stderr, stderr_lines)
            error_info = self._extract_error_from_messages(messages)
            detail = error_info or stderr
            raise RuntimeError(f"CLI process failed (exit code {e.exit_code}): {detail}") from e
        except Exception as e:
            # Same race as above: the watchdog may have killed the subprocess
            # and the SDK may have re-raised as a generic Exception. Check
            # both the flag AND the wall-clock in case the flag flip races
            # with our catch-entry.
            if self._timed_out(timeout_hit, deadline):
                self._state = AgentState.ERROR
                raise TurnTimeoutError(timeout if timeout is not None else 0.0, iteration=self._iteration) from e
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
        finally:
            if watchdog_task is not None and not watchdog_task.done():
                watchdog_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await watchdog_task
            self._active_transport = None

        # Only trust `timeout_hit` in the happy path: if the loop completed
        # cleanly, a wall-clock drift during post-loop cleanup would falsely
        # classify a successful turn as a timeout. The watchdog and in-loop
        # guard are the authoritative signals.
        if timeout_hit:
            raise TurnTimeoutError(timeout if timeout is not None else 0.0, iteration=self._iteration)

        # PHASE 3: Finalize commands and build turn record
        commands = self._finalize_commands(pending_commands, messages)
        token_usage = self._build_token_usage(sdk_result_usage, sdk_result_cost)
        files_after = self._capture_file_tree()
        file_changes = self._detect_file_changes(files_before, files_after)
        agent_output = self._format_messages(messages)
        self._update_state_from_messages(messages)

        # Detect max_turns exhaustion: SDK used all available turns without the agent voluntarily completing.
        # Use strict > because if the agent finishes voluntarily on exactly turn N (== max_turns),
        # that is a normal completion, not exhaustion. The SDK reports num_turns > max_turns when
        # it forcibly stops the agent.
        max_turns_exhausted = (
            self.config.max_turns is not None and sdk_num_turns is not None and sdk_num_turns > self.config.max_turns
        )
        if max_turns_exhausted:
            logger.warning(
                "Agent exhausted max_turns (%d/%d) — the SDK hit the turn limit before the agent completed.",
                sdk_num_turns,
                self.config.max_turns,
            )

        duration = time.monotonic() - turn_start_time

        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output=agent_output,
            commands=commands,
            files_changed=file_changes,
            timestamp=datetime.now(),
            duration_seconds=duration,
            token_usage=token_usage,
            model_used=sdk_model_used,
            assistant_turn_count=assistant_turn_count,
            max_turns_exhausted=max_turns_exhausted,
        )

    async def stop(self) -> None:
        """Stop the agent and clean up resources."""
        # Clean up any resources
        # Note: Client is created per-communication using async context manager
        self.client = None
        self._state = AgentState.FINISHED

    async def kill(self) -> None:
        """Force-terminate the in-flight Claude CLI subprocess, if any.

        Called by the orchestrator when a timeout fires on its backstop path.
        The watchdog inside communicate() uses _kill_transport directly (with
        the transport captured in its closure) to avoid a cross-turn race
        where a stale watchdog could kill a later turn's subprocess.
        """
        self._kill_transport(self._active_transport)

    @staticmethod
    def _timed_out(timeout_hit: bool, deadline: float | None) -> bool:
        """Return True if the turn has exceeded its deadline by either path.

        Checks both the watchdog flag AND the wall clock. The flag-only check
        races with the watchdog: if the handler was entered just before the
        watchdog flipped the flag, we'd misreport a timeout as a generic
        error. Checking wall-clock is the belt that catches that case.
        """
        if timeout_hit:
            return True
        return deadline is not None and time.monotonic() > deadline

    @staticmethod
    def _kill_transport(transport: SubprocessCLITransport | None) -> None:
        """SIGKILL the subprocess behind `transport`, if any.

        The SDK wraps the subprocess in anyio cancel scopes that suppress
        asyncio.CancelledError, so cooperative cancellation doesn't reliably
        stop a stuck CLI. Sending SIGKILL releases stdout/stdin, which
        unblocks the anyio readers so the async generator unwinds cleanly.
        """
        if transport is None:
            return
        # _process is set by transport.connect(); may be None if the call failed
        # before connect, or already cleared by the SDK's own cleanup.
        proc = getattr(transport, "_process", None)
        if proc is None or proc.returncode is not None:
            return
        logger.warning("Hard-killing Claude CLI subprocess (pid=%s)", getattr(proc, "pid", "?"))
        with suppress(ProcessLookupError, Exception):
            proc.kill()

    def get_state(self) -> AgentState:
        """Get the current state of the agent.

        Returns:
            Current agent state
        """
        return self._state

    @staticmethod
    def _finalize_commands(
        pending_commands: dict[str, dict[str, Any]], messages: list[Message]
    ) -> list[CommandTelemetry]:
        """Convert pending commands to a finalized list, marking unresolved ones as unknown."""
        commands: list[CommandTelemetry] = []
        unknown_status_count = 0

        for tool_id, cmd_data in pending_commands.items():
            cmd = cmd_data["telemetry"]
            if cmd.result_status is None:
                cmd.result_status = "unknown"
                unknown_status_count += 1
                logger.warning(
                    f"Command {cmd.tool_name}:{tool_id} completed without tool result. "
                    + "Status set to 'unknown'. This may indicate agent interruption or SDK issue."
                )
            if cmd.duration_ms is None:
                cmd.duration_ms = 0.0
            commands.append(cmd)

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

        return commands

    @staticmethod
    def _build_token_usage(sdk_result_usage: dict[str, Any] | None, sdk_result_cost: float | None) -> TokenUsage | None:
        """Build a TokenUsage from SDK ResultMessage data, or None if unavailable."""
        if not sdk_result_usage:
            return None
        return TokenUsage(
            input_tokens=sdk_result_usage.get("input_tokens", 0),
            output_tokens=sdk_result_usage.get("output_tokens", 0),
            cache_creation_input_tokens=sdk_result_usage.get("cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=sdk_result_usage.get("cache_read_input_tokens", 0) or 0,
            total_cost_usd=sdk_result_cost,
        )

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

                # Expand all env vars in the path, then resolve relative paths
                # against the process cwd (not the sandbox cwd) so plugins are found
                expanded = os.path.expandvars(path)
                processed_plugin["path"] = str(Path(expanded).resolve())

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
                        params_str = format_payload(block.input, max_chars=800)
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
                        result_preview = (
                            format_payload(block.content, max_chars=800) if block.content is not None else "(empty)"
                        )
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
    def _try_parse_json_value(content: Any) -> dict[str, Any] | list[Any] | None:
        """Return the parsed JSON object or array from content, else None.

        Strict telemetry-capture variant. ``coder_eval.formatting._extract_json``
        is the lenient display-path variant — keep behaviour aligned when you
        change one, but they are intentionally separate: the telemetry path
        feeds ``CommandTelemetry.result_data`` where false positives persist
        into ``task.json`` and downstream dashboards.

        Accepts the two SDK-delivered shapes for ToolResultBlock.content: a plain
        string, or a list of content blocks (MCP tools use this, e.g.
        [{"type": "text", "text": "..."}]). Within the first 200 characters,
        looks for the first line whose first non-whitespace character is `{` or
        `[` and parses from there using raw_decode, so prefix noise (e.g. warning
        lines the `uip` CLI prints before the JSON body) and trailing garbage are
        tolerated. Requiring the brace to start a line avoids false positives
        from incidental `{` or `[` embedded inside text (e.g. the Read tool's
        line-numbered source where `items: list = []` would otherwise parse as
        an empty list). The 200-char cap further rules out braces buried deep in
        long text output. If the candidate fails to parse, returns None — no
        fragment fallback, which would surface misleading partial captures from
        truncated payloads. Bare empty containers (`{}` / `[]`) are rejected:
        a non-empty dict or list is evidence of real structured content.
        Primitives (strings, numbers, booleans, null) are rejected for the same
        reason — a bare primitive adds no information beyond result_summary.
        Non-JSON tool output is normal; parse failures are swallowed silently.
        """
        if isinstance(content, list):
            text_parts = [
                block["text"]
                for block in content
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            ]
            if not text_parts:
                return None
            content = "".join(text_parts)
        if not isinstance(content, str):
            return None
        # Only look for the JSON start within the first 200 chars — enough to skip
        # a few prefix warning lines but not so lax that a brace buried in a long
        # text body gets mistaken for a structured payload.
        match = re.search(r"(?:^|\n)[^\S\n]*[{[]", content[:_JSON_START_SEARCH_LIMIT])
        if not match:
            return None
        try:
            parsed, _ = json.JSONDecoder().raw_decode(content, match.end() - 1)
        except ValueError:
            return None
        # Reject bare empty containers ({} / []): a non-empty dict or list is
        # evidence of real structured content, an empty one is indistinguishable
        # from an accidental match and adds nothing over result_summary.
        if isinstance(parsed, (dict, list)) and parsed:
            return parsed
        return None

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
            cmd.result_data = ClaudeCodeAgent._try_parse_json_value(content)

            if is_error:
                cmd.error_message = content_str

                # Permission-blocked tool use is abnormal flow — warn so it
                # surfaces in runs that don't have DEBUG enabled.
                content_lower = content_str.lower()
                if any(
                    phrase in content_lower
                    for phrase in ("permission", "not allowed", "requires approval", "denied", "blocked")
                ):
                    logger.warning(
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
                # SDK ResultMessage uses "result" (not "content") for the final text
                result_text = getattr(msg, "result", "") or ""
                is_error = getattr(msg, "is_error", False)
                status = "ERROR" if is_error else "SUCCESS"
                formatted_parts.append(f"[RESULT - {status}] {result_text}")

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
