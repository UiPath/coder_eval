"""Codex agent implementation using the official OpenAI Codex SDK."""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._logging import PrefixedAdapter
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import ThreadedWatchdog
from coder_eval.config import settings
from coder_eval.errors import (
    AgentCrashError,
    TurnTimeoutError,
    format_timeout_reason,
    truncate_crash_message,
)
from coder_eval.formatting import format_payload, format_token_usage
from coder_eval.models import (
    AgentKind,
    ApiRoute,
    CodexAgentConfig,
    CommandTelemetry,
    DirectRoute,
    TokenUsage,
    TurnRecord,
)
from coder_eval.streaming.callbacks import StreamCallback, safe_emit
from coder_eval.streaming.events import TextChunkEvent, ToolCallEvent, ToolResultEvent, TurnCompleteEvent


logger = logging.getLogger(__name__)

# Tool name mapping: Claude Code SDK names → Codex SDK names
_CLAUDE_TO_CODEX_TOOL_MAP: dict[str, str] = {
    "Bash": "shell",
    "Write": "apply_patch",
    "Edit": "apply_patch",
    "Read": "shell",
    "Grep": "shell",
    "Glob": "shell",
}

# Permission mode → sandbox mode mapping
_PERMISSION_MODE_TO_SANDBOX: dict[str, str] = {
    "bypassPermissions": "full-access",
    "acceptEdits": "workspace-write",
    "default": "workspace-write",
    "plan": "read-only",
}

# Permission mode → approval mode mapping
_PERMISSION_MODE_TO_APPROVAL: dict[str, str] = {
    "bypassPermissions": "auto_review",
    "acceptEdits": "auto_review",
    "default": "auto_review",
    "plan": "deny_all",
}

# Provider id registered in thread config when CODEX_BASE_URL routes to a
# custom endpoint.
_CUSTOM_PROVIDER_ID = "custom"


def _get_item_root(notification: Any) -> Any:
    """Extract the typed item root from a Codex SDK notification.

    Handles nested getattr safely: notification.payload.item.root
    """
    payload = getattr(notification, "payload", None)
    if payload is None:
        return None
    item = getattr(payload, "item", None)
    if item is None:
        return None
    return getattr(item, "root", None)


