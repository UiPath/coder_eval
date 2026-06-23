"""Codex agent implementation using the official OpenAI Codex SDK."""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._logging import PrefixedAdapter, log_raw_sdk_event
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import ThreadedWatchdog
from coder_eval.config import settings
from coder_eval.errors import (
    AgentCrashError,
    TurnTimeoutError,
    format_timeout_reason,
    truncate_crash_message,
)
from coder_eval.models import (
    AgentKind,
    ApiRoute,
    AssistantMessage,
    CodexAgentConfig,
    CommandTelemetry,
    ContentBlock,
    DirectRoute,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
)
from coder_eval.proxy.pricing import calculate_cost
from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.utils import expand_env_vars


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

# Approval mode — the SAME for every permission mode (no per-mode mapping).
#
# The Codex SDK exposes exactly two approval modes:
#   - auto_review → AskForApproval.on_request + a SERVER-SIDE ApprovalsReviewer.
#     The reviewer (an extra app-server/gateway decision) adjudicates each
#     apply_patch/shell escalation. Under gateway load it can spuriously return
#     "declined" — files silently not written. Claude has no analog: its
#     Write/Edit permissions are decided CLIENT-SIDE with no model/reviewer in
#     the loop, so it never hits this failure mode.
#   - deny_all → AskForApproval.never + NO reviewer. Despite the name, this is
#     the "run autonomously, never prompt, no reviewer" mode: in-sandbox
#     operations (apply_patch within cwd, shell) execute directly; only
#     escalations BEYOND the sandbox are refused.
#
# An eval harness never wants a reviewer that can flake, so EVERY permission mode
# uses deny_all. The trust boundary is the sandbox (_PERMISSION_MODE_TO_SANDBOX),
# which DOES vary by mode: `plan` stays read-only, `bypassPermissions` is
# full-access (intended for isolated Docker runs).
_CODEX_APPROVAL_MODE = "deny_all"

# Provider id registered in thread config when CODEX_BASE_URL routes to a
# custom endpoint.
_CUSTOM_PROVIDER_ID = "custom"

# Codex apply_patch (Write/Edit) statuses that mean the patch did not apply.
# PatchApplyStatus enum values are inProgress/completed/failed/declined — never
# the literal "error" the code used to compare against, so a failed patch was
# silently recorded as a successful Write. We use this only to classify the
# Write TELEMETRY honestly (failed → error). We do NOT fail or retry the turn on
# it: "declined" can only come from an approval reviewer, and every permission
# mode uses deny_all (no reviewer; see _CODEX_APPROVAL_MODE), so
# in-sandbox apply_patch is applied directly and "declined" should not occur;
# "failed" (diff context mismatch) is self-healed by the model within the turn,
# and grading checks the actual files regardless.
_FILE_CHANGE_FAILURE_STATUSES = frozenset({"failed", "declined"})

# Codex thread-item types that carry transcript CONTENT or session metadata
# rather than a tool call. Everything else streamed as item/started+item/completed
# is treated as a tool call (see _run_turn_with_streaming), so new Codex tool
# kinds are captured automatically instead of being silently dropped — the old
# code hard-coded only commandExecution/fileChange.
#   - reasoning / agentMessage  -> assistant transcript blocks (handled inline)
#   - userMessage               -> prompt echo (skipped)
#   - contextCompaction / entered|exitedReviewMode / hookPrompt / plan ->
#     session lifecycle + planning items, not agent tool calls.
_CONTENT_ITEM_TYPES = frozenset(
    {
        "reasoning",
        "agentMessage",
        "userMessage",
        "contextCompaction",
        "enteredReviewMode",
        "exitedReviewMode",
        "hookPrompt",
        "plan",
    }
)

# Friendly tool-name labels for known Codex tool item types. Unknown tool types
# fall back to the raw item type (so they still surface, just un-prettied).
_TOOL_ITEM_NAMES: dict[str, str] = {
    "commandExecution": "Bash",
    "fileChange": "Write",
    "collabAgentToolCall": "Agent",
    "mcpToolCall": "Mcp",
    "dynamicToolCall": "Tool",
    "webSearch": "WebSearch",
    "imageGeneration": "ImageGeneration",
    "imageView": "ImageView",
}

# collabAgentToolCall.tool value that spawns a NEW sub-agent (vs "wait"/messaging
# operations that act on an already-spawned agent). Only spawns register a new
# child thread to recover. Lowercased to match _status_value, which normalizes
# the SDK's "spawnAgent" enum value to lowercase.
_COLLAB_SPAWN_TOOL = "spawnagent"

# Friendly tool-name labels for the raw ResponseItem function-call names found in
# a sub-agent's on-disk rollout (see _recover_subagent_tool_calls). The child
# thread persists `function_call`/`local_shell_call` ResponseItems unconditionally
# even though its `commandExecution` events are dropped under Limited persistence,
# so the rollout is the only place the sub-agent's inner tool calls survive.
_ROLLOUT_FN_NAMES: dict[str, str] = {
    "exec_command": "Bash",
    "shell": "Bash",
    "local_shell": "Bash",
    "apply_patch": "Write",
    "spawn_agent": "Agent",
    "wait_agent": "Agent",
}

# Rollout ResponseItem payload types that are sub-agent tool CALLS and their
# matching OUTPUT type (keyed by `call_id`). Anything not listed is skipped.
_ROLLOUT_TOOL_CALL_TYPES = frozenset({"function_call", "local_shell_call", "custom_tool_call"})
_ROLLOUT_TOOL_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})

# Sentinel returned by ``next(stream_iter, _STREAM_DONE)`` at stream end. We pass
# a default rather than catching StopIteration because a StopIteration raised
# inside ``asyncio.to_thread`` is converted by asyncio into a TypeError
# ("StopIteration interacts badly with generators…") that escapes ``except
# StopIteration`` — masking the real turn-failure reason on the stream-end path.
_STREAM_DONE = object()


def _ms_to_dt(ms: int | None) -> datetime:
    """Convert a Codex Unix-millisecond timestamp to a datetime (now() if absent)."""
    if ms is None:
        return datetime.now()
    return datetime.fromtimestamp(ms / 1000)


def _status_value(status: Any) -> str:
    """Normalize a Codex status (enum or str) to its lowercase string value."""
    value = getattr(status, "value", status)
    return str(value).lower() if value is not None else ""


def _fresh_input_tokens(raw_input: int, cached: int) -> int:
    """The fresh (uncached) prompt slice = tokens written to cache this call.

    Single definition of the OpenAI cache-write convention, shared by the
    per-message (`_flush_message`) and per-turn (`_token_usage_from_sdk`) paths so
    they can't drift if the billing model ever changes.
    """
    return max(raw_input - cached, 0)


def _message_uncached_input(m: AssistantMessage) -> int:
    """A captured generation's fresh (uncached) input.

    Single definition of the per-message convention, shared by the cost and the
    fold-up paths. Codex children carry 0 ``cache_creation`` (no cache-write fee),
    but fold both defensively so nothing is dropped if that ever changes.
    """
    return m.input_tokens + m.cache_creation_tokens