@AgentRegistry.register(AgentKind.CODEX, CodexAgentConfig)
class CodexAgent(Agent[CodexAgentConfig]):
    """Implementation of the Agent interface for OpenAI Codex using the Codex SDK."""

    def __init__(
        self,
        config: CodexAgentConfig,
        route: ApiRoute | None = None,
        *,
        instance_name: str = "codex",
    ):
        """Initialize the Codex agent.

        Args:
            config: Agent configuration
            route: API routing configuration (unused for Codex, kept for interface compatibility)
            instance_name: Short label used to prefix this instance's log records
        """
        self.config = config
        self.route = route or DirectRoute()
        self.codex_client: Any = None
        self.thread: Any = None
        self.working_directory: Path | None = None
        self._state = AgentState.WORKING
        self._iteration = 0
        self._iteration_was_incremented = False
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})
        self.pending_turn: TurnRecord | None = None
        # Live handle to the in-flight turn, set by _run_turn_with_streaming and
        # cleared in its finally. kill()/kill_sync() use it to interrupt a stuck
        # turn — the watchdog's task.cancel() alone can't preempt a blocking SDK
        # call (it lands only at an await point, which we now create via to_thread).
        self._active_turn_handle: Any = None

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        """Initialize and start the Codex agent.

        Args:
            working_directory: Path to the working directory
            env_path_prepend: Optional PATH prepend directories (unused for Codex)
            plugin_tools_dir: Optional plugin tools directory (for skills setup)
        """
        self.working_directory = Path(working_directory)
        self._state = AgentState.WORKING

        try:
            from openai_codex import Codex, CodexConfig

            # Build CodexConfig with environment variables for custom API configuration
            env_override = self._build_codex_env()
            config = CodexConfig(env=env_override) if env_override else None

            # Initialize the Codex client (context manager compatible)
            self.codex_client = Codex(config=config)
            self._log.debug("Codex client initialized")

            # Authenticate with the API key when one is configured. Without this
            # the app-server falls back to an existing ChatGPT login, so headless
            # API-key runs (CI) would otherwise fail to authenticate.
            api_key = os.getenv("CODEX_API_KEY")
            if api_key:
                try:
                    await self._run_async(self.codex_client.login_api_key, api_key)
                except Exception as exc:
                    self._log.warning(
                        "CodexAgent: login_api_key failed — agent will fall back to env-based auth: %s", exc
                    )

            # Set up skills from plugin_tools_dir or plugins config
            self._setup_skills(plugin_tools_dir)

            # Log permission and tool configuration
            self._log_config_enforcement()

        except ImportError as e:
            raise RuntimeError("Codex SDK not installed. Install with: pip install 'coder-eval[codex]'") from e
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Codex client: {e}") from e

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
    ) -> TurnRecord:
        """Send a message to Codex and receive its response.

        Args:
            user_input: The message/prompt to send
            stream_callback: Optional callback for real-time event streaming
            timeout: Hard wall-clock deadline in seconds
            max_turns: Hard cap on inner-loop turns (unused for Codex single-turn)

        Returns:
            TurnRecord containing the complete interaction

        Raises:
            RuntimeError: If agent is not started
            TurnTimeoutError: Timeout elapsed
            AgentCrashError: SDK/CLI failed mid-turn
        """
        if not self.working_directory or not self.codex_client:
            raise RuntimeError("Agent not started. Call start() first.")

        assert self.config.type is not None, "CodexAgent requires AgentConfig.type to be set before communicate()"

        self.pending_turn = None

        turn_start_time = time.monotonic()
        timeout_hit = False

        self._iteration += 1
        self._iteration_was_incremented = True

        commands: list[CommandTelemetry] = []
        agent_output = ""
        streamed_text = ""
        sdk_token_usage: Any = None

        def _set_pending(crash_reason: str) -> None:
            try:
                self.pending_turn = TurnRecord(
                    iteration=self._iteration,
                    user_input=user_input,
                    agent_output=agent_output,
                    commands=commands,
                    timestamp=datetime.now(),
                    duration_seconds=time.monotonic() - turn_start_time,
                    token_usage=None,
                    model_used=None,
                    assistant_turn_count=0,
                    max_turns_exhausted=False,
                    crashed=True,
                    crash_reason=crash_reason,
                )
            except Exception:
                logger.exception("Failed to build partial turn record")
                self.pending_turn = None

        try:
            if self.thread is None:
                thread_kwargs = self._build_thread_options()
                # Add working directory
                if self.working_directory:
                    thread_kwargs["cwd"] = str(self.working_directory)
                self.thread = await self._run_async(self.codex_client.thread_start, **thread_kwargs)

            def _on_turn_timeout() -> None:
                nonlocal timeout_hit
                timeout_hit = True

            with ThreadedWatchdog(
                timeout_seconds=timeout,
                on_timeout=_on_turn_timeout,
                asyncio_task_to_cancel=asyncio.current_task(),
                label=f"Turn timeout ({timeout:g}s)" if timeout else "turn_timeout",
            ):
                self._log.debug("Starting Codex turn...")

                try:
                    turn_result, sdk_token_usage, streamed_text = await self._run_turn_with_streaming(
                        user_input, stream_callback, commands
                    )
                except asyncio.CancelledError:
                    if timeout_hit:
                        self._state = AgentState.ERROR
                        _set_pending(format_timeout_reason(timeout or 0))
                        raise TurnTimeoutError(timeout or 0, iteration=self._iteration) from None
                    raise
                except Exception as e:
                    if timeout_hit:
                        self._state = AgentState.ERROR
                        _set_pending(format_timeout_reason(timeout or 0))
                        raise TurnTimeoutError(timeout or 0, iteration=self._iteration) from e
                    self._state = AgentState.ERROR
                    message = truncate_crash_message(f"Codex turn failed: {e!s}")
                    _set_pending(message)
                    raise AgentCrashError(message) from e

            if timeout_hit:
                assert timeout is not None
                _set_pending(format_timeout_reason(timeout))
                raise TurnTimeoutError(timeout, iteration=self._iteration)

            # The turn/completed payload is a Turn (no final_response field); the
            # real assistant text arrives as agentMessage deltas during streaming.
            # Fall back to formatting the Turn only if no text was streamed.
            agent_output = streamed_text or self._format_turn_result(turn_result)
        except (AgentCrashError, TurnTimeoutError):
            # Already funneled through _set_pending by the inner handlers.
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Catches failures OUTSIDE the inner turn block — notably thread_start
            # and _format_turn_result. Without this, such errors escape as a bare
            # exception: the orchestrator never drains pending_turn and _iteration
            # stays incremented, violating the pending-turn contract.
            self._state = AgentState.ERROR
            message = truncate_crash_message(f"Codex turn failed: {e!s}")
            _set_pending(message)
            raise AgentCrashError(message) from e

        self._state = AgentState.WORKING
        self._iteration_was_incremented = False
        duration = time.monotonic() - turn_start_time

        token_usage = self._token_usage_from_sdk(sdk_token_usage)
        # The Turn payload doesn't carry the model; fall back to the pinned config
        # model so model-keyed report aggregation has a value when one was set.
        model_used = getattr(turn_result, "model", None) or self.config.model

        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output=agent_output,
            commands=commands,
            timestamp=datetime.now(),
            duration_seconds=duration,
            token_usage=token_usage,
            model_used=model_used,
            assistant_turn_count=1,
            max_turns_exhausted=False,
        )

    async def stop(self) -> None:
        """Stop the agent and tear down the Codex SDK session.

        ``Codex(config=...)`` eagerly spawns an app-server subprocess plus reader
        threads; ``close()`` reaps them. Skipping it leaks a subprocess + threads
        per task across a batch run, so close before nulling the reference.
        """
        self._close_client()
        self.thread = None
        self._active_turn_handle = None
        self.pending_turn = None
        self._state = AgentState.FINISHED

    async def kill(self) -> None:
        """Force-terminate the agent: interrupt any in-flight turn, then tear down."""
        self._interrupt_active_turn()
        await self.stop()

    def kill_sync(self) -> None:
        """Synchronous abort for the watchdog thread (cannot await coroutines).

        Best-effort: interrupt the in-flight turn so the blocked stream iteration
        unblocks, then close the client. Safe to call at any time and idempotent.
        """
        self._interrupt_active_turn()
        self._close_client()

    def _interrupt_active_turn(self) -> None:
        """Interrupt the in-flight Codex turn, if any (best-effort, idempotent)."""
        handle = self._active_turn_handle
        if handle is None:
            return
        with contextlib.suppress(Exception):
            handle.interrupt()

    def _close_client(self) -> None:
        """Close the Codex SDK client (best-effort), reaping its subprocess/threads."""
        client = self.codex_client
        self.codex_client = None
        if client is None:
            return
        with contextlib.suppress(Exception):
            client.close()

    async def discard_pending_turn(self) -> None:
        """Clear pending_turn and roll back the iteration counter.

        Rolls back when either signal says a turn was attempted: the
        ``_iteration_was_incremented`` flag (survives ``_set_pending`` swallowing
        a partial-build exception, which leaves ``pending_turn=None``) or a
        non-None ``pending_turn`` (for callers that set it directly). Idempotent.
        """
        should_rollback = self._iteration_was_incremented or self.pending_turn is not None
        self.pending_turn = None
        self._iteration_was_incremented = False
        if should_rollback and self._iteration > 0:
            self._iteration -= 1

    def get_state(self) -> AgentState:
        """Get the current state of the agent."""
        return self._state

    def _setup_skills(self, plugin_tools_dir: str | None) -> None:
        """Set up .agents/skills directory from plugins or plugin_tools_dir.

        Codex auto-discovers skills in .agents/skills/ directories scanned from
        the working directory up through parent directories to repo root.

        Skills are collected from two sources:
        1. config.plugins - task-defined plugins with type='local' and path pointing to skills
        2. plugin_tools_dir parameter - runtime plugin directory

        Supports SKILL.md files following the Agent Skills open standard.
        Creates .agents/skills/ directory and symlinks/copies skill directories.
        """
        if not self.working_directory:
            return

        skills_sources: list[Path] = []

        # Collect skills directories from config.plugins
        if self.config.plugins:
            for plugin in self.config.plugins:
                if isinstance(plugin, dict) and plugin.get("type") == "local":
                    path_str = plugin.get("path")
                    if path_str:
                        # Expand environment variables in path
                        expanded_path = self._expand_env_vars(path_str)
                        plugin_path = Path(expanded_path)
                        if plugin_path.exists() and plugin_path.is_dir():
                            skills_sources.append(plugin_path)
                            self._log.debug(f"Found skills from plugin: {plugin_path}")

        # Also check plugin_tools_dir parameter
        if plugin_tools_dir:
            plugin_path = Path(plugin_tools_dir)
            if plugin_path.exists() and plugin_path.is_dir():
                skills_sources.append(plugin_path)
                self._log.debug(f"Found skills from plugin_tools_dir: {plugin_path}")

        if not skills_sources:
            return

        # Create .agents/skills directory (Codex auto-discovery location)
        agents_skills_dir = self.working_directory / ".agents" / "skills"
        try:
            agents_skills_dir.mkdir(parents=True, exist_ok=True)

            # Symlink or copy skills from all sources
            for skills_source in skills_sources:
                for skill_dir in skills_source.iterdir():
                    if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                        target = agents_skills_dir / skill_dir.name
                        if target.exists():
                            # Skip if already exists (first source wins)
                            continue

                        try:
                            # Try to create a symlink for efficiency
                            target.symlink_to(skill_dir)
                            self._log.debug(f"Linked skill: {skill_dir.name}")
                        except (OSError, NotImplementedError):
                            # Fall back to copying if symlink fails (Windows compatibility)
                            shutil.copytree(skill_dir, target, dirs_exist_ok=True)
                            self._log.debug(f"Copied skill: {skill_dir.name}")

            self._log.debug(f"Skills set up in {agents_skills_dir}")

        except Exception as e:
            self._log.warning(f"Failed to set up skills: {e}")

    def _expand_env_vars(self, path_str: str) -> str:
        """Expand environment variables in a path string.

        Supports $VAR and ${VAR} syntax.
        """
        var_pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

        def replace_var(match: re.Match[str]) -> str:
            var_name = match.group(1) or match.group(2)
            return os.environ.get(var_name, match.group(0))

        return var_pattern.sub(replace_var, path_str)

    @staticmethod
    def _resolve_base_url() -> str | None:
        """Custom Codex endpoint base URL from CODEX_BASE_URL, or None."""
        return os.getenv("CODEX_BASE_URL") or None

    def _effective_model(self) -> str | None:
        """Resolve the model: task/CLI ``agent.model`` wins, else CODEX_MODEL.

        Mirrors the Claude agent's ``config_model or route_model`` precedence
        (where the route fallback is BEDROCK_MODEL); here the fallback is the
        settings-backed CODEX_MODEL.
        """
        return self.config.model or settings.codex_model

    def _build_codex_env(self) -> dict[str, str] | None:
        """Build the environment passed to the Codex app-server.

        Only the API key travels via env (``CODEX_API_KEY``); the codex binary
        reads it when a model provider's ``env_key`` points at it. The base URL
        is NOT an env var the binary honors — it is applied through the model
        provider config in ``_build_thread_options`` instead.
        """
        env: dict[str, str] = {}
        api_key = os.getenv("CODEX_API_KEY")
        if api_key:
            env["CODEX_API_KEY"] = api_key
            self._log.debug("CODEX_API_KEY configured")
        return env if env else None

    def _build_thread_options(self) -> dict[str, Any]:
        """Build thread_start options from agent config.

        Returns a dict with sandbox, approval_mode, and config parameters
        for thread_start() based on permission_mode, allowed_tools, and disallowed_tools.
        """
        from openai_codex.api import ApprovalMode, Sandbox  # pyright: ignore[reportPrivateImportUsage]

        options: dict[str, Any] = {}

        # Pin the model when one is resolved; otherwise Codex picks its default
        # and two runs can silently differ.
        effective_model = self._effective_model()
        if effective_model:
            options["model"] = effective_model
            self._log.debug(f"Codex model pinned to {effective_model}")

        # Map permission_mode to sandbox and approval_mode
        permission_mode = self.config.permission_mode.value
        sandbox_mode_str = _PERMISSION_MODE_TO_SANDBOX.get(permission_mode, "workspace-write")
        approval_mode_str = _PERMISSION_MODE_TO_APPROVAL.get(permission_mode, "auto_review")

        # Convert to Codex SDK enum values
        # Sandbox enum values use hyphens, ApprovalMode uses underscores
        options["sandbox"] = Sandbox(sandbox_mode_str)
        options["approval_mode"] = ApprovalMode(approval_mode_str)

        # For logging, use the enum names (which use underscores)
        sandbox_name = options["sandbox"].name
        approval_name = options["approval_mode"].name

        self._log.debug(f"Permission mode {permission_mode} → sandbox={sandbox_name}, approval_mode={approval_name}")

        # Build config dict for tool enforcement
        tool_config: dict[str, Any] = {}

        if self.config.allowed_tools:
            enabled_tools = [_CLAUDE_TO_CODEX_TOOL_MAP.get(tool, tool) for tool in self.config.allowed_tools]
            tool_config["enabled_tools"] = enabled_tools
            normalized = ", ".join(enabled_tools)
            self._log.debug(f"Allowed tools (normalized): {normalized}")

        if self.config.disallowed_tools:
            disabled_tools = [_CLAUDE_TO_CODEX_TOOL_MAP.get(tool, tool) for tool in self.config.disallowed_tools]
            tool_config["disabled_tools"] = disabled_tools
            normalized = ", ".join(disabled_tools)
            self._log.warning(
                f"disallowed_tools ({normalized}) is passed to Codex but NOT enforced by the SDK; "
                + "do not rely on it as a security boundary."
            )

        # Route through a custom endpoint (e.g. an OpenAI-/responses-compatible
        # gateway) when CODEX_BASE_URL is set. The codex binary has no base-URL
        # env var — a model provider must be defined in config and selected, with
        # env_key naming the env var that holds the key (CODEX_API_KEY).
        base_url = self._resolve_base_url()
        if base_url:
            options["model_provider"] = _CUSTOM_PROVIDER_ID
            if not effective_model:
                self._log.warning(
                    "CODEX_BASE_URL is set but no model resolved (agent.model / CODEX_MODEL) "
                    + "— the provider may reject the request."
                )
            tool_config["model_providers"] = {
                _CUSTOM_PROVIDER_ID: {
                    "name": "Custom",
                    "base_url": base_url,
                    "env_key": "CODEX_API_KEY",
                    "wire_api": "responses",
                }
            }
            self._log.debug(f"Codex routed via custom provider (host={urlparse(base_url).hostname or '(unknown)'})")

        if tool_config:
            options["config"] = tool_config

        return options

    def _log_config_enforcement(self) -> None:
        """Log configuration settings."""
        if self.config.allowed_tools:
            self._log.debug(f"Allowed tools: {', '.join(self.config.allowed_tools)}")

        if self.config.disallowed_tools:
            self._log.debug(f"Disallowed tools: {', '.join(self.config.disallowed_tools)}")

        self._log.debug(f"Permission mode: {self.config.permission_mode.value}")
        if self.config.permission_mode.value == "bypassPermissions":
            self._log.warning(
                "[SECURITY] bypassPermissions grants unrestricted sandbox access (full-access). "
                + "Only use in fully isolated environments with untrusted code execution disabled."
            )

    def _format_turn_result(self, turn_result: Any) -> str:
        """Format Codex turn result to readable string."""
        try:
            final_response = getattr(turn_result, "final_response", None)
            if final_response:
                return str(final_response)

            result_dict = turn_result.model_dump() if hasattr(turn_result, "model_dump") else vars(turn_result)
            return json.dumps(result_dict, indent=2, default=str)
        except Exception as e:
            self._log.warning(f"Failed to format turn result: {e}")
            return str(turn_result)

    async def _run_turn_with_streaming(
        self, user_input: str, stream_callback: StreamCallback | None, commands: list[CommandTelemetry]
    ) -> tuple[Any, Any, str]:
        """Run a turn with streaming support, emitting typed events in real-time.

        Uses turn.stream() to get event notifications and emits ToolCallEvent,
        ToolResultEvent, TextChunkEvent, and TurnCompleteEvent to stream_callback.
        Extracts command telemetry from CommandExecutionThreadItem events.

        Returns:
            Tuple of (turn_result, latest_token_usage, agent_text) where turn_result
            is the final Turn object, latest_token_usage is the SDK's ThreadTokenUsage
            (or None), and agent_text is the assistant message assembled from the
            streamed agentMessage deltas.
        """
        from openai_codex.generated.v2_all import TurnCompletedNotification

        task_id = self.config.type.value if self.config.type else ""

        # Create turn handle (starts the turn but doesn't block)
        turn_handle = await self._run_async(self.thread.turn, user_input)
        self._active_turn_handle = turn_handle

        # Get the event stream
        stream = await self._run_async(turn_handle.stream)

        turn_result = None
        latest_token_usage: Any = None
        agent_message_chunks: list[str] = []
        # Sequence per executable item, assigned at item/started and reused at
        # item/completed via this id->seq map. Advancing only on successful
        # telemetry extraction (the old behavior) collided fallback IDs and froze
        # the counter whenever extraction returned None.
        next_sequence = 0
        seq_by_id: dict[str, int] = {}
        stream_iter = iter(stream)
        try:
            while True:
                # Offload the blocking SDK iteration to a worker thread so the
                # event loop stays free (parallel agents don't serialize) and the
                # watchdog's task.cancel() can actually land at this await point.
                try:
                    notification = await asyncio.to_thread(next, stream_iter)
                except StopIteration:
                    break

                method = notification.method

                # --- item/started: Emit ToolCallEvent for executable items ---
                if method == "item/started":
                    root = _get_item_root(notification)
                    if root is not None:
                        root_type = getattr(root, "type", None)
                        if root_type == "commandExecution":
                            command_id = getattr(root, "id", f"cmd_{next_sequence}")
                            seq_by_id[command_id] = next_sequence
                            command = getattr(root, "command", "")
                            safe_emit(
                                stream_callback,
                                ToolCallEvent(
                                    task_id=task_id,
                                    tool_name="Bash",
                                    tool_id=command_id,
                                    parameters={"command": command},
                                    sequence_number=next_sequence,
                                ),
                            )
                            next_sequence += 1
                        elif root_type == "fileChange":
                            change_id = getattr(root, "id", f"change_{next_sequence}")
                            seq_by_id[change_id] = next_sequence
                            changes = getattr(root, "changes", [])
                            path_preview = str(changes[0].path) if changes and hasattr(changes[0], "path") else "?"
                            safe_emit(
                                stream_callback,
                                ToolCallEvent(
                                    task_id=task_id,
                                    tool_name="Write",
                                    tool_id=change_id,
                                    parameters={"path": path_preview},
                                    sequence_number=next_sequence,
                                ),
                            )
                            next_sequence += 1

                # --- item/completed: Emit ToolResultEvent + extract CommandTelemetry ---
                elif method == "item/completed":
                    root = _get_item_root(notification)
                    if root is not None:
                        root_type = getattr(root, "type", None)
                        if root_type == "commandExecution":
                            command_id = getattr(root, "id", f"cmd_{next_sequence}")
                            seq = seq_by_id.get(command_id, next_sequence)
                            exit_code = getattr(root, "exit_code", None)
                            output = getattr(root, "aggregated_output", "") or ""

                            safe_emit(
                                stream_callback,
                                ToolResultEvent(
                                    task_id=task_id,
                                    tool_id=command_id,
                                    tool_name="Bash",
                                    success=exit_code == 0,
                                    result_preview=format_payload(output, max_chars=800),
                                ),
                            )

                            telemetry = self._extract_command_telemetry(root, seq)
                            if telemetry:
                                commands.append(telemetry)

                        elif root_type == "fileChange":
                            change_id = getattr(root, "id", f"change_{next_sequence}")
                            seq = seq_by_id.get(change_id, next_sequence)
                            changes = getattr(root, "changes", [])
                            status = getattr(root, "status", "success")

                            safe_emit(
                                stream_callback,
                                ToolResultEvent(
                                    task_id=task_id,
                                    tool_id=change_id,
                                    tool_name="Write",
                                    success=status != "error",
                                    result_preview=f"{len(changes)} file(s) changed",
                                ),
                            )

                            # Record the edit as telemetry so name-keyed criteria
                            # (command_executed) and commands_efficiency count file
                            # changes, matching how Claude counts Write/Edit calls.
                            telemetry = self._extract_file_change_telemetry(change_id, changes, status, seq)
                            if telemetry:
                                commands.append(telemetry)

                # --- item/agentMessage/delta: Emit TextChunkEvent for streaming text ---
                elif method == "item/agentMessage/delta":
                    if notification.payload:
                        delta = getattr(notification.payload, "delta", None)
                        if delta:
                            agent_message_chunks.append(delta)
                            safe_emit(
                                stream_callback,
                                TextChunkEvent(
                                    task_id=task_id,
                                    text=delta,
                                ),
                            )

                # --- thread/tokenUsage/updated: Capture for TurnCompleteEvent ---
                elif method == "thread/tokenUsage/updated":
                    if notification.payload:
                        latest_token_usage = getattr(notification.payload, "token_usage", None)

                # --- turn/completed: Capture final result ---
                elif method == "turn/completed":
                    if isinstance(notification.payload, TurnCompletedNotification):
                        turn_result = notification.payload.turn
                        break
        finally:
            self._active_turn_handle = None
            with contextlib.suppress(Exception):
                await self._run_async(stream.close)

        if turn_result is None:
            raise RuntimeError("Turn did not complete (no turn/completed notification received)")

        # Emit TurnCompleteEvent with final metrics
        temp_token_usage = self._token_usage_from_sdk(latest_token_usage)

        duration_s = (turn_result.duration_ms or 0) / 1000.0
        safe_emit(
            stream_callback,
            TurnCompleteEvent(
                task_id=task_id,
                iteration=self._iteration,
                duration_s=duration_s,
                command_count=len(commands),
                token_usage_str=format_token_usage(temp_token_usage),
            ),
        )

        return turn_result, latest_token_usage, "".join(agent_message_chunks)

    def _extract_command_telemetry(self, command_item: Any, sequence: int) -> CommandTelemetry | None:
        """Extract CommandTelemetry from a CommandExecutionThreadItem.

        Maps Codex command execution details to the CommandTelemetry format used by Claude Code.
        """

        try:
            # Extract basic info
            command = getattr(command_item, "command", "")
            command_id = getattr(command_item, "id", f"cmd_{sequence}")
            duration_ms = getattr(command_item, "duration_ms", None)
            exit_code = getattr(command_item, "exit_code", None)
            output = getattr(command_item, "aggregated_output", None)

            # Determine result status from exit code
            result_status = "success" if exit_code == 0 else "error" if exit_code is not None else "unknown"

            # Build result summary with output if available
            summary_parts = [f"Exit code: {exit_code}" if exit_code is not None else "Command executed"]
            if output and len(output.strip()) > 0:
                summary_parts.append(f"Output: {output[:100]}")
            result_summary = " | ".join(summary_parts)

            # Try to parse output as JSON
            result_data = None
            if output:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    result_data = json.loads(output)

            # Build parameters from command string
            parameters = {"command": command}

            return CommandTelemetry(
                tool_name="Bash",
                tool_id=command_id,
                timestamp=datetime.now(),
                duration_ms=float(duration_ms) if duration_ms is not None else None,
                parameters=parameters,
                result_status=result_status,
                result_summary=result_summary,
                error_message=None if exit_code == 0 else output or f"Exit code {exit_code}",
                result_data=result_data,
                sequence_number=sequence,
            )
        except Exception as e:
            self._log.debug(f"Failed to extract command telemetry: {e}")
            return None

    def _extract_file_change_telemetry(
        self, change_id: str, changes: Any, status: Any, sequence: int
    ) -> CommandTelemetry | None:
        """Build CommandTelemetry for a Codex fileChange item.

        Recorded as a ``Write`` tool call so cross-agent criteria that count or
        match file edits (``command_executed``, ``commands_efficiency``) see the
        same signal they get from Claude's Write/Edit tool calls.
        """
        try:
            paths = [str(c.path) for c in changes if hasattr(c, "path")] if changes else []
            return CommandTelemetry(
                tool_name="Write",
                tool_id=change_id,
                timestamp=datetime.now(),
                duration_ms=None,
                parameters={"paths": paths},
                result_status="success" if status != "error" else "error",
                result_summary=f"{len(paths)} file(s) changed",
                error_message=None if status != "error" else "File change failed",
                result_data=None,
                sequence_number=sequence,
            )
        except Exception as e:
            self._log.debug(f"Failed to extract file-change telemetry: {e}")
            return None

    @staticmethod
    def _token_usage_from_sdk(sdk_token_usage: Any) -> TokenUsage | None:
        """Convert the Codex SDK's ThreadTokenUsage to our TokenUsage.

        Single conversion site for both the TurnRecord and the TurnCompleteEvent,
        so cached-input tokens can't be captured in one path but dropped in the
        other.
        """
        if not sdk_token_usage:
            return None
        total = getattr(sdk_token_usage, "total", None)
        if not total:
            return None
        return TokenUsage(
            input_tokens=getattr(total, "input_tokens", 0) or 0,
            output_tokens=getattr(total, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(total, "cached_input_tokens", 0) or 0,
        )

    @staticmethod
    async def _run_async(func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a potentially blocking or async function."""
        result = func(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result