# Wire protocol for the custom model provider. The pinned codex binary only
# supports the Responses API (it rejects `wire_api = "chat"` as "no longer
# supported"), so this is a fixed constant, not an operator knob.
_CODEX_WIRE_API = "responses"


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
        # _state / _iteration / _iteration_was_incremented / pending_turn lifecycle
        # bookkeeping lives on the Agent base class (shared defaults + helpers).
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})
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

            # Initialize the Codex client (context manager compatible). Close any
            # prior client first: start() is driven through execute_with_retry, so
            # a retried start would otherwise orphan the previous app-server
            # subprocess + reader threads (reaped only at final cleanup).
            self._close_client()
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

    async def communicate(  # noqa: PLR0915 — god-function tracked for decomposition (code-review 2026-06-22)
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

        # Reset the pending slot + bump the iteration counter (shared lifecycle).
        self._begin_turn()

        turn_start_time = time.monotonic()
        timeout_hit = False

        commands: list[CommandTelemetry] = []
        messages: list[TranscriptMessage] = []
        streamed_text = ""
        sdk_token_usage: Any = None
        turn_result: Any = None

        # Event emission: the agent is the SOLE emitter; events fan out to an
        # internal EventCollector (which assembles the TurnRecord — the single,
        # agent-agnostic capture path) and the caller's stream_callback.
        task_id = str(self.config.type)  # str() so a plugin subclass with a non-enum kind also works
        collector = EventCollector()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])

        # Codex has no per-API-call boundary: one thread.turn() == one turn_id.
        turn_id = f"codex-{self._iteration}"
        finalized = False

        def _finalize(status: AgentEndStatus, *, crashed: bool, crash_reason: str | None) -> None:
            nonlocal finalized
            if finalized:
                return
            finalized = True

            # Prefer the SDK total. On a crash/timeout the stream raises before it
            # returns its usage tuple (sdk_token_usage stays None), so fall back to
            # the per-generation tokens already captured on the messages — otherwise
            # a crashed turn under-reports tokens/cost it actually spent.
            token_usage = self._token_usage_from_sdk(sdk_token_usage) or self._token_usage_from_messages(messages)
            # Codex bills sub-agents on separate threads, so the parent total
            # excludes them. Fold the recovered child generations (the
            # ``parent_tool_use_id`` messages, carrying their real per-generation
            # tokens) into the turn total — matching Claude, whose total already
            # includes its bubbled-up sub-agent messages.
            token_usage = self._fold_subagent_tokens(token_usage, messages)

            # AgentEndStatus and TurnEndStatus share identical members; map by value
            # (no duplicated dict, no KeyError if a member is ever added).
            emit.on_event(
                TurnEndEvent(
                    task_id=task_id,
                    turn_id=turn_id,
                    status=TurnEndStatus(status.value),
                    tokens=token_usage,
                )
            )

            model_used = getattr(turn_result, "model", None) or self.config.model
            usage = token_usage or TokenUsage()
            # Real assistant text arrives as agentMessage deltas; fall back to the
            # raw Turn dump only when nothing streamed.
            agent_output = streamed_text or (self._format_turn_result(turn_result) if turn_result is not None else "")

            emit.on_event(
                AgentEndEvent(
                    task_id=task_id,
                    status=status,
                    usage=usage,
                    iteration=self._iteration,
                    user_input=user_input,
                    agent_output=agent_output,
                    model_used=model_used,
                    assistant_turn_count=1,
                    messages=messages,
                    num_turns=1,
                    crashed=crashed,
                    crash_reason=crash_reason,
                    duration_seconds=time.monotonic() - turn_start_time,
                )
            )

            if crashed:
                try:
                    self.pending_turn = collector.build_turn_record()
                except Exception:
                    logger.exception("Failed to build partial turn record")
                    self.pending_turn = None

        try:
            emit.on_event(
                AgentStartEvent(
                    task_id=task_id,
                    prompt=user_input,
                    iteration=self._iteration,
                    model=self._effective_model(),
                )
            )

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
                emit.on_event(TurnStartEvent(task_id=task_id, turn_id=turn_id, model=self._effective_model()))

                try:
                    turn_result, sdk_token_usage, streamed_text = await self._run_turn_with_streaming(
                        user_input, emit, task_id, turn_id, commands, messages
                    )
                except asyncio.CancelledError:
                    if timeout_hit:
                        self._state = AgentState.ERROR
                        _finalize(
                            AgentEndStatus.TIMEOUT, crashed=True, crash_reason=format_timeout_reason(timeout or 0)
                        )
                        raise TurnTimeoutError(timeout or 0, iteration=self._iteration) from None
                    raise
                except Exception as e:
                    if timeout_hit:
                        self._state = AgentState.ERROR
                        _finalize(
                            AgentEndStatus.TIMEOUT, crashed=True, crash_reason=format_timeout_reason(timeout or 0)
                        )
                        raise TurnTimeoutError(timeout or 0, iteration=self._iteration) from e
                    self._state = AgentState.ERROR
                    message = truncate_crash_message(f"Codex turn failed: {e!s}")
                    _finalize(AgentEndStatus.CRASHED, crashed=True, crash_reason=message)
                    raise AgentCrashError(message) from e

            if timeout_hit:
                assert timeout is not None
                _finalize(AgentEndStatus.TIMEOUT, crashed=True, crash_reason=format_timeout_reason(timeout))
                raise TurnTimeoutError(timeout, iteration=self._iteration)
        except (AgentCrashError, TurnTimeoutError):
            # Already funneled through _finalize by the inner handlers.
            raise
        except asyncio.CancelledError:
            # Non-timeout cancel (external cancellation, or a cancel during
            # thread_start before the watchdog block). The timeout path already
            # finalized above; otherwise close the AgentStart so the event tree
            # stays balanced and the pending-turn contract holds. _finalize is
            # idempotent, so the timeout case is a no-op here.
            if not finalized:
                self._state = AgentState.ERROR
                _finalize(AgentEndStatus.CRASHED, crashed=True, crash_reason="turn cancelled")
            raise
        except Exception as e:
            # Catches failures OUTSIDE the inner turn block — notably thread_start
            # and _format_turn_result. Without this, such errors escape as a bare
            # exception: the orchestrator never drains pending_turn and _iteration
            # stays incremented, violating the pending-turn contract.
            self._state = AgentState.ERROR
            message = truncate_crash_message(f"Codex turn failed: {e!s}")
            _finalize(AgentEndStatus.CRASHED, crashed=True, crash_reason=message)
            raise AgentCrashError(message) from e

        self._state = AgentState.WORKING
        self._end_turn_ok()

        # The TurnRecord is the EventCollector's reduction of the emitted events.
        _finalize(AgentEndStatus.COMPLETED, crashed=False, crash_reason=None)
        return collector.build_turn_record()

    async def stop(self) -> None:
        """Stop the agent and tear down the Codex SDK session.

        ``Codex(config=...)`` eagerly spawns an app-server subprocess plus reader
        threads; ``close()`` reaps them. Skipping it leaks a subprocess + threads
        per task across a batch run, so close before nulling the reference.
        """
        self._close_client()
        self.thread = None
        self._active_turn_handle = None
        self._mark_stopped()

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

    def get_environment_info(self) -> dict[str, Any]:
        """Record the resolved Codex routing so runs are auditable/comparable.

        Only emits when a custom endpoint is configured (CODEX_BASE_URL). On a
        custom endpoint the model is an operator-chosen alias (a deployment name
        on Azure), so two operators' ``gpt-5-codex`` deployments are otherwise
        indistinguishable in run artifacts. The host (not the full URL) is
        recorded to avoid leaking any embedded credentials; the API key is never
        recorded.
        """
        base_url = self._resolve_base_url()
        if not base_url:
            return {}
        return {
            "codex_base_url_host": urlparse(base_url).hostname or "",
            "codex_wire_api": _CODEX_WIRE_API,
            "codex_api_version": self._resolve_api_version() or "",
            "codex_model_is_deployment": True,
        }

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
                        expanded_path = expand_env_vars(path_str)
                        plugin_path = Path(expanded_path)
                        if plugin_path.exists() and plugin_path.is_dir():
                            skills_sources.append(plugin_path)
                            self._log.debug(f"Found skills from plugin: {plugin_path}")
                        else:
                            # Loud: an unresolved env var (e.g. unset
                            # $SKILLS_REPO_PATH) or missing dir silently drops
                            # the plugin's skills, so the agent runs blind.
                            hint = "env var likely unset" if "$" in expanded_path else "path does not exist"
                            self._log.warning(
                                f"Plugin skills path did not resolve: {path_str!r} "
                                + f"→ {expanded_path!r} ({hint}); no skills linked from it"
                            )

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

            # Symlink or copy skills from all sources. A source may either
            # contain skill dirs directly (<source>/<skill>/SKILL.md) or be a
            # Claude plugin-marketplace root whose skills live one level deeper
            # (<source>/skills/<skill>/SKILL.md). Scan both layouts.
            for skills_source in skills_sources:
                scan_dirs = [skills_source]
                nested = skills_source / "skills"
                if nested.is_dir():
                    scan_dirs.append(nested)

                for scan_dir in scan_dirs:
                    for skill_dir in scan_dir.iterdir():
                        if not (skill_dir.is_dir() and (skill_dir / "SKILL.md").exists()):
                            continue
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

            linked = list(agents_skills_dir.iterdir())
            if linked:
                self._log.debug(f"Linked {len(linked)} skill(s) into {agents_skills_dir}")
            else:
                # Sources existed but no SKILL.md was found under them or their
                # skills/ subdir — codex will run without any skill context.
                self._log.warning(
                    f"0 skills linked into {agents_skills_dir} despite "
                    + f"{len(skills_sources)} plugin source(s): "
                    + f"{[str(s) for s in skills_sources]}; "
                    + "check the plugin path points at a skills repo root"
                )

        except Exception as e:
            self._log.warning(f"Failed to set up skills: {e}")

    @staticmethod
    def _resolve_base_url() -> str | None:
        """Custom Codex endpoint base URL from CODEX_BASE_URL, or None."""
        return os.getenv("CODEX_BASE_URL") or None

    @staticmethod
    def _resolve_api_version() -> str | None:
        """Azure OpenAI ``api-version`` from CODEX_API_VERSION, or None.

        Azure's Responses endpoint requires an ``api-version`` query parameter on
        every request; when set it is injected as the custom provider's
        ``query_params``. Plain OpenAI / gateway endpoints leave this unset.
        """
        return os.getenv("CODEX_API_VERSION") or None

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
        approval_mode_str = _CODEX_APPROVAL_MODE

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

        # Codex's workspace-write sandbox disables network by default, so any
        # tool the agent needs to install (npm/pip/etc.) fails with
        # "fetch failed". Always open network in workspace-write — every task
        # we exercise needs the UiPath CLI / package installs. Read-only and
        # danger-full-access keep their built-in network defaults.
        if sandbox_mode_str == "workspace-write":
            tool_config["sandbox_workspace_write"] = {"network_access": True}

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
        # gateway, or Azure OpenAI) when CODEX_BASE_URL is set. The codex binary
        # has no base-URL env var — a model provider must be defined in config and
        # selected, with env_key naming the env var that holds the key
        # (CODEX_API_KEY). For Azure, CODEX_API_VERSION adds the required
        # ``api-version`` query param and CODEX_MODEL is the deployment name.
        base_url = self._resolve_base_url()
        if base_url:
            options["model_provider"] = _CUSTOM_PROVIDER_ID
            if not effective_model:
                self._log.warning(
                    "CODEX_BASE_URL is set but no model resolved (agent.model / CODEX_MODEL) "
                    + "— the provider may reject the request."
                )
            provider: dict[str, Any] = {
                "name": "Custom",
                "base_url": base_url,
                "env_key": "CODEX_API_KEY",
                # The pinned codex binary only supports the Responses wire API
                # (it rejects `wire_api = "chat"` as "no longer supported"), so
                # this is fixed rather than configurable.
                "wire_api": _CODEX_WIRE_API,
            }
            api_version = self._resolve_api_version()
            if api_version:
                # Azure requires ?api-version=… on every request; the codex binary
                # appends these to the provider's request URL.
                provider["query_params"] = {"api-version": api_version}
            tool_config["model_providers"] = {_CUSTOM_PROVIDER_ID: provider}
            self._log.debug(
                f"Codex routed via custom provider (host={urlparse(base_url).hostname or '(unknown)'}, "
                + f"api_version={'set' if api_version else 'unset'})"
            )

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
        """Format a Codex Turn to a readable string — fallback when no text streamed.

        The Turn payload has no ``final_response`` field; assistant text arrives as
        agentMessage deltas during streaming. This only fires when streaming produced
        nothing, dumping the raw Turn for debugging.
        """
        try:
            result_dict = turn_result.model_dump() if hasattr(turn_result, "model_dump") else vars(turn_result)
            return json.dumps(result_dict, indent=2, default=str)
        except Exception as e:
            self._log.warning(f"Failed to format turn result: {e}")
            return str(turn_result)

    async def _run_turn_with_streaming(  # noqa: PLR0912, PLR0915 — god-function tracked for decomposition (code-review 2026-06-22)
        self,
        user_input: str,
        emit: CompositeStreamCallback,
        task_id: str,
        turn_id: str,
        commands: list[CommandTelemetry],
        messages: list[TranscriptMessage],
    ) -> tuple[Any, Any, str]:
        """Run a turn with streaming support, emitting the standard event protocol.

        Uses turn.stream() to get notifications and emits ToolStartEvent,
        ToolEndEvent, and TextChunkEvent (the enclosing communicate() owns the
        TurnStart/TurnEnd/AgentEnd boundaries). EVERY tool-bearing item kind is
        captured generically (see _CONTENT_ITEM_TYPES) — command/file-change keep
        rich extractors, the rest route through _extract_generic_telemetry — so
        MCP, web-search and collab-agent calls aren't dropped.

        Also reconstructs the assistant transcript into ``messages`` (mutated in
        place, like ``commands``, so a mid-turn crash keeps the partial transcript):
        ``reasoning`` items → thinking blocks, every tool item → tool_use blocks
        (id == the CommandTelemetry ``tool_id``, so the evalboard joins them), and
        ``agentMessage`` items → text blocks. Codex collab-agent spawns also append
        a nested child AssistantMessage (``parent_tool_use_id``) carrying the
        sub-agent's returned result. Each ``agentMessage`` closes one
        AssistantMessage; any trailing tool/reasoning blocks flush at stream end.

        Returns:
            Tuple of (turn_result, latest_token_usage, agent_text) where turn_result
            is the final Turn object, latest_token_usage is the SDK's ThreadTokenUsage
            (or None), and agent_text is the assistant message assembled from the
            streamed agentMessage deltas.
        """
        from openai_codex.generated.v2_all import TurnCompletedNotification

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
        # Maps a spawned sub-agent's child thread id -> the spawning Agent call's
        # tool_use_id, so the child's returned result can be nested under THAT
        # call in the transcript (parent_tool_use_id) — which is what makes the
        # Agent row expandable in the evalboard.
        collab_spawn_by_thread: dict[str, str] = {}
        # Every spawned sub-agent: (child_thread_id, spawning Agent tool_use_id,
        # spawned model). Unlike collab_spawn_by_thread (popped once the result
        # nests), this is the durable record used AFTER the turn to recover each
        # child's inner tool calls AND per-generation token usage from its on-disk
        # rollout (the parent stream carries neither), reconstructed as nested
        # ``parent_tool_use_id`` messages.
        spawned_children: list[tuple[str, str, str | None]] = []
        # child thread id -> the message it returned to the parent (from the wait
        # collab call). Only used as a FALLBACK when the child's rollout can't be
        # found: then we nest just this returned text. When the rollout IS found,
        # the child's own final generation already carries it.
        collab_results: dict[str, str] = {}

        # --- Assistant-transcript reconstruction state ---
        # One AssistantMessage per model generation. Codex emits exactly one
        # ``thread/tokenUsage/updated`` per generation (carrying that response's
        # ``last`` delta) AFTER the generation's items, so it is the reliable
        # generation boundary AND the per-message token source. Tool-call items
        # start (item/started) before that tokenUsage but may complete after it,
        # so tool_use blocks are recorded at item/started — held by reference in
        # ``blocks_by_id`` so a later item/completed can patch ``is_error`` even
        # after the message has been flushed.
        open_blocks: list[ContentBlock] = []
        open_start_ms: int | None = None
        open_end_ms: int | None = None
        start_ms_by_id: dict[str, int] = {}
        blocks_by_id: dict[str, ContentBlock] = {}
        # Tools that emitted item/started but not yet item/completed, keyed by
        # tool_id → the start-time CommandTelemetry (carries the name we know from
        # the item type). Popped at item/completed. Whatever remains at turn end is
        # an ORPHAN — a tool the stream never closed (e.g. a Codex collab op that
        # starts without a discrete completion) — and is force-closed as
        # ``unresolved`` in ``finally`` so its transcript block keeps a real tool
        # name + countable telemetry instead of rendering as "unknown".
        open_tools: dict[str, CommandTelemetry] = {}
        # Thinking blocks for reasoning items that arrived WITHOUT visible text
        # (OpenAI hides raw chain-of-thought; summaries are off). Resolved at flush
        # once the generation's reasoning-token count is known: filled with a
        # policy placeholder, or dropped if no reasoning was billed.
        reasoning_placeholders: list[ContentBlock] = []
        gen_index = 0  # one per generation; sub-messages of a gen share its message_id
        # Prompt size (input_tokens) of the previous generation. Used to tell a
        # genuine cache WRITE (the prompt grew → new prefix cached) apart from a
        # cache MISS (the prompt didn't grow but fewer tokens hit cache — a re-send
        # of previously-cached content, e.g. after the spawn/wait pause). Only the
        # growth is bucketed as cache_creation; the rest of the fresh slice is
        # plain input_tokens. Same cost (write rate == input rate), truthful label.
        prev_prompt_tokens = 0

        def _record_block(block: ContentBlock, item_id: str, completed_ms: int | None) -> None:
            open_blocks.append(block)
            nonlocal open_start_ms, open_end_ms
            start_ms = start_ms_by_id.get(item_id)
            if start_ms is not None and (open_start_ms is None or start_ms < open_start_ms):
                open_start_ms = start_ms
            if completed_ms is not None and (open_end_ms is None or completed_ms > open_end_ms):
                open_end_ms = completed_ms

        def _flush_message(last: Any) -> None:
            """Cut the open buffer into AssistantMessage(s) for one generation.

            ``last`` is the SDK ``TokenUsageBreakdown`` for the generation that
            produced these blocks (or None for a safety flush with no usage).

            Emits ONE sub-message per block kind (thinking vs tool/text), all
            sharing this generation's ``message_id`` — mirroring how the Claude
            agent emits per-block-kind so the evalboard attributes output tokens
            per block (reasoning output → the thinking row, the rest → the tool/
            text row). Reasoning output tokens ARE output tokens (a subset), so
            the thinking row's usage shows them as output.
            """
            nonlocal open_blocks, open_start_ms, open_end_ms, reasoning_placeholders, gen_index
            nonlocal prev_prompt_tokens
            if not open_blocks:
                reasoning_placeholders = []
                return
            # Per-generation tokens from the matching tokenUsage `last` delta;
            # these sum to the turn `total`, so summing messages won't double-count.
            cached = (getattr(last, "cached_input_tokens", 0) or 0) if last else 0
            raw_input = (getattr(last, "input_tokens", 0) or 0) if last else 0
            total_output = (getattr(last, "output_tokens", 0) or 0) if last else 0
            # The fresh (uncached) prompt slice is plain input — OpenAI bills no
            # separate cache-write fee, so Codex carries 0 cache_creation. (Cost is
            # unchanged; the cache-write rate == input rate for OpenAI.)
            gen_input = _fresh_input_tokens(raw_input, cached)
            gen_cache_write = 0
            prev_prompt_tokens = max(prev_prompt_tokens, raw_input)
            reasoning_tok = (getattr(last, "reasoning_output_tokens", 0) or 0) if last else 0
            # Resolve text-less reasoning blocks: OpenAI hides the raw CoT, so show
            # a policy placeholder when reasoning was billed, else drop the block.
            if reasoning_placeholders:
                if reasoning_tok > 0:
                    for blk in reasoning_placeholders:
                        blk.thinking = "_Reasoning hidden by OpenAI policy_"
                else:
                    for blk in reasoning_placeholders:
                        if blk in open_blocks:
                            open_blocks.remove(blk)
                reasoning_placeholders = []
            if not open_blocks:
                open_start_ms = open_end_ms = None
                return

            thinking_blocks = [b for b in open_blocks if b.block_type == "thinking"]
            action_blocks = [b for b in open_blocks if b.block_type != "thinking"]
            started = _ms_to_dt(open_start_ms)
            completed = _ms_to_dt(open_end_ms if open_end_ms is not None else open_start_ms)
            gen_ms = max((completed - started).total_seconds() * 1000.0, 0.0)
            message_id = f"{turn_id}-msg-{gen_index}"

            # Output split: reasoning portion to the thinking row, the remainder to
            # the action row. With only one kind present, that kind gets all output.
            think_out = reasoning_tok if action_blocks else total_output
            action_out = max(total_output - reasoning_tok, 0) if thinking_blocks else total_output

            # Sub-message specs in generation order (thinking first). The FIRST one
            # carries the generation's input/cache + gen-time; the rest carry 0 so
            # the evalboard's per-message_id sums aren't double-counted.
            specs: list[tuple[list[ContentBlock], int, int]] = []
            if thinking_blocks:
                specs.append((thinking_blocks, think_out, reasoning_tok))
            if action_blocks:
                specs.append((action_blocks, action_out, 0))

            for idx, (blocks, out_tok, reas_tok) in enumerate(specs):
                for i, blk in enumerate(blocks):
                    blk.sequence = i
                first = idx == 0
                messages.append(
                    AssistantMessage(
                        started_at=started,
                        completed_at=completed,
                        generation_duration_ms=gen_ms if first else 0.0,
                        content_blocks=blocks,
                        tool_use_ids=[b.tool_use_id for b in blocks if b.block_type == "tool_use" and b.tool_use_id],
                        input_tokens=gen_input if first else 0,
                        output_tokens=out_tok,
                        cache_creation_tokens=gen_cache_write if first else 0,
                        cache_read_tokens=cached if first else 0,
                        reasoning_tokens=reas_tok,
                        model=self._effective_model(),
                        message_id=message_id,
                    )
                )
            gen_index += 1
            open_blocks = []
            open_start_ms = None
            open_end_ms = None

        stream_iter = iter(stream)
        try:
            while True:
                # Offload the blocking SDK iteration to a worker thread so the
                # event loop stays free (parallel agents don't serialize) and the
                # watchdog's task.cancel() can actually land at this await point.
                notification: Any = await asyncio.to_thread(next, stream_iter, _STREAM_DONE)
                if notification is _STREAM_DONE:
                    break

                root = _get_item_root(notification)
                method = notification.method
                log_raw_sdk_event(
                    self._log,
                    repr_target=notification,
                    attr_target=root,
                    method=method,
                    root_type=getattr(root, "type", None),
                )

                # --- item/started: Emit ToolStartEvent for every tool-like item ---
                if method == "item/started":
                    root = _get_item_root(notification)
                    if root is not None:
                        # Record the start time for every item kind so flushed
                        # AssistantMessages get real per-message generation timing.
                        item_id = getattr(root, "id", None)
                        started_at_ms = getattr(notification.payload, "started_at_ms", None)
                        if item_id is not None and started_at_ms is not None:
                            start_ms_by_id[item_id] = started_at_ms
                        root_type = getattr(root, "type", None)
                        # Any item that isn't transcript content is a tool call.
                        # Generic handling (vs the old commandExecution/fileChange
                        # allowlist) captures MCP, web-search, collab-agent and any
                        # future Codex tool kind automatically instead of dropping it.
                        if root_type is not None and root_type not in _CONTENT_ITEM_TYPES:
                            tool_id = item_id or f"{root_type}_{next_sequence}"
                            seq_by_id[tool_id] = next_sequence
                            start_tel = CommandTelemetry(
                                tool_name=self._tool_name(root_type),
                                tool_id=tool_id,
                                timestamp=datetime.now(),
                                parameters=self._tool_parameters(root, root_type),
                                sequence_number=next_sequence,
                            )
                            # Remember it until item/completed; whatever is left
                            # open at turn end is force-closed as unresolved.
                            open_tools[tool_id] = start_tel
                            emit.on_event(ToolStartEvent(task_id=task_id, turn_id=turn_id, tool=start_tel))
                            next_sequence += 1
                            # Record the tool_use block now (item/started precedes
                            # this generation's tokenUsage flush); is_error patched
                            # at item/completed, even after the message is flushed.
                            block = ContentBlock(block_type="tool_use", sequence=0, tool_use_id=tool_id)
                            blocks_by_id[tool_id] = block
                            _record_block(block, tool_id, None)

                # --- item/completed: Emit ToolEndEvent + capture telemetry/sub-agents ---
                elif method == "item/completed":
                    root = _get_item_root(notification)
                    if root is not None:
                        completed_ms = getattr(notification.payload, "completed_at_ms", None)
                        root_type = getattr(root, "type", None)
                        if root_type is not None and root_type not in _CONTENT_ITEM_TYPES:
                            tool_id = getattr(root, "id", None) or f"{root_type}_{next_sequence}"
                            seq = seq_by_id.get(tool_id, next_sequence)
                            # This tool is now resolved — drop it from the orphan set.
                            open_tools.pop(tool_id, None)

                            # commandExecution/fileChange keep their rich extractors;
                            # every other tool kind gets generic telemetry so it still
                            # counts toward command_executed / commands_efficiency and
                            # renders in the transcript.
                            telemetry, is_error = self._telemetry_for_item(root, root_type, tool_id, seq)
                            if telemetry:
                                commands.append(telemetry)
                            emit.on_event(
                                ToolEndEvent(
                                    task_id=task_id,
                                    turn_id=turn_id,
                                    tool=telemetry
                                    or CommandTelemetry(
                                        tool_name=self._tool_name(root_type),
                                        tool_id=tool_id,
                                        timestamp=datetime.now(),
                                        sequence_number=seq,
                                    ),
                                    status=ToolEndStatus.ERROR if is_error else ToolEndStatus.OK,
                                )
                            )
                            # Patch the block recorded at item/started (held by
                            # reference, so this lands even post-flush) + extend the
                            # still-open message's end time.
                            if tool_id in blocks_by_id:
                                blocks_by_id[tool_id].is_error = is_error
                            if (
                                open_blocks
                                and completed_ms is not None
                                and (open_end_ms is None or completed_ms > open_end_ms)
                            ):
                                open_end_ms = completed_ms

                            # Codex's native multi-agent calls land here as
                            # collabAgentToolCall items — record spawns as
                            # sub-agents and nest each child's returned result
                            # under the spawning Agent call.
                            if root_type == "collabAgentToolCall":
                                self._handle_collab_completion(
                                    root,
                                    tool_id,
                                    collab_spawn_by_thread,
                                    spawned_children,
                                    collab_results,
                                )

                        elif root_type == "reasoning":
                            # Reasoning items: use the summary text when present
                            # (only if summaries are enabled). OpenAI never returns
                            # the raw CoT, so a text-less item becomes a placeholder
                            # block, resolved with its token count at flush.
                            reasoning_id = getattr(root, "id", f"reasoning_{next_sequence}")
                            parts = getattr(root, "content", None) or getattr(root, "summary", None) or []
                            text = "\n".join(p for p in parts if p)
                            block = ContentBlock(block_type="thinking", sequence=0, thinking=text or None)
                            _record_block(block, reasoning_id, completed_ms)
                            if not text:
                                reasoning_placeholders.append(block)

                        elif root_type == "agentMessage":
                            # Full assistant text — append a text block. The
                            # message is cut at the following tokenUsage event (the
                            # generation boundary), not here.
                            message_item_id = getattr(root, "id", f"msg_{next_sequence}")
                            text = getattr(root, "text", "") or ""
                            if text:
                                _record_block(
                                    ContentBlock(block_type="text", sequence=0, text=text),
                                    message_item_id,
                                    completed_ms,
                                )

                # --- item/agentMessage/delta: Emit TextChunkEvent for streaming text ---
                elif method == "item/agentMessage/delta":
                    if notification.payload:
                        delta = getattr(notification.payload, "delta", None)
                        if delta:
                            agent_message_chunks.append(delta)
                            emit.on_event(TextChunkEvent(task_id=task_id, turn_id=turn_id, text=delta))

                # --- thread/tokenUsage/updated: one per generation → cut a message ---
                # Carries `total` (cumulative; the turn-level figure) and `last`
                # (this generation's delta). Fires AFTER the generation's items, so
                # it is the generation boundary: attribute `last` to the open buffer
                # and flush it as one AssistantMessage.
                elif method == "thread/tokenUsage/updated":
                    if notification.payload:
                        latest_token_usage = getattr(notification.payload, "token_usage", None)
                        _flush_message(getattr(latest_token_usage, "last", None))

                # --- turn/completed: Capture final result ---
                elif method == "turn/completed":
                    if isinstance(notification.payload, TurnCompletedNotification):
                        turn_result = notification.payload.turn
                        break
        finally:
            self._active_turn_handle = None
            # Close any tool that started but never completed (an orphan): some
            # Codex collab ops emit item/started without a discrete item/completed,
            # so their transcript block would otherwise have no telemetry and the
            # evalboard would render it "unknown". Emit a ToolEndEvent(unresolved)
            # carrying the name known at item/started, so it keeps a real tool name
            # and counts. Mirrors the crash-path orphan closure in _finalize; runs
            # on every exit (normal completion, crash, timeout).
            for start_tel in sorted(open_tools.values(), key=lambda t: getattr(t, "sequence_number", 0)):
                start_tel.result_status = "unknown"
                emit.on_event(
                    ToolEndEvent(
                        task_id=task_id,
                        turn_id=turn_id,
                        tool=start_tel,
                        status=ToolEndStatus.UNRESOLVED,
                    )
                )
            open_tools.clear()
            # Flush any trailing blocks not closed by a tokenUsage event (e.g. a
            # crash mid-generation) so the partial transcript survives.
            _flush_message(None)
            with contextlib.suppress(Exception):
                await self._run_async(stream.close)

        if turn_result is None:
            raise RuntimeError("Turn did not complete (no turn/completed notification received)")

        # Belt-and-suspenders: if streaming surfaced no assistant transcript,
        # rebuild it from the terminal Turn's ordered item list.
        if not messages:
            messages.extend(self._messages_from_items(getattr(turn_result, "items", None), turn_id))

        # Recover each spawned sub-agent's INNER tool calls from its on-disk
        # rollout and nest them under the spawning Agent call. The parent stream
        # never carries the child's commands (the child thread persists with
        # Limited mode, which drops commandExecution events), but its rollout
        # always persists the raw function_call/local_shell_call ResponseItems.
        if spawned_children:
            await self._recover_subagent_tool_calls(
                spawned_children, collab_results, messages, commands, emit, task_id, turn_id
            )

        return turn_result, latest_token_usage, "".join(agent_message_chunks)

    def _messages_from_items(self, items: Any, turn_id: str) -> list[AssistantMessage]:
        """Rebuild the assistant transcript from a Turn's ``items`` list (fallback).

        Same item→block mapping as the streaming path, but Turn items carry no
        per-item timestamps, so timing falls back to now()/0.0. Used only when the
        stream produced no messages.
        """
        if not items:
            return []

        rebuilt: list[AssistantMessage] = []
        open_blocks: list[ContentBlock] = []

        def _flush() -> None:
            nonlocal open_blocks
            if not open_blocks:
                return
            for i, blk in enumerate(open_blocks):
                blk.sequence = i
            now = datetime.now()
            rebuilt.append(
                AssistantMessage(
                    started_at=now,
                    completed_at=now,
                    generation_duration_ms=0.0,
                    content_blocks=open_blocks,
                    tool_use_ids=[b.tool_use_id for b in open_blocks if b.block_type == "tool_use" and b.tool_use_id],
                    model=self._effective_model(),
                    message_id=f"{turn_id}-msg-{len(rebuilt)}",
                )
            )
            open_blocks = []

        for item in items:
            root = getattr(item, "root", item)
            root_type = getattr(root, "type", None)
            item_id = getattr(root, "id", "")
            if root_type is not None and root_type not in _CONTENT_ITEM_TYPES:
                # Any tool-like item (generic, not just command/fileChange) → a
                # tool_use block, mirroring the streaming path's broad capture.
                status = _status_value(getattr(root, "status", "completed"))
                exit_code = getattr(root, "exit_code", None)
                is_error = (
                    status in _FILE_CHANGE_FAILURE_STATUSES
                    or (exit_code is not None and exit_code != 0)
                    or bool(getattr(root, "error", None))
                    or getattr(root, "success", None) is False
                )
                open_blocks.append(
                    ContentBlock(block_type="tool_use", sequence=0, tool_use_id=item_id, is_error=is_error)
                )
            elif root_type == "reasoning":
                parts = getattr(root, "content", None) or getattr(root, "summary", None) or []
                text = "\n".join(p for p in parts if p)
                if text:
                    open_blocks.append(ContentBlock(block_type="thinking", sequence=0, thinking=text))
            elif root_type == "agentMessage":
                text = getattr(root, "text", "") or ""
                if text:
                    open_blocks.append(ContentBlock(block_type="text", sequence=0, text=text))
                _flush()

        _flush()
        return rebuilt

    @staticmethod
    def _tool_name(root_type: str | None) -> str:
        """Friendly tool label for a Codex item type (falls back to the raw type)."""
        return _TOOL_ITEM_NAMES.get(root_type or "", root_type or "Tool")

    def _tool_parameters(self, root: Any, root_type: str | None) -> dict[str, Any]:
        """Best-effort ToolStartEvent parameters for any Codex tool item.

        Per-kind for the items we understand; an empty dict for unknown tool
        kinds (still emitted, just without parameters).
        """
        if root_type == "commandExecution":
            return {"command": getattr(root, "command", "")}
        if root_type == "fileChange":
            changes = getattr(root, "changes", None) or []
            path = str(changes[0].path) if changes and hasattr(changes[0], "path") else "?"
            return {"path": path}
        if root_type == "collabAgentToolCall":
            params: dict[str, Any] = {"operation": _status_value(getattr(root, "tool", ""))}
            if model := getattr(root, "model", None):
                params["model"] = model
            if prompt := getattr(root, "prompt", None):
                params["prompt"] = prompt
            return params
        if root_type in ("mcpToolCall", "dynamicToolCall"):
            params = {"tool": getattr(root, "tool", "")}
            if server := (getattr(root, "server", None) or getattr(root, "namespace", None)):
                params["server"] = server
            args = getattr(root, "arguments", None)
            if args is not None:
                params["arguments"] = args
            return params
        if root_type == "webSearch":
            return {"query": getattr(root, "query", "")}
        if root_type == "imageView":
            return {"path": str(getattr(root, "path", ""))}
        if root_type == "imageGeneration":
            return {"prompt": getattr(root, "revised_prompt", None) or ""}
        return {}

    def _telemetry_for_item(
        self, root: Any, root_type: str | None, tool_id: str, seq: int
    ) -> tuple[CommandTelemetry | None, bool]:
        """Build (telemetry, is_error) for a completed tool item.

        commandExecution/fileChange keep their dedicated rich extractors; every
        other tool kind routes through the generic builder so it still produces
        countable telemetry.
        """
        if root_type == "commandExecution":
            exit_code = getattr(root, "exit_code", None)
            return self._extract_command_telemetry(root, seq), exit_code != 0
        if root_type == "fileChange":
            changes = getattr(root, "changes", []) or []
            status_str = _status_value(getattr(root, "status", "completed"))
            failed = status_str in _FILE_CHANGE_FAILURE_STATUSES
            return self._extract_file_change_telemetry(tool_id, changes, status_str, seq), failed
        return self._extract_generic_telemetry(root, root_type, tool_id, seq)

    def _extract_generic_telemetry(
        self, root: Any, root_type: str | None, tool_id: str, seq: int
    ) -> tuple[CommandTelemetry | None, bool]:
        """CommandTelemetry for any tool item without a dedicated extractor.

        Reads status / duration / error generically so MCP calls, web searches,
        collab-agent spawns and future tool kinds all render and count uniformly.
        """
        try:
            status_str = _status_value(getattr(root, "status", "") or "")
            err = getattr(root, "error", None)
            success = getattr(root, "success", None)
            is_error = bool(err) or success is False or status_str in _FILE_CHANGE_FAILURE_STATUSES
            duration_ms = getattr(root, "duration_ms", None)
            return (
                CommandTelemetry(
                    tool_name=self._tool_name(root_type),
                    tool_id=tool_id,
                    timestamp=datetime.now(),
                    duration_ms=float(duration_ms) if duration_ms is not None else None,
                    parameters=self._tool_parameters(root, root_type),
                    result_status="error" if is_error else ("success" if status_str else "unknown"),
                    result_summary=self._summarize_tool_item(root, root_type),
                    error_message=str(err) if err else None,
                    sequence_number=seq,
                ),
                is_error,
            )
        except Exception as e:
            self._log.debug(f"Failed to extract generic tool telemetry ({root_type}): {e}")
            return None, False

    @staticmethod
    def _summarize_tool_item(root: Any, root_type: str | None) -> str:
        """One-line human summary of a generic tool item for telemetry."""
        if root_type == "collabAgentToolCall":
            op = _status_value(getattr(root, "tool", ""))
            states = getattr(root, "agents_states", None) or {}
            messages = [s.message for s in states.values() if getattr(s, "message", None)]
            return f"collab {op}: {'; '.join(messages)[:200]}" if messages else f"collab {op}"
        if root_type == "webSearch":
            return f"query: {getattr(root, 'query', '')}"
        if root_type in ("mcpToolCall", "dynamicToolCall"):
            return f"{getattr(root, 'server', '') or getattr(root, 'namespace', '')}:{getattr(root, 'tool', '')}".strip(
                ":"
            )
        return _status_value(getattr(root, "status", "")) or (root_type or "")

    def _handle_collab_completion(
        self,
        root: Any,
        tool_id: str,
        spawn_by_thread: dict[str, str],
        spawned_children: list[tuple[str, str, str | None]],
        collab_results: dict[str, str],
    ) -> None:
        """Process a completed Codex collab-agent call (spawn / wait / message).

        Two responsibilities:

        1. SPAWN (``tool == 'spawnAgent'``): remember which Agent call owns each
           spawned child thread (so the child's result can nest under it) and the
           spawned model. Follow-up ``wait``/messaging calls reuse the same thread
           and are NOT new sub-agents.

           Codex emits NO per-sub-agent token breakdown in the parent stream —
           every ``thread/tokenUsage/updated`` reports only the PARENT thread's
           cumulative usage. The child's real per-generation tokens are recovered
           AFTER the turn from its on-disk rollout and reconstructed as nested
           ``parent_tool_use_id`` messages (see ``_recover_subagent_tool_calls``);
           ``_finalize`` then folds those messages into the turn total.

        2. RESULT: any collab completion may carry the child's returned message in
           ``agents_states[thread].message``. We stash it in ``collab_results`` as
           a FALLBACK — used only if the child's rollout can't be found later. When
           the rollout IS found, ``_recover_subagent_tool_calls`` rebuilds the
           sub-agent's full generation sequence (tool calls + final text) with real
           per-generation tokens, so the returned message is just the last of those.
        """
        tool = _status_value(getattr(root, "tool", ""))
        receivers = getattr(root, "receiver_thread_ids", None) or []
        if tool == _COLLAB_SPAWN_TOOL:
            model = getattr(root, "model", None) or self._effective_model() or None
            for thread_id in receivers:
                spawn_by_thread[thread_id] = tool_id
                spawned_children.append((str(thread_id), tool_id, model))

        states = getattr(root, "agents_states", None) or {}
        for thread_id, state in states.items():
            message = getattr(state, "message", None)
            if message and thread_id in spawn_by_thread:
                collab_results[str(thread_id)] = str(message)

    async def _recover_subagent_tool_calls(
        self,
        spawned_children: list[tuple[str, str, str | None]],
        collab_results: dict[str, str],
        messages: list[TranscriptMessage],
        commands: list[CommandTelemetry],
        emit: StreamCallback,
        task_id: str,
        turn_id: str,
    ) -> None:
        """Recover each spawned sub-agent's INNER tool calls AND token usage.

        Codex runs every sub-agent on its own child thread whose events never
        reach the parent stream, and that child thread persists with *Limited*
        rollout policy — which drops ``commandExecution`` events. So neither the
        live stream nor ``thread.read`` surfaces the sub-agent's shell commands,
        and ``thread/tokenUsage/updated`` only ever reports the PARENT thread, so
        per-child tokens never appear in the live stream.

        But the child rollout ALWAYS persists the raw ``function_call`` /
        ``local_shell_call`` / ``custom_tool_call`` (+ ``*_output``) ResponseItems
        (``should_persist_response_item`` keeps them regardless of mode) AND a
        ``token_count`` event with the child thread's cumulative usage. So we
        locate the child rollout by thread id and:

        - per inner call, emit one ``CommandTelemetry`` (so the tool row resolves)
          plus one nested ``AssistantMessage`` parented to the spawning Agent call
          (so the evalboard renders it as an expandable child), carrying that
          generation's real per-generation tokens. ``_finalize`` folds these
          ``parent_tool_use_id`` messages into the turn total so the run cost
          includes the sub-agent, exactly as Claude's total already includes its
          bubbled-up sub-agent messages.

        Best-effort: any failure (missing file, parse error) is swallowed so a
        recovery hiccup never fails the turn.
        """
        home = self._codex_home()
        for thread_id, parent_tool_id, model in spawned_children:
            try:
                path = await self._await_rollout_file(home, thread_id)
                if path is None:
                    # No rollout to mine: nest just the returned message (if any) so
                    # the sub-agent's answer still shows, tokenless.
                    self._log.debug("CodexAgent: no rollout found for sub-agent thread %s", thread_id)
                    result = collab_results.get(thread_id)
                    if result:
                        messages.append(
                            self._subagent_text_message(result, parent_tool_id, model, turn_id, len(messages))
                        )
                    continue
                gens = self._parse_rollout_generations(path)
                # Rebuild the sub-agent's generations in order — each a nested
                # message parented to the spawn, carrying its real per-generation
                # tokens (fresh slice is plain input, cache_creation=0 — Codex has
                # no separate cache-write fee) and its blocks.
                for gi, gen in enumerate(gens):
                    blocks, tools = self._subagent_generation_blocks(gen, thread_id)
                    if not blocks:
                        continue
                    for tel in tools:
                        commands.append(tel)
                        emit.on_event(ToolStartEvent(task_id=task_id, turn_id=turn_id, tool=tel))
                        emit.on_event(
                            ToolEndEvent(
                                task_id=task_id,
                                turn_id=turn_id,
                                tool=tel,
                                status=ToolEndStatus.ERROR if tel.result_status == "error" else ToolEndStatus.OK,
                            )
                        )
                    messages.append(self._subagent_generation_message(blocks, gen, parent_tool_id, model, turn_id, gi))
            except Exception as exc:
                # Best-effort: a recovery hiccup must never fail the turn.
                self._log.debug("CodexAgent: sub-agent recovery failed for %s: %s", thread_id, exc)

    @staticmethod
    def _codex_home() -> Path:
        """Codex data directory (rollouts live under ``<home>/sessions``)."""
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))

    async def _await_rollout_file(self, home: Path, thread_id: str, *, attempts: int = 20) -> Path | None:
        """Locate a thread's rollout file, polling briefly for the async flush.

        The child turn has finished by the time its ``wait`` returns, but the
        rollout recorder flushes on a background task, so the file can lag the
        parent ``turn/completed`` by a beat. Poll up to ~2s before giving up.

        If ``<home>/sessions`` doesn't exist at all, the binary isn't writing
        rollouts there — bail immediately rather than polling for a flush that
        can never land (also keeps unit tests with a stub home fast).
        """
        if not (home / "sessions").is_dir():
            return None
        for _ in range(attempts):
            path = self._find_rollout_file(home, thread_id)
            if path is not None:
                return path
            await asyncio.sleep(0.1)
        return None

    @staticmethod
    def _find_rollout_file(home: Path, thread_id: str) -> Path | None:
        """Find ``<home>/sessions/**/rollout-<ts>-<thread_id>.jsonl`` (the id is
        embedded verbatim in the filename, so a suffix glob is exact)."""
        sessions = home / "sessions"
        if not sessions.is_dir():
            return None
        matches = sorted(sessions.glob(f"**/rollout-*-{thread_id}.jsonl"))
        return matches[-1] if matches else None

    @classmethod
    def _parse_rollout_generations(cls, path: Path) -> list[dict[str, Any]]:
        """Reconstruct a sub-agent's GENERATIONS from its rollout JSONL.

        A ``token_count`` event marks each generation boundary (same as the
        parent stream's ``thread/tokenUsage/updated``). We walk the ordered
        ``response_item`` lines, accumulating tool calls / assistant text into the
        current generation, and close it on each ``token_count`` with that
        generation's ``last_token_usage``. Tool CALLS are paired with their OUTPUT
        (``*_output``, possibly emitted in a later generation) by ``call_id``.

        Returns ordered generation dicts: ``{"tokens": (input, cached, output,
        reasoning) | None, "items": [ordered specs], "tools": [tool-call dicts]}``.
        Trailing items with no closing ``token_count`` flush as a final
        token-less generation. Partial/corrupt lines are skipped.
        """
        objs: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                objs.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

        # Pass 1: tool OUTPUTs by call_id (a call's result can land a generation later).
        outputs: dict[str, tuple[str, bool]] = {}
        for obj in objs:
            if obj.get("type") == "response_item":
                p = obj.get("payload") or {}
                if p.get("type") in _ROLLOUT_TOOL_OUTPUT_TYPES:
                    outputs[str(p.get("call_id") or "")] = cls._subagent_output(p)

        # Pass 2: walk into generations.
        gens: list[dict[str, Any]] = []
        cur: list[dict[str, Any]] = []
        n_calls = 0

        def close(tokens: tuple[int, int, int, int] | None) -> None:
            nonlocal cur
            if not cur and tokens is None:
                return
            gens.append({"tokens": tokens, "items": cur, "tools": [it["call"] for it in cur if it["kind"] == "tool"]})
            cur = []

        for obj in objs:
            t = obj.get("type")
            p = obj.get("payload") or {}
            if t == "response_item":
                pt = p.get("type")
                if pt in _ROLLOUT_TOOL_CALL_TYPES:
                    call_id = str(p.get("call_id") or p.get("id") or f"call_{n_calls}")
                    n_calls += 1
                    summary, is_error = outputs.get(call_id, ("", False))
                    cur.append(
                        {
                            "kind": "tool",
                            "call": {
                                "call_id": call_id,
                                "tool_name": cls._subagent_tool_name(p),
                                "parameters": cls._subagent_parameters(p),
                                "result_summary": summary,
                                "is_error": is_error,
                            },
                        }
                    )
                elif pt == "message" and p.get("role") == "assistant":
                    text = cls._message_text(p.get("content"))
                    if text:
                        cur.append({"kind": "text", "text": text})
            elif t == "event_msg" and p.get("type") == "token_count":
                last = (p.get("info") or {}).get("last_token_usage") or {}
                close(
                    (
                        int(last.get("input_tokens", 0) or 0),
                        int(last.get("cached_input_tokens", 0) or 0),
                        int(last.get("output_tokens", 0) or 0),
                        int(last.get("reasoning_output_tokens", 0) or 0),
                    )
                )
        close(None)
        return gens

    @staticmethod
    def _message_text(content: Any) -> str:
        """Join the text of a rollout ``message`` ResponseItem's content parts."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]
        return "".join(parts)

    @staticmethod
    def _subagent_tool_name(payload: dict[str, Any]) -> str:
        """Friendly tool name for a rollout tool-call ResponseItem."""
        name = payload.get("name")
        if isinstance(name, str) and name:
            return _ROLLOUT_FN_NAMES.get(name, name)
        if payload.get("type") == "local_shell_call":
            return "Bash"
        return "Tool"

    @staticmethod
    def _subagent_parameters(payload: dict[str, Any]) -> dict[str, Any]:
        """Best-effort parameters for a rollout tool-call ResponseItem.

        ``function_call.arguments`` is a JSON string; ``local_shell_call`` carries
        an ``action``. Shell-style calls are normalized to ``{"command": ...}`` so
        the transcript renders the command line; everything else is passed through.
        """
        args = payload.get("arguments")
        if isinstance(args, str) and args:
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(args)
                if isinstance(parsed, dict):
                    if "cmd" in parsed and "command" not in parsed:
                        parsed["command"] = parsed["cmd"]
                    return parsed
            return {"arguments": args}
        action = payload.get("action")
        if isinstance(action, dict):
            command = action.get("command")
            if isinstance(command, list):
                return {"command": " ".join(str(c) for c in command)}
            if command is not None:
                return {"command": str(command)}
            return dict(action)
        if isinstance(payload.get("input"), (str, dict)):
            return {"input": payload["input"]}
        return {}

    @staticmethod
    def _subagent_output(payload: dict[str, Any]) -> tuple[str, bool]:
        """(result_summary, is_error) for a rollout tool-output ResponseItem."""
        output = payload.get("output")
        is_error = False
        if isinstance(output, dict):
            if output.get("success") is False:
                is_error = True
            text = output.get("content") or output.get("output") or json.dumps(output)
        else:
            text = "" if output is None else str(output)
        return str(text), is_error

    def _subagent_generation_blocks(
        self, gen: dict[str, Any], thread_id: str
    ) -> tuple[list[ContentBlock], list[CommandTelemetry]]:
        """Content blocks + tool telemetry for one recovered sub-agent generation.

        Blocks are emitted in rollout order (tool calls, assistant text). Each
        tool call gets a ``tool_use`` block whose id (``sub:<thread>:<call_id>``)
        matches a ``CommandTelemetry`` so the evalboard tool row resolves. Inner
        tool ids are thread-prefixed to stay unique across the parent's own tools.
        """
        blocks: list[ContentBlock] = []
        telemetries: list[CommandTelemetry] = []
        for seq, item in enumerate(gen["items"]):
            if item["kind"] == "tool":
                call = item["call"]
                tool_id = f"sub:{thread_id}:{call['call_id']}"
                blocks.append(
                    ContentBlock(block_type="tool_use", sequence=seq, tool_use_id=tool_id, is_error=call["is_error"])
                )
                telemetries.append(
                    CommandTelemetry(
                        tool_name=call["tool_name"],
                        tool_id=tool_id,
                        timestamp=datetime.now(),
                        parameters=call["parameters"],
                        result_status="error" if call["is_error"] else "success",
                        result_summary=call["result_summary"],
                    )
                )
            elif item["kind"] == "text":
                blocks.append(ContentBlock(block_type="text", sequence=seq, text=item["text"]))
        return blocks, telemetries

    def _subagent_generation_message(
        self,
        blocks: list[ContentBlock],
        gen: dict[str, Any],
        parent_tool_use_id: str,
        model: str | None,
        turn_id: str,
        index: int,
    ) -> AssistantMessage:
        """A nested sub-agent generation as an AssistantMessage with real tokens.

        Parented to the spawning Agent call so it nests in the transcript. Tokens
        come from the child's per-generation ``token_count``: the fresh slice
        (input - cached) is plain ``input`` and ``cache_creation`` is 0 — Codex
        has no separate cache-write fee.
        """
        raw_input, cached, output, reasoning = gen["tokens"] or (0, 0, 0, 0)
        fresh = _fresh_input_tokens(raw_input, cached)
        now = datetime.now()
        return AssistantMessage(
            started_at=now,
            completed_at=now,
            generation_duration_ms=0.0,
            content_blocks=blocks,
            tool_use_ids=[b.tool_use_id for b in blocks if b.block_type == "tool_use" and b.tool_use_id],
            input_tokens=fresh,
            output_tokens=output,
            cache_creation_tokens=0,
            cache_read_tokens=cached,
            reasoning_tokens=reasoning,
            model=model or self._effective_model(),
            message_id=f"{turn_id}-subagent-{index}",
            parent_tool_use_id=parent_tool_use_id,
        )

    def _subagent_text_message(
        self, text: str, parent_tool_use_id: str, model: str | None, turn_id: str, index: int
    ) -> AssistantMessage:
        """Fallback nested message: just the sub-agent's returned text, tokenless.

        Used only when the child's rollout can't be found, so the answer still
        shows under the Agent call even without per-generation detail. ``model``
        is the spawned sub-agent's model (not the parent's), matching the
        rollout-found path."""
        now = datetime.now()
        return AssistantMessage(
            started_at=now,
            completed_at=now,
            generation_duration_ms=0.0,
            content_blocks=[ContentBlock(block_type="text", sequence=0, text=text)],
            tool_use_ids=[],
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            model=model or self._effective_model(),
            message_id=f"{turn_id}-subagent-{index}",
            parent_tool_use_id=parent_tool_use_id,
        )

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
        same signal they get from Claude's Write/Edit tool calls. A failed/declined
        apply_patch is recorded as an ``error`` (the old ``status != "error"``
        test never matched the real PatchApplyStatus values, so failed patches
        were scored as successful writes).
        """
        try:
            paths = [str(c.path) for c in changes if hasattr(c, "path")] if changes else []
            status_str = _status_value(status)
            failed = status_str in _FILE_CHANGE_FAILURE_STATUSES
            return CommandTelemetry(
                tool_name="Write",
                tool_id=change_id,
                timestamp=datetime.now(),
                duration_ms=None,
                parameters={"paths": paths},
                result_status="error" if failed else "success",
                result_summary=(
                    f"{len(paths)} file(s) changed"
                    if not failed
                    else f"apply_patch {status_str}: {len(paths)} file(s) not written"
                ),
                error_message=f"apply_patch {status_str}" if failed else None,
                result_data=None,
                sequence_number=sequence,
            )
        except Exception as e:
            self._log.debug(f"Failed to extract file-change telemetry: {e}")
            return None

    def _token_usage_from_sdk(self, sdk_token_usage: Any) -> TokenUsage | None:
        """Convert the Codex SDK's ThreadTokenUsage to our TokenUsage.

        Single conversion site for both the TurnEndEvent and the AgentEndEvent,
        so cached-input tokens can't be captured in one path but dropped in the
        other. The Codex SDK does not surface cost, so we derive it from the
        pricing table keyed on the effective model (None if the model is unpriced).

        Cache-bucket convention (Codex/OpenAI): the SDK's ``input_tokens`` is the
        FULL prompt count, *inclusive* of the cached prefix. The fresh slice
        (``input_tokens - cached``) is the uncached input (OpenAI bills no separate
        cache-write fee), so:

            uncached_input_tokens       = input - cached
            cache_creation_input_tokens = 0            (no separate cache-write bucket)
            cache_read_input_tokens     = cached
            input_tokens (derived)      = uncached + cache_read == the full prompt

        Cost bills the uncached slice at the input rate — identical to the old
        "fresh as cache-write" pricing since OpenAI's cache-write rate == input
        rate, just labeled honestly.
        """
        if not sdk_token_usage:
            return None
        total = getattr(sdk_token_usage, "total", None)
        if not total:
            return None
        input_tokens = getattr(total, "input_tokens", 0) or 0
        output_tokens = getattr(total, "output_tokens", 0) or 0
        cached_input = getattr(total, "cached_input_tokens", 0) or 0
        # Fresh (uncached) prompt slice = full prompt minus the cached prefix.
        uncached = _fresh_input_tokens(input_tokens, cached_input)
        cost = calculate_cost(
            self._effective_model() or "",
            uncached_input_tokens=uncached,
            output_tokens=output_tokens,
            cache_read_tokens=cached_input,
        )
        return TokenUsage(
            uncached_input_tokens=uncached,
            output_tokens=output_tokens,
            cache_read_input_tokens=cached_input,
            total_cost_usd=cost,
        )

    def _fold_subagent_tokens(self, parent: TokenUsage | None, messages: list[TranscriptMessage]) -> TokenUsage | None:
        """Add recovered sub-agent (child-thread) tokens to the parent turn total.

        Codex bills children on separate threads, so the parent's streamed total
        (``_token_usage_from_sdk`` / parent-only ``_token_usage_from_messages``)
        omits them. The child generations were reconstructed as
        ``parent_tool_use_id``-tagged ``AssistantMessage``s carrying their real
        per-generation tokens (fresh slice in ``input_tokens``, ``cache_read`` for
        the cached prefix, no ``cache_creation`` — Codex has no cache-write fee).
        Sum those here as ``uncached_input``, priced per child model, to make the
        turn total all-inclusive — the same end state Claude reaches naturally,
        where sub-agent messages bubble into the parent stream. A no-op when no
        child generations were recovered.
        """
        children = [
            m
            for m in messages
            if isinstance(m, AssistantMessage)
            and m.parent_tool_use_id is not None
            and (m.input_tokens or m.output_tokens or m.cache_creation_tokens or m.cache_read_tokens)
        ]
        if not children:
            return parent
        base = parent or TokenUsage()

        # Price each child generation on its own model (sub-agents may run a
        # different model than the parent), then sum.
        child_cost = 0.0
        for m in children:
            child_cost += (
                calculate_cost(
                    m.model or self._effective_model() or "",
                    uncached_input_tokens=_message_uncached_input(m),
                    output_tokens=m.output_tokens,
                    cache_read_tokens=m.cache_read_tokens,
                )
                or 0.0
            )

        base_cost = base.total_cost_usd
        return TokenUsage(
            uncached_input_tokens=base.uncached_input_tokens + sum(_message_uncached_input(m) for m in children),
            output_tokens=base.output_tokens + sum(m.output_tokens for m in children),
            cache_creation_input_tokens=base.cache_creation_input_tokens,
            cache_read_input_tokens=base.cache_read_input_tokens + sum(m.cache_read_tokens for m in children),
            total_cost_usd=(base_cost or 0.0) + child_cost if (base_cost is not None or child_cost) else None,
        )

    def _token_usage_from_messages(self, messages: list[TranscriptMessage]) -> TokenUsage | None:
        """Sum per-generation tokens off the captured assistant messages.

        Crash/timeout fallback for ``_finalize``: when the stream raises before it
        returns the SDK ``total`` (so ``_token_usage_from_sdk`` has nothing), the
        per-generation tokens were already recorded on the flushed
        ``AssistantMessage``s (fresh slice in ``input_tokens``, cached prefix in
        ``cache_read``). Summing them recovers the tokens/cost a crashed turn
        actually spent. Returns None when nothing was captured, matching
        ``_token_usage_from_sdk``'s empty contract.
        """
        # PARENT-thread messages only — sub-agent (separate-thread) tokens are
        # added via _fold_subagent_tokens, not summed here (would double-count).
        assistant = [m for m in messages if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None]
        if not assistant:
            return None
        uncached = sum(_message_uncached_input(m) for m in assistant)
        output = sum(m.output_tokens for m in assistant)
        cache_read = sum(m.cache_read_tokens for m in assistant)
        if not (uncached or output or cache_read):
            return None
        cost = calculate_cost(
            self._effective_model() or "",
            uncached_input_tokens=uncached,
            output_tokens=output,
            cache_read_tokens=cache_read,
        )
        return TokenUsage(
            uncached_input_tokens=uncached,
            output_tokens=output,
            cache_read_input_tokens=cache_read,
            total_cost_usd=cost,
        )

    @staticmethod
    async def _run_async(func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a potentially blocking or async function."""
        result = func(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result
