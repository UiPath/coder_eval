"""Claude Code agent implementation using the Claude Agent SDK."""

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    Message,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    UserMessage,
    query,
)

# Private SDK import — the public `query()` API doesn't expose the subprocess
# handle, but we need it to SIGKILL on timeout (the SDK's anyio task groups
# swallow asyncio cancellation, so cooperative cancel doesn't preempt a stuck
# CLI). If this import breaks on an SDK upgrade, the threaded watchdog loses
# its kill target and timeouts will no longer be enforced at the agent layer.
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._logging import PrefixedAdapter
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import ThreadedWatchdog
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
    BedrockRoute,
    ClaudeCodeAgentConfig,
    CommandTelemetry,
    ContentBlock,
    DirectRoute,
    ProxyRoute,
    ResultSummary,
    SubAgentUsage,
    TokenUsage,
    TurnRecord,
    to_bedrock_inference_profile,
)
from coder_eval.models import (
    AssistantMessage as AssistantMessageTelemetry,
)
from coder_eval.models import (
    UserMessage as UserMessageTelemetry,
)
from coder_eval.streaming.callbacks import StreamCallback, safe_emit
from coder_eval.streaming.events import TextChunkEvent, ToolCallEvent, ToolResultEvent, TurnCompleteEvent


logger = logging.getLogger(__name__)


# Type guards for SDK message types (using duck typing for robustness)
def _is_assistant_message(message: Any) -> bool:
    """Check if message is an AssistantMessage using duck typing."""
    return hasattr(message, "content") and hasattr(message, "model")


def _is_tool_use_block(block: Any) -> bool:
    """Check if block is a ToolUseBlock using duck typing."""
    return hasattr(block, "name") and hasattr(block, "id") and hasattr(block, "input")


def _is_thinking_block(block: Any) -> bool:
    """Check if block is a ThinkingBlock (extended-thinking reasoning)."""
    return hasattr(block, "thinking") and not hasattr(block, "text")


def _is_text_block(block: Any) -> bool:
    """Check if block is a TextBlock (visible narration)."""
    return hasattr(block, "text") and not hasattr(block, "thinking")


def _distribute_output_tokens(total: int, weights: list[int]) -> list[int]:
    """Split a call's output_tokens across its block-emissions by content weight.

    The Anthropic API reports output_tokens per API *call*, not per content
    block, but the CLI surfaces one call as several per-block emissions. To make
    each emission's recorded output sensible (rather than dumping the whole
    call on the first block and zeroing the rest), we apportion the call total
    across emissions by a content-length proxy (thinking/text length, or tool
    name + serialized args length).

    Uses the largest-remainder (Hamilton) method so the returned integers sum
    EXACTLY to ``total`` — per-message output stays reconcilable with the
    iteration aggregate. Falls back to an even split when all weights are zero.
    """
    n = len(weights)
    if n == 0:
        return []
    if total <= 0:
        return [0] * n
    tw = sum(weights)
    if tw <= 0:
        base = total // n
        out = [base] * n
        for i in range(total - base * n):
            out[i] += 1
        return out
    raw = [total * w / tw for w in weights]
    floors = [int(r) for r in raw]
    remainder = total - sum(floors)
    # Hand the leftover (from flooring) to the largest fractional parts.
    order = sorted(range(n), key=lambda i: (raw[i] - floors[i], weights[i]), reverse=True)
    for i in range(remainder):
        floors[order[i]] += 1
    return floors


def _is_user_message(message: Any) -> bool:
    """Check if message is a UserMessage (which may contain tool results) using duck typing."""
    return hasattr(message, "content") and hasattr(message, "tool_use_result")


def _is_tool_result_block(block: Any) -> bool:
    """Check if block is a ToolResultBlock using duck typing."""
    return hasattr(block, "tool_use_id") and hasattr(block, "is_error")


def _is_task_notification(message: Any) -> bool:
    """Check if message is a TaskNotificationMessage (sub-agent terminal event).

    It is a SystemMessage carrying per-sub-agent ``usage`` (a TaskUsage), keyed
    by the spawning ``tool_use_id``. It also has ``session_id`` + ``usage``, so
    it would otherwise be misread as the final ResultMessage — hence the
    explicit guard, checked before ``_is_sdk_result_message``. Identified by the
    SDK type or ``subtype`` (both reliably present on the real message); we avoid
    attribute-presence sniffing so it can't misfire on test mocks. The ``subtype``
    fallback also lets duck-typed mocks (not real SDK instances) be recognized.
    """
    return isinstance(message, TaskNotificationMessage) or getattr(message, "subtype", None) == "task_notification"


def _is_sdk_result_message(message: Any) -> bool:
    """Check if message is the SDK's final ResultMessage (with usage/cost data).

    Distinct from ToolResultBlock which has tool_use_id, and from
    TaskNotificationMessage which also carries session_id + usage (excluded).
    """
    return hasattr(message, "session_id") and hasattr(message, "usage") and not _is_task_notification(message)


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


@AgentRegistry.register(AgentKind.CLAUDE_CODE, ClaudeCodeAgentConfig)
class ClaudeCodeAgent(Agent[ClaudeCodeAgentConfig]):
    """Implementation of the Agent interface for Claude Code using the SDK."""

    def __init__(
        self,
        config: ClaudeCodeAgentConfig,
        route: ApiRoute | None = None,
        *,
        instance_name: str = "coder",
        extra_mcp_servers: dict[str, Any] | None = None,
    ):
        """Initialize the Claude Code agent.

        Args:
            config: Agent configuration
            route: API routing configuration. If None, uses DirectRoute.
            instance_name: Short label used to prefix this instance's log
                records (e.g. ``"coder"`` for the coding agent,
                ``"simulator"`` for the tools-disabled user-simulator agent).
                Lets you tell them apart in ``task.log`` when both run in
                the same process.
            extra_mcp_servers: Runtime-only in-process MCP servers (e.g. the
                judge ``submit_verdict`` tool) merged into ``ClaudeAgentOptions.mcp_servers``.
                NOT sourced from YAML — ``mcp_servers`` is in
                ``_FRAMEWORK_OWNED_SDK_FIELDS`` and explicitly denied via
                ``sdk_options`` for security. The judge criterion is the only
                caller today.
        """
        self.config = config
        self.route = route or DirectRoute()
        self._extra_mcp_servers = extra_mcp_servers or {}
        self.client: ClaudeSDKClient | None = None
        self.working_directory: Path | None = None
        self._state = AgentState.WORKING
        self._iteration = 0
        # Set True right after _iteration is incremented at the start of communicate();
        # consumed (and cleared) by discard_pending_turn() to roll back the counter exactly
        # once per failed turn, even when _set_pending swallows a partial-build exception
        # and leaves pending_turn as None.
        self._iteration_was_incremented = False
        self._sdk_options_dump: dict[str, Any] | None = None
        self._session_id: str | None = None
        # Transport reference held only while a communicate() call is in flight,
        # so kill() can reach into the CLI subprocess when the SDK swallows
        # asyncio cancellation.
        self._active_transport: SubprocessCLITransport | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})
        # Deduplicate "unhandled SDK message type" warnings per agent
        # instance — _format_messages runs many times per task and these
        # types are stable for the lifetime of a session.
        self._warned_unknown_types: set[str] = set()
        self.pending_turn: TurnRecord | None = None

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        """Initialize and start the Claude Code agent.

        Args:
            working_directory: Path to the working directory
            env_path_prepend: Absolute directories to prepend to PATH for the SDK
                subprocess (typically the resolved ``SandboxConfig.mock_path_dirs``).
            plugin_tools_dir: Canonical ``node_modules/@uipath`` to export as
                ``PLUGIN_TOOLS_DIR``. An external env-var pin still wins.
        """
        self.working_directory = Path(working_directory)
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        self._state = AgentState.WORKING
        # Note: Client is created per-communication to avoid transport issues

    @staticmethod
    def _build_sdk_env(
        route: ApiRoute,
        path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> tuple[dict[str, str], str | None]:
        """Build SDK environment variables and resolve effective model for the given route.

        Args:
            route: API routing configuration.
            path_prepend: Absolute directories to prepend (in order) to PATH so their
                contents shadow same-named binaries in the parent PATH. Resolved by the
                sandbox manager from ``SandboxConfig.mock_path_dirs``; the agent does no
                filesystem inspection of its own.
            plugin_tools_dir: Fallback canonical ``node_modules/@uipath`` to export as
                ``PLUGIN_TOOLS_DIR`` when the process environment doesn't already
                provide one. An external ``PLUGIN_TOOLS_DIR`` always wins.

        Returns:
            Tuple of (env_vars_dict, model_override_or_None).
        """
        base_env: dict[str, str] = {}
        if path := os.environ.get("PATH"):
            base_env["PATH"] = path

        if path_prepend:
            prefix = os.pathsep.join(path_prepend)
            base_env["PATH"] = f"{prefix}{os.pathsep}{base_env.get('PATH', '')}"

        # Pin UiPath CLI plugin discovery for the agent SDK subprocess. External
        # env wins over sandbox-derived fallback so operators can override.
        if tools_dir := os.environ.get("PLUGIN_TOOLS_DIR"):
            base_env["PLUGIN_TOOLS_DIR"] = tools_dir
        elif plugin_tools_dir:
            base_env["PLUGIN_TOOLS_DIR"] = plugin_tools_dir

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

    def _resolve_effective_model(
        self, config_model: str | None, env: dict[str, str], route_model: str | None
    ) -> str | None:
        """Resolve the effective model and sync subprocess env on Bedrock.

        Precedence: config_model (task YAML / --model / DEFAULT_AGENT_MODEL) wins
        over the route default (BEDROCK_MODEL). On a Bedrock route, a bare alias
        is auto-qualified with ``anthropic.`` and the region's inference-profile
        prefix (``eu.``/``us.``/``apac.``) so the same value works across regions.
        On Bedrock, the resolved value is always written to ``ANTHROPIC_MODEL``
        so the subprocess sees the same model as ``ClaudeAgentOptions.model``.
        """
        if isinstance(self.route, BedrockRoute):
            if config_model is not None:
                config_model = to_bedrock_inference_profile(config_model, self.route.region)
            effective = config_model or route_model
            if effective:
                env["ANTHROPIC_MODEL"] = effective
            return effective
        return config_model or route_model

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
    ) -> TurnRecord:
        """Send a message to Claude and receive its response.

        Args:
            user_input: The message/prompt to send
            stream_callback: Optional callback for real-time event streaming
            timeout: Hard wall-clock deadline in seconds. When exceeded, a
                watchdog task force-kills the CLI subprocess (the SDK's anyio
                task groups suppress cooperative cancellation, so a graceful
                asyncio.wait_for is not sufficient).
            max_turns: Hard cap on inner-loop turns for this call. None defers
                to the SDK default.

        Returns:
            TurnRecord containing the complete interaction

        Raises:
            RuntimeError: If agent is not started.
            TurnTimeoutError: Watchdog/wall-clock fired; carries a partial TurnRecord.
            AgentCrashError: SDK/CLI failed mid-turn; carries a partial TurnRecord.
        """
        if not self.working_directory:
            raise RuntimeError("Agent not started. Call start() first.")

        # AgentConfig.type is `AgentKind | None` (Phase 3) but the orchestrator,
        # SubAgentRunner, and UserSimulator all set it before construction. Assert
        # the invariant so streaming-event sites below can safely call `type.value`.
        assert self.config.type is not None, "ClaudeCodeAgent requires AgentConfig.type to be set before communicate()"

        # Reset slot defensively in case the previous caller forgot to drain it.
        self.pending_turn = None

        turn_start_time = time.monotonic()
        deadline = turn_start_time + timeout if timeout is not None else None
        # timeout_hit is set by _on_turn_timeout (timer thread) and read by
        # the asyncio thread via _timed_out(). Python bool assignment is
        # atomic under the GIL; no explicit lock needed here.
        timeout_hit = False

        self._iteration += 1
        self._iteration_was_incremented = True

        # Collect all messages from the turn
        messages = []

        # NEW: Two-phase command tracking with precise duration measurement
        # Phase 1: Store pending commands keyed by tool_id with start time
        # Phase 2: Update status and duration when ResultMessage arrives
        pending_commands: dict[str, dict[str, Any]] = {}  # tool_id -> {telemetry, command_start_time}
        processed_results: set[str] = set()  # Track duplicate ResultMessages
        sequence_number = 0

        # Per-message telemetry (SDK messages). The list index is the
        # assistant_turn_index attached to commands emitted in that turn.
        sdk_messages: list[UserMessageTelemetry | AssistantMessageTelemetry] = []
        last_assistant_message_index: int | None = None  # Track last AssistantMessage to populate with final tokens
        # The SDK does not surface a "message started" event, so we approximate
        # generation_duration_ms as wall-clock between SDK events: previous tool
        # result (or turn start) to AssistantMessage arrival.
        last_event_monotonic: float = turn_start_time
        last_event_wall: datetime = datetime.now()

        # SDK ResultMessage token usage (captured from final message)
        sdk_result_usage: dict[str, Any] | None = None
        # Cumulative per-model billing (authoritative for token_usage + cost —
        # see _build_token_usage). Captured from the final ResultMessage.
        sdk_result_model_usage: dict[str, Any] | None = None
        sdk_result_cost: float | None = None
        # Per-sub-agent usage from TaskNotification messages, keyed by the
        # spawning Agent tool_use_id. The sub-agent's own emissions are only
        # partially surfaced in the stream, so this is the one per-sub-agent
        # token figure the SDK exposes.
        sub_agent_usages: list[SubAgentUsage] = []
        num_turns: int | None = None
        # Diagnostic summary of the final ResultMessage (status + error fields).
        # Populated on every ResultMessage (last one wins). Consumed by the
        # session-id retention branch, the debug-log path, and the error-path
        # formatter; persisted on TurnRecord only on the success path (the
        # error paths raise before the TurnRecord constructor runs).
        sdk_result_summary: ResultSummary | None = None

        # Model identifier from AssistantMessage (last one wins)
        sdk_model_used: str | None = None

        # Per-emission output_tokens recovered from raw stream events.
        # The CLI emits AssistantMessage.usage.output_tokens with only a
        # partial streaming snapshot (anthropics/claude-code#22686); the
        # authoritative cumulative count for an API call arrives later, on
        # that call's ``message_delta`` stream event.
        #
        # The ``message_delta`` event has no message_id, but the preceding
        # ``message_start`` does, and both bracket the same API call. So we
        # track the in-flight message_id (current_stream_message_id) and, when
        # the delta arrives, split the call's cumulative output_tokens across
        # that call's block-emissions (emissions_by_id) by a content-length
        # proxy (emission_proxies_by_id) via _distribute_output_tokens. This
        # fixes both the off-by-one of the old "stamp on the next message"
        # scheme (which credited call N's output to call N+1) and the
        # all-on-the-first-block dump (which left tool emissions reading 0).
        # input / cache stay on the first emission only — those are per-call
        # read costs, not generated per block.
        #
        # pending_delta_output_tokens is retained only as a fallback for
        # streams that never surface a message_start id (legacy SDKs / mocks).
        pending_delta_output_tokens: int | None = None
        current_stream_message_id: str | None = None
        emissions_by_id: dict[str, list[AssistantMessageTelemetry]] = {}
        emission_proxies_by_id: dict[str, list[int]] = {}

        # Anthropic's CLI splits one API call into multiple "assistant"
        # JSON events (one per content-block kind) that all share the
        # same ``message_id`` and repeat the SAME usage dict. Summing
        # tokens across them double-counts. We populate tokens only on
        # the first AssistantMessage for each id; subsequent ones for
        # the same id get zeros so naive sums reconcile with the
        # iteration aggregate.
        seen_message_ids: set[str] = set()

        # Whether the most recent AssistantMessage carried a ``message_id``.
        # Used to gate the ResultMessage backfill *per-message* (not per-turn):
        # if the SDK emits some events with an id and some without (mixed
        # streams), we want the backfill to apply only to the last one when
        # it lacked an id, and stay suppressed when it had one.
        last_message_had_id: bool = False

        # Count of AssistantMessage objects in this turn
        assistant_turn_count = 0

        # Capture stderr for debugging
        stderr_lines = []

        def capture_stderr(line: str) -> None:
            stderr_lines.append(line)

        # Build a partial TurnRecord and store it in the slot. Partial-build
        # failure is downgraded to None so the typed exception's category is
        # preserved; rollback of _iteration happens exclusively in
        # discard_pending_turn(), which the orchestrator calls after every
        # failed communicate().
        def _set_pending(crash_reason: str) -> None:
            try:
                self.pending_turn = self._build_partial_turn_record(
                    user_input=user_input,
                    messages=messages,
                    pending_commands=pending_commands,
                    assistant_turn_count=assistant_turn_count,
                    messages_list=sdk_messages,
                    sdk_result_usage=sdk_result_usage,
                    sdk_result_model_usage=sdk_result_model_usage,
                    sdk_result_cost=sdk_result_cost,
                    num_turns=num_turns,
                    sdk_model_used=sdk_model_used,
                    sdk_result_summary=sdk_result_summary,
                    turn_start_time=turn_start_time,
                    crash_reason=crash_reason,
                    sub_agent_usages=sub_agent_usages,
                )
            except Exception:
                logger.exception("Failed to build partial turn record; continuing without partial")
                self.pending_turn = None

        try:
            # Process plugins: copy from config and replace env vars in paths
            plugins = self._process_plugins(self.config.plugins or [])  # type: ignore[arg-type]

            # Build env overrides and resolve model for the configured API route.
            # Precedence: task/CLI agent.model > route default (e.g. BEDROCK_MODEL).
            env, route_model = self._build_sdk_env(
                self.route,
                path_prepend=self._env_path_prepend,
                plugin_tools_dir=self._plugin_tools_dir,
            )
            effective_model = self._resolve_effective_model(self.config.model, env, route_model)

            disallowed_tools = list(self.config.disallowed_tools or [])
            # Do not allow ToolSearch. This is required to keep Bedrock backend in sync with the other backends.
            if "ToolSearch" not in disallowed_tools:
                disallowed_tools.append("ToolSearch")

            # as_posix(), not str(): bash on Windows strips backslashes from unquoted
            # paths, so a redirect like `> D:\foo\bar` ends up writing to "Dfoobar".
            options = ClaudeAgentOptions(
                cwd=self.working_directory.as_posix(),
                permission_mode=self.config.permission_mode.value,
                allowed_tools=self.config.allowed_tools or [],
                disallowed_tools=disallowed_tools,
                model=effective_model,
                max_turns=max_turns,
                plugins=plugins,  # type: ignore[arg-type]
                stderr=capture_stderr,  # Capture stderr for better error messages
                env=env,
                # Subscribe to raw stream events so we can recover the
                # *cumulative* output_tokens for each emission from
                # ``message_delta.usage`` events. Claude Code CLI ships
                # AssistantMessage.usage.output_tokens with only a partial
                # streaming snapshot (see anthropics/claude-code#22686),
                # so summing per-message values undercounts by 10x+.
                # ``message_delta`` carries the final per-emission output
                # tally; we stamp that onto the next AssistantMessage we
                # record. Without this flag StreamEvents are suppressed
                # by the SDK.
                include_partial_messages=True,
                system_prompt=self.config.system_prompt,
                setting_sources=self.config.setting_sources if self.config.setting_sources is not None else ["project"],
                resume=self._session_id,
                settings=json.dumps(self.config.claude_settings)
                if isinstance(self.config.claude_settings, dict)
                else self.config.claude_settings,
                mcp_servers=self._extra_mcp_servers,
                **self.config.sdk_options,
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

            # IMPORTANT: the transport is captured in the closure (not read
            # from self._active_transport) so a stale watchdog from an
            # earlier turn cannot kill a subsequent turn's subprocess.
            watchdog_target = transport

            def _on_turn_timeout() -> None:
                nonlocal timeout_hit
                timeout_hit = True
                self._kill_transport(watchdog_target)

            # Use the query function for one-shot interaction. Only forward
            # the transport kwarg when we actually built one — otherwise keep
            # the call shape identical to the pre-timeout code path so mocks
            # with strict signatures (prompt, options) keep working.
            query_kwargs: dict[str, Any] = {"prompt": user_input, "options": options}
            if transport is not None:
                query_kwargs["transport"] = transport
            self._log.debug("Starting agent query stream...")
            # OS-thread watchdog: fires at `timeout` seconds regardless of
            # event-loop liveness. Immune to anyio cancel-scope suppression,
            # which is why an asyncio.sleep-based watchdog was unreliable.
            with ThreadedWatchdog(
                timeout_seconds=timeout,
                on_timeout=_on_turn_timeout,
                asyncio_task_to_cancel=asyncio.current_task(),
                label=f"Turn timeout ({timeout:g}s)" if timeout else "turn_timeout",
            ):
                async for message in query(**query_kwargs):
                    # Wall-clock guard inside the loop. Triggers the cooperative
                    # exit path when messages are still flowing; the watchdog is
                    # the fallback when they aren't.
                    if deadline is not None and time.monotonic() > deadline:
                        timeout_hit = True
                        self._log.warning("Turn timeout reached mid-stream; breaking out of message loop")
                        break

                    messages.append(message)
                    msg_type = type(message).__name__

                    # Raw, untruncated dump of every SDK event exactly as it
                    # arrives — opt-in via CODER_EVAL_RAW_SDK_LOG so normal runs
                    # stay quiet. Use this to inspect what (if any) token usage
                    # rides on each message type (e.g. Agent tool-result vs.
                    # TaskNotification vs. ResultMessage).
                    self._log_message_raw(message, msg_type)

                    # Stream debug logging for real-time visibility
                    self._log_message_debug(message, msg_type)

                    # Two-phase command telemetry capture using type guards

                    # PHASE 1: Capture ToolUseBlock + build AssistantTurn record.
                    if _is_assistant_message(message):
                        message_arrival_monotonic = time.monotonic()
                        message_arrival_wall = datetime.now()
                        generation_started_wall = last_event_wall
                        generation_duration_ms = (message_arrival_monotonic - last_event_monotonic) * 1000

                        current_turn_index = len(sdk_messages)
                        assistant_turn_count += 1
                        model_attr = getattr(message, "model", None)
                        if isinstance(model_attr, str):
                            sdk_model_used = model_attr

                        content = getattr(message, "content", None)
                        turn_content_blocks: list[ContentBlock] = []
                        turn_tool_use_ids: list[str] = []
                        # Length of generated content in this emission, used to
                        # weight its share of the call's output_tokens.
                        emission_content_chars = 0

                        # Content can be a list of blocks (text, thinking, tool_use, etc.)
                        if content and isinstance(content, list):
                            for block in content:
                                block_seq = len(turn_content_blocks)

                                if _is_tool_use_block(block):
                                    tool_args = block.input if isinstance(block.input, dict) else {"raw": block.input}
                                    emission_content_chars += len(str(getattr(block, "name", "") or "")) + len(
                                        json.dumps(tool_args, default=str)
                                    )
                                    command_start_time = time.monotonic()  # Precise command start time

                                    telemetry = CommandTelemetry(
                                        tool_name=block.name,
                                        tool_id=block.id,
                                        timestamp=message_arrival_wall,
                                        generation_completed_at=message_arrival_wall,
                                        assistant_turn_index=current_turn_index,
                                        parameters=block.input
                                        if isinstance(block.input, dict)
                                        else {"raw": block.input},
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

                                    turn_content_blocks.append(
                                        ContentBlock(
                                            block_type="tool_use",
                                            sequence=block_seq,
                                            tool_use_id=block.id,
                                        )
                                    )
                                    turn_tool_use_ids.append(block.id)

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
                                elif _is_thinking_block(block):
                                    thinking_text = getattr(block, "thinking", None)
                                    if thinking_text:
                                        emission_content_chars += len(str(thinking_text))
                                    turn_content_blocks.append(
                                        ContentBlock(
                                            block_type="thinking",
                                            sequence=block_seq,
                                            thinking=str(thinking_text) if thinking_text else None,
                                            signature=getattr(block, "signature", None),
                                        )
                                    )
                                elif _is_text_block(block):
                                    text_value = str(block.text)
                                    emission_content_chars += len(text_value)
                                    turn_content_blocks.append(
                                        ContentBlock(
                                            block_type="text",
                                            sequence=block_seq,
                                            text=text_value,
                                        )
                                    )
                                    safe_emit(
                                        stream_callback,
                                        TextChunkEvent(
                                            task_id=self.config.type.value,
                                            text=text_value,
                                        ),
                                    )

                        # Per-message token usage with two corrections layered on
                        # the raw SDK values:
                        #
                        # 1. **Dedup by message_id.** Claude Code's CLI splits one
                        #    Anthropic API call into multiple "assistant" JSON
                        #    events (one per content-block kind) that share a
                        #    ``message_id`` and repeat the SAME usage dict.
                        #    Naively recording usage on each duplicates input /
                        #    cache_creation / cache_read. We populate tokens
                        #    only on the first AssistantMessage per id and
                        #    write zeros on follow-ups so naive sums match the
                        #    iteration aggregate.
                        # 2. **Override output_tokens from message_delta.** The
                        #    CLI's per-event ``usage.output_tokens`` is a
                        #    streaming snapshot, not cumulative (see
                        #    anthropics/claude-code#22686). We capture the
                        #    final cumulative value from the
                        #    ``message_delta`` stream event handler below and
                        #    stamp it here. Falls back to the raw SDK value
                        #    when ``include_partial_messages`` is off or the
                        #    delta wasn't observed.
                        msg_usage = getattr(message, "usage", None) or {}
                        message_id = getattr(message, "message_id", None)
                        # Branch identity: the Task tool_use_id that spawned this
                        # message's sub-agent (None for the main thread). Sub-agent
                        # calls bubble up into this same stream; this is the only
                        # link back to which branch they belong to — needed to model
                        # the cache cascade as a tree, not a flat list.
                        parent_tool_use_id = getattr(message, "parent_tool_use_id", None)
                        is_duplicate_emission = isinstance(message_id, str) and message_id in seen_message_ids
                        if isinstance(message_id, str):
                            seen_message_ids.add(message_id)
                            last_message_had_id = True
                        else:
                            last_message_had_id = False

                        if is_duplicate_emission:
                            # Same API call, additional content block — billing
                            # was already accounted for on the first one.
                            in_tok = out_tok = cw_tok = cr_tok = rt_tok = 0
                        else:
                            in_tok = int(msg_usage.get("input_tokens", 0) or 0)
                            cw_tok = int(msg_usage.get("cache_creation_input_tokens", 0) or 0)
                            cr_tok = int(msg_usage.get("cache_read_input_tokens", 0) or 0)
                            rt_tok = int(msg_usage.get("reasoning_tokens", 0) or 0)
                            # Prefer the stream-event delta value if we got one;
                            # else fall back to the (partial) SDK value so we
                            # don't regress relative to pre-fix behavior.
                            if pending_delta_output_tokens is not None:
                                out_tok = pending_delta_output_tokens
                            else:
                                out_tok = int(msg_usage.get("output_tokens", 0) or 0)
                            # Consume the pending value; the next emission will
                            # set its own via the StreamEvent handler.
                            pending_delta_output_tokens = None

                        assistant_telemetry = AssistantMessageTelemetry(
                            started_at=generation_started_wall,
                            completed_at=message_arrival_wall,
                            generation_duration_ms=max(0.0, generation_duration_ms),
                            content_blocks=turn_content_blocks,
                            tool_use_ids=turn_tool_use_ids,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            cache_creation_tokens=cw_tok,
                            cache_read_tokens=cr_tok,
                            reasoning_tokens=rt_tok,
                            stop_reason=(
                                getattr(message, "stop_reason", None)
                                if isinstance(getattr(message, "stop_reason", None), str)
                                else None
                            ),
                            model=sdk_model_used,
                            message_id=message_id if isinstance(message_id, str) else None,
                            parent_tool_use_id=(parent_tool_use_id if isinstance(parent_tool_use_id, str) else None),
                        )
                        sdk_messages.append(assistant_telemetry)
                        # Register every block-emission of this id (with its
                        # content-length proxy) so the matching ``message_delta``
                        # can split the call's output_tokens across them.
                        if isinstance(message_id, str):
                            emissions_by_id.setdefault(message_id, []).append(assistant_telemetry)
                            emission_proxies_by_id.setdefault(message_id, []).append(emission_content_chars)
                        # Track last AssistantMessage to populate with final tokens from ResultMessage
                        last_assistant_message_index = len(sdk_messages) - 1

                        # Mark this point as the latest event for the next
                        # generation_duration_ms calculation.
                        last_event_monotonic = message_arrival_monotonic
                        last_event_wall = message_arrival_wall

                    # TaskNotification fires when an Agent-tool spawn finishes,
                    # but its ``usage`` is lossy (omits cache-read). We capture
                    # per-sub-agent usage from the Agent tool-result instead (see
                    # the _is_user_message branch). This guard stays solely to
                    # stop _is_sdk_result_message from misreading it (session_id
                    # + usage would otherwise match).
                    elif _is_task_notification(message):
                        pass

                    # Capture SDK ResultMessage with token usage (check BEFORE tool results
                    # to avoid misclassification if SDK message also has tool_use_id/is_error)
                    elif _is_sdk_result_message(message):
                        sdk_result_usage = getattr(message, "usage", None)
                        sdk_result_model_usage = getattr(message, "model_usage", None)
                        sdk_result_cost = getattr(message, "total_cost_usd", None)
                        num_turns = getattr(message, "num_turns", None)
                        sdk_result_summary = self._summarize_result(message)
                        # Only advance session_id on clean turns. Resuming
                        # via --resume from an errored ResultMessage often
                        # reproduces the same crash (e.g. Windows PowerShell
                        # binary stdout case), so we keep the prior good id.
                        new_session_id = getattr(message, "session_id", None)
                        if sdk_result_summary is not None and sdk_result_summary.is_error:
                            self._log.debug(
                                "is_error ResultMessage; not advancing session_id (kept %s)",
                                self._session_id,
                            )
                        else:
                            if new_session_id != self._session_id:
                                self._log.debug("session_id changed: %s -> %s", self._session_id, new_session_id)
                            self._session_id = new_session_id

                        # Fallback: retro-populate the last AssistantMessage
                        # from ResultMessage.usage when per-message capture
                        # was not in effect for *that specific message* —
                        # i.e. it lacked a ``message_id`` (legacy SDKs / mock
                        # streams). We gate per-message rather than per-turn
                        # so mixed streams (some emissions with id, some
                        # without) still backfill the trailing id-less one
                        # without clobbering the id'd emissions whose tokens
                        # were already captured correctly. When per-message
                        # capture IS working, follow-up AssistantMessages
                        # from the same API call are intentionally zeroed;
                        # overwriting them with the ResultMessage cumulative
                        # would double-count against the first message.
                        if last_assistant_message_index is not None and sdk_result_usage and not last_message_had_id:
                            last_msg = sdk_messages[last_assistant_message_index]
                            if isinstance(last_msg, AssistantMessageTelemetry):
                                last_msg.input_tokens = int(sdk_result_usage.get("input_tokens", 0) or 0)
                                last_msg.output_tokens = int(sdk_result_usage.get("output_tokens", 0) or 0)
                                last_msg.cache_creation_tokens = int(
                                    sdk_result_usage.get("cache_creation_input_tokens", 0) or 0
                                )
                                last_msg.cache_read_tokens = int(
                                    sdk_result_usage.get("cache_read_input_tokens", 0) or 0
                                )
                                last_msg.reasoning_tokens = int(sdk_result_usage.get("reasoning_tokens", 0) or 0)

                    # Raw Anthropic stream events (only delivered when
                    # ``include_partial_messages=True``). We use these
                    # exclusively to recover the cumulative
                    # ``output_tokens`` for each emission — the CLI's
                    # AssistantMessage.usage.output_tokens carries only a
                    # streaming snapshot (anthropics/claude-code#22686).
                    # The ``message_delta`` event carries the final value
                    # right before ``message_stop``; we stash it so the
                    # next AssistantMessage we record can stamp it on.
                    elif isinstance(getattr(message, "event", None), dict):
                        evt: dict[str, Any] = getattr(message, "event", None) or {}
                        evt_type = evt.get("type")
                        if evt_type == "message_start":
                            # Opens an API call; carries the message_id that the
                            # call's later message_delta (which has none) belongs to.
                            mid = (evt.get("message") or {}).get("id")
                            current_stream_message_id = mid if isinstance(mid, str) else None
                        elif evt_type == "message_delta":
                            # Carries the authoritative cumulative output_tokens
                            # for the in-flight call. Split it across that call's
                            # block-emissions by content length so each emission
                            # reads a sensible share (and the parts sum exactly).
                            # Fall back to the stamp-on-next scheme only when we
                            # never saw a message_start id (legacy SDKs / mocks).
                            usage = evt.get("usage") or {}
                            ot = usage.get("output_tokens")
                            if isinstance(ot, int):
                                records: list[AssistantMessageTelemetry] | None = None
                                proxies: list[int] = []
                                if current_stream_message_id is not None:
                                    records = emissions_by_id.get(current_stream_message_id)
                                    proxies = emission_proxies_by_id.get(current_stream_message_id, [])
                                if records:
                                    shares = _distribute_output_tokens(ot, proxies)
                                    for record, share in zip(records, shares, strict=False):
                                        record.output_tokens = share
                                else:
                                    # No message_start id to attribute the delta to:
                                    # fall back to stamping it on the next emission.
                                    pending_delta_output_tokens = ot

                    # PHASE 2: Process tool results from UserMessage content blocks.
                    # The SDK delivers tool results as UserMessage objects containing
                    # ToolResultBlock in their content list (not as standalone messages).
                    elif _is_user_message(message):
                        # A user/tool-result message is what Claude reads
                        # next; the LLM's generation clock for the *next*
                        # assistant turn starts here.
                        last_event_monotonic = time.monotonic()
                        last_event_wall = datetime.now()

                        # The Agent tool-result rides on this message's
                        # ``tool_use_result`` and carries the COMPLETE
                        # per-sub-agent usage (input/output/cache-create/
                        # cache-read). Harvest it keyed by the Agent tool_use_id.
                        sub_usage = self._extract_sub_agent_usage(message)
                        if sub_usage is not None:
                            sub_agent_usages.append(sub_usage)

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

            self._log.debug("Agent query stream ended")

        except asyncio.CancelledError:
            # The threaded watchdog cancels the running task via
            # loop.call_soon_threadsafe(task.cancel) when it fires. If that
            # cancel landed *because* of the timeout, re-raise as
            # TurnTimeoutError so the retry system sees a terminal timeout
            # (not a transient cancel). External cancels (not our watchdog)
            # propagate unchanged.
            if self._timed_out(timeout_hit, deadline):
                self._state = AgentState.ERROR
                assert timeout is not None
                _set_pending(format_timeout_reason(timeout))
                raise TurnTimeoutError(timeout, iteration=self._iteration) from None
            raise
        except ProcessError as e:
            # When the watchdog SIGKILLs the subprocess, the SDK surfaces it
            # as a ProcessError (exit code -9). Classify as a timeout so the
            # retry system doesn't treat it as a transient AGENT_CRASH.
            if self._timed_out(timeout_hit, deadline):
                self._state = AgentState.ERROR
                assert timeout is not None
                _set_pending(format_timeout_reason(timeout))
                raise TurnTimeoutError(timeout, iteration=self._iteration) from e
            if not self._max_turns_short_circuit(sdk_result_summary, f"ProcessError(exit={e.exit_code})"):
                self._state = AgentState.ERROR
                stderr = self._build_stderr_message(e.stderr, stderr_lines)
                error_info = self._format_error_summary(sdk_result_summary)
                detail = error_info or stderr
                message = f"CLI process failed (exit code {e.exit_code}): {detail}"
                _set_pending(truncate_crash_message(message))
                raise AgentCrashError(message) from e
        except Exception as e:
            # Same race as above: the watchdog may have killed the subprocess
            # and the SDK may have re-raised as a generic Exception. Check
            # both the flag AND the wall-clock in case the flag flip races
            # with our catch-entry.
            if self._timed_out(timeout_hit, deadline):
                self._state = AgentState.ERROR
                assert timeout is not None
                _set_pending(format_timeout_reason(timeout))
                raise TurnTimeoutError(timeout, iteration=self._iteration) from e
            if not self._max_turns_short_circuit(sdk_result_summary, "Generic Exception"):
                self._state = AgentState.ERROR
                # The SDK wraps ProcessError as a generic Exception via the message stream.
                # Read the captured ResultMessage summary (if any) for diagnostic context.
                error_info = self._format_error_summary(sdk_result_summary)
                cause_stderr = self._extract_cause_stderr(e)
                stderr = self._build_stderr_message(cause_stderr, stderr_lines)
                error_details = self._clean_error_message(str(e))
                if error_info:
                    error_details += f"\nDetails: {error_info}"
                elif stderr:
                    error_details += f"\nStderr output:\n{stderr}"
                message = f"Communication with agent failed: {error_details}"
                _set_pending(truncate_crash_message(message))
                raise AgentCrashError(message) from e
        finally:
            self._active_transport = None

        # Only trust `timeout_hit` in the happy path: if the loop completed
        # cleanly, a wall-clock drift during post-loop cleanup would falsely
        # classify a successful turn as a timeout. The watchdog and in-loop
        # guard are the authoritative signals.
        if timeout_hit:
            assert timeout is not None
            _set_pending(format_timeout_reason(timeout))
            raise TurnTimeoutError(timeout, iteration=self._iteration)

        # PHASE 3: Finalize commands and build turn record
        commands = self._finalize_commands(pending_commands, messages)
        token_usage = self._build_token_usage(sdk_messages, sdk_result_usage, sdk_result_cost, sdk_result_model_usage)
        agent_output = self._format_messages(messages)
        self._update_state_from_messages(messages)

        # max_turns exhaustion: ResultMessage subtype OR num_turns > max_turns (strict; == is a normal completion).
        max_turns_exhausted = self._is_max_turns_result(sdk_result_summary) or (
            max_turns is not None and num_turns is not None and num_turns > max_turns
        )
        if max_turns_exhausted:
            self._log.warning(
                "Agent exhausted max_turns (%s/%s) — the SDK hit the turn limit before the agent completed.",
                num_turns,
                max_turns,
            )

        duration = time.monotonic() - turn_start_time

        # Clear the rollback flag — this turn completed successfully and the iteration
        # increment stands. (Discard is no-op on a successful turn anyway, but stay tidy.)
        self._iteration_was_incremented = False

        safe_emit(
            stream_callback,
            TurnCompleteEvent(
                task_id=self.config.type.value,
                iteration=self._iteration,
                duration_s=duration,
                command_count=len(commands),
                token_usage_str=format_token_usage(token_usage),
            ),
        )

        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output=agent_output,
            commands=commands,
            timestamp=datetime.now(),
            duration_seconds=duration,
            token_usage=token_usage,
            model_used=sdk_model_used,
            assistant_turn_count=assistant_turn_count,
            messages=sdk_messages,
            num_turns=num_turns,
            max_turns_exhausted=max_turns_exhausted,
            result_summary=sdk_result_summary,
            sub_agent_usage=sub_agent_usages,
        )

    async def stop(self) -> None:
        """Stop the agent and clean up resources."""
        self.client = None
        self.pending_turn = None
        self._state = AgentState.FINISHED

    async def kill(self) -> None:
        """Force-terminate the in-flight Claude CLI subprocess, if any.

        Async wrapper around ``kill_sync`` for callers that prefer async.
        The threaded watchdog inside communicate() uses ``_kill_transport``
        directly on a captured transport (not via ``self._active_transport``)
        to avoid a cross-turn race where a stale watchdog could kill a later
        turn's subprocess.
        """
        self.kill_sync()

    def kill_sync(self) -> None:
        """Synchronously SIGKILL the in-flight Claude CLI subprocess, if any.

        Safe to call from a non-asyncio thread (e.g. a ``threading.Timer``
        callback). Reads ``self._active_transport`` once; if a later turn
        has already cleared it, this is a no-op.
        """
        self._kill_transport(self._active_transport)

    async def discard_pending_turn(self) -> None:
        """Clear pending_turn and roll back the iteration counter.

        Decrement fires when either signal says a turn was attempted:
          - _iteration_was_incremented: set by communicate() right after the
            counter bump. Survives _set_pending swallowing a partial-build
            exception (pending_turn=None) — pending_turn alone is not a
            reliable signal.
          - pending_turn is not None: preserves backward compatibility for
            callers that set pending_turn directly (tests).
        Idempotent: after the first call, both signals are cleared.
        """
        should_rollback = self._iteration_was_incremented or self.pending_turn is not None
        self.pending_turn = None
        self._iteration_was_incremented = False
        if should_rollback and self._iteration > 0:
            self._iteration -= 1

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
        # OSError covers ProcessLookupError (already exited) and permission /
        # ESRCH races; any other exception would be a real bug worth raising.
        with suppress(OSError):
            proc.kill()

    def get_state(self) -> AgentState:
        """Get the current state of the agent.

        Returns:
            Current agent state
        """
        return self._state

    def _build_partial_turn_record(
        self,
        *,
        user_input: str,
        messages: list[Message],
        pending_commands: dict[str, dict[str, Any]],
        assistant_turn_count: int,
        messages_list: list[UserMessageTelemetry | AssistantMessageTelemetry],
        sdk_result_usage: dict[str, Any] | None,
        sdk_result_model_usage: dict[str, Any] | None,
        sdk_result_cost: float | None,
        num_turns: int | None,
        sdk_model_used: str | None,
        sdk_result_summary: ResultSummary | None,
        turn_start_time: float,
        crash_reason: str | None = None,
        sub_agent_usages: list[SubAgentUsage] | None = None,
    ) -> TurnRecord:
        """Build a crashed=True TurnRecord from pre-crash telemetry.

        Message-formatting failure substitutes a placeholder. Other exceptions
        propagate to the caller.
        """
        commands = self._finalize_commands(pending_commands, messages)
        token_usage = self._build_token_usage(messages_list, sdk_result_usage, sdk_result_cost, sdk_result_model_usage)
        duration = time.monotonic() - turn_start_time

        # Broad handler: secondary failure here would defeat partial preservation.
        try:
            agent_output = self._format_messages(messages)
        except Exception as fmt_err:
            logger.warning(
                "Failed to format messages for partial turn record; continuing with placeholder agent_output",
                exc_info=True,
            )
            agent_output = f"<partial record: message formatting failed: {type(fmt_err).__name__}: {fmt_err}>"

        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output=agent_output,
            commands=commands,
            timestamp=datetime.now(),
            duration_seconds=duration,
            token_usage=token_usage,
            model_used=sdk_model_used,
            assistant_turn_count=assistant_turn_count,
            messages=messages_list,
            num_turns=num_turns,
            max_turns_exhausted=False,
            result_summary=sdk_result_summary,
            crashed=True,
            crash_reason=crash_reason,
            sub_agent_usage=sub_agent_usages or [],
        )

    def _finalize_commands(
        self, pending_commands: dict[str, dict[str, Any]], messages: list[Message]
    ) -> list[CommandTelemetry]:
        """Convert pending commands to a finalized list, marking unresolved ones as unknown."""
        commands: list[CommandTelemetry] = []
        unknown_status_count = 0

        for tool_id, cmd_data in pending_commands.items():
            cmd = cmd_data["telemetry"]
            if cmd.result_status is None:
                cmd.result_status = "unknown"
                unknown_status_count += 1
                self._log.warning(
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
            self._log.warning(
                f"Turn completed with {unknown_status_count} command(s) in 'unknown' status. "
                + f"Messages received: [{type_summary}]. "
                + "This may indicate an SDK message type mismatch or agent interruption."
            )

        return commands

    @staticmethod
    def _aggregate_model_usage(model_usage: dict[str, Any] | None) -> TokenUsage | None:
        """Sum the SDK ResultMessage ``model_usage`` into a cumulative TokenUsage.

        ``model_usage`` maps each model id to its cumulative billing for the
        session — ``{model: {inputTokens, outputTokens, cacheReadInputTokens,
        cacheCreationInputTokens, costUSD, ...}}`` (camelCase, unlike ``usage``).
        This is the SDK's authoritative cost breakdown: summed and priced it
        reconciles to ``total_cost_usd`` exactly, and it INCLUDES sub-agent
        consumption (notably cache-creation/input) that the assistant-message
        stream and the ``usage`` snapshot under-report. Returns None when absent
        or empty so the caller can fall back.
        """
        if not isinstance(model_usage, dict) or not model_usage:
            return None
        inp = out = cache_creation = cache_read = 0
        cost = 0.0
        any_cost = False
        for entry in model_usage.values():
            if not isinstance(entry, dict):
                continue
            inp += int(entry.get("inputTokens", 0) or 0)
            out += int(entry.get("outputTokens", 0) or 0)
            cache_creation += int(entry.get("cacheCreationInputTokens", 0) or 0)
            cache_read += int(entry.get("cacheReadInputTokens", 0) or 0)
            c = entry.get("costUSD")
            if c is not None:
                cost += float(c)
                any_cost = True
        return TokenUsage(
            input_tokens=inp,
            output_tokens=out,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
            total_cost_usd=cost if any_cost else None,
        )

    @staticmethod
    def _build_token_usage(
        messages: Sequence[UserMessageTelemetry | AssistantMessageTelemetry],
        sdk_result_usage: dict[str, Any] | None,
        sdk_result_cost: float | None,
        sdk_result_model_usage: dict[str, Any] | None = None,
    ) -> TokenUsage | None:
        """Build the run's cumulative TokenUsage, or None if unavailable.

        Source-of-truth order:

        1. ``ResultMessage.model_usage`` — the SDK's cumulative per-model billing.
           Summed + priced at list rates it equals ``total_cost_usd`` exactly,
           and it captures sub-agent token consumption (especially cache-creation
           and input) that the assistant-message stream and the ``usage`` snapshot
           do NOT — sub-agent emissions are only partially (sometimes never)
           bubbled into the recorded stream. This is authoritative; prefer it.

        2. Per-call telemetry stream (sum) — used when ``model_usage`` is absent
           (e.g. LLMGW/proxy or legacy/mock SDKs). Recorded usage is deduped by
           ``message_id``, so summing is exact when every token-bearing emission
           carries an id; this still beats the ``usage`` snapshot, which
           under-reports the cache-read cascade ~2-3x on multi-call runs.

        3. ``ResultMessage.usage`` snapshot — last resort.

        ``total_cost_usd`` comes from ``model_usage.costUSD`` when present, else
        the ResultMessage ``total_cost_usd`` — the real billed total.
        """
        from_models = ClaudeCodeAgent._aggregate_model_usage(sdk_result_model_usage)
        if from_models is not None:
            if from_models.total_cost_usd is None:
                from_models.total_cost_usd = sdk_result_cost
            return from_models

        assistant_msgs = [m for m in messages if isinstance(m, AssistantMessageTelemetry)]
        token_bearing = [
            m
            for m in assistant_msgs
            if m.input_tokens or m.output_tokens or m.cache_creation_tokens or m.cache_read_tokens
        ]
        # Summing is exact only when every token-bearing emission has an id (so
        # the dedup in communicate() applied and no ResultMessage backfill was
        # mixed in). Otherwise defer to the ResultMessage summary.
        if token_bearing and all(m.message_id for m in token_bearing):
            return TokenUsage(
                input_tokens=sum(m.input_tokens for m in assistant_msgs),
                output_tokens=sum(m.output_tokens for m in assistant_msgs),
                cache_creation_input_tokens=sum(m.cache_creation_tokens for m in assistant_msgs),
                cache_read_input_tokens=sum(m.cache_read_tokens for m in assistant_msgs),
                total_cost_usd=sdk_result_cost,
            )
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

    def _process_plugins(self, plugins: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                        self._log.warning(f"Plugin path contains undefined environment variable ${var_name}: {path}")

                # Expand all env vars in the path, then resolve relative paths
                # against the process cwd (not the sandbox cwd) so plugins are found
                expanded = os.path.expandvars(path)
                processed_plugin["path"] = str(Path(expanded).resolve())

            processed.append(processed_plugin)

        return processed

    def _log_message_raw(self, message: Any, msg_type: str) -> None:
        """Dump an SDK event verbatim, the instant it arrives, when opted in.

        Gated behind the ``CODER_EVAL_RAW_SDK_LOG`` env var (truthy = "1",
        "true", "yes", "on") so normal runs stay quiet. Emits at INFO so it
        shows up in task.log without flipping the whole logger to DEBUG.

        For each event we log, in order:
          * the message type name,
          * the full ``repr(message)`` (untruncated), and
          * a sorted ``key=value`` dump of every public attribute (so token
            fields like ``usage`` / ``model_usage`` / ``total_cost_usd`` are
            visible exactly as the SDK delivered them, even when ``repr`` is
            terse).

        Use this to confirm, first-hand, which message types actually carry
        token usage — e.g. that an Agent tool-result message does *not*, while
        the sibling TaskNotification does.
        """
        flag = os.environ.get("CODER_EVAL_RAW_SDK_LOG", "")
        if flag.strip().lower() not in ("1", "true", "yes", "on"):
            return

        try:
            raw_repr = repr(message)
        except Exception as exc:  # pragma: no cover - defensive
            raw_repr = f"<unreprable: {exc!r}>"

        attrs: dict[str, Any] = {}
        for name in dir(message):
            if name.startswith("_"):
                continue
            try:
                value = getattr(message, name)
            except Exception as exc:  # pragma: no cover - defensive
                value = f"<unreadable: {exc!r}>"
            if callable(value):
                continue
            attrs[name] = value
        attr_dump = "\n".join(f"    {k} = {v!r}" for k, v in sorted(attrs.items()))

        self._log.info(
            "RAW_SDK_EVENT type=%s\n  repr=%s\n  attrs:\n%s",
            msg_type,
            raw_repr,
            attr_dump or "    (none)",
        )

    def _log_message_debug(self, message: Any, msg_type: str) -> None:
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
                    if hasattr(block, "text"):
                        text = str(block.text)[:500]
                        self._log.debug(f">>> ASSISTANT: {text}")
                    else:
                        block_type = type(block).__name__
                        self._log.debug(f">>> ASSISTANT BLOCK ({block_type}): {str(block)[:200]}")
            elif content and isinstance(content, str):
                self._log.debug(f">>> ASSISTANT: {content[:500]}")

        elif msg_type == "UserMessage":
            pass

        elif msg_type == "ResultMessage":
            summary = self._summarize_result(message)
            if summary is not None and summary.is_error:
                fields = {k: v for k, v in summary.model_dump().items() if v not in (None, False, "")}
                rendered = ", ".join(f"{k}={str(v)[:200]}" for k, v in fields.items()) if fields else "(no detail)"
                self._log.debug(f"<<< RESULT [ERROR]: {rendered}")
            else:
                pass

        elif msg_type == "SystemMessage":
            # Most system messages are init or thinking messages.
            # These are noisy — skip the debug dump.
            pass

        elif msg_type == "StreamEvent":
            # StreamEvents drive token-delta capture (see message_delta
            # handler) but are noisy in transcripts — skip the debug dump.
            pass

        else:
            self._log.debug(f"--- {msg_type}: {str(message)[:200]}")

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
    def _extract_sub_agent_usage(message: Any) -> SubAgentUsage | None:
        """Build a SubAgentUsage from an Agent tool-result UserMessage.

        The Agent tool-result rides on ``message.tool_use_result`` — a dict
        carrying the sub-agent's ``agentId``, ``totalToolUseCount``,
        ``totalDurationMs`` and a full ``usage`` breakdown (input / output /
        cache-creation / cache-read). This is the authoritative per-sub-agent
        token source, complete with the cache-read that TaskNotification.usage
        drops.

        Returns None for non-sub-agent tool results (regular Bash/Write/etc.
        results also carry ``tool_use_result`` but no ``agentId``/``usage``).
        The Agent tool_use_id is read from the message's ToolResultBlock so the
        usage can be attributed to the spawning Agent call.
        """
        tur = getattr(message, "tool_use_result", None)
        if not isinstance(tur, dict) or "agentId" not in tur:
            return None
        usage = tur.get("usage")
        if not isinstance(usage, dict):
            return None

        # The spawning Agent tool_use_id lives on the ToolResultBlock, not on
        # tool_use_result itself.
        tool_use_id: str | None = None
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                if _is_tool_result_block(block):
                    tool_use_id = getattr(block, "tool_use_id", None)
                    break

        def _int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        input_tokens = _int(usage.get("input_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        cache_creation = _int(usage.get("cache_creation_input_tokens"))
        cache_read = _int(usage.get("cache_read_input_tokens"))

        return SubAgentUsage(
            tool_use_id=tool_use_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
            total_tokens=input_tokens + output_tokens + cache_creation + cache_read,
            tool_uses=_int(tur.get("totalToolUseCount")),
            duration_ms=_int(tur.get("totalDurationMs")),
            status=tur.get("status") if isinstance(tur.get("status"), str) else None,
        )

    def _resolve_pending_command(
        self,
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
            cmd.result_summary = content_str if content_str else None
            cmd.result_data = ClaudeCodeAgent._try_parse_json_value(content)

            # Wall-clock execution bounds. `execution_completed_at` is now;
            # `execution_started_at` is reconstructed by subtracting the
            # measured monotonic duration. This avoids storing a separate
            # wall-clock start (we don't have one without restructuring
            # pending_commands further) while still giving consumers two
            # explicit timestamps with the right delta.
            cmd.execution_completed_at = datetime.now()
            cmd.execution_started_at = cmd.execution_completed_at - timedelta(milliseconds=duration_ms)

            if is_error:
                cmd.error_message = content_str

                # Permission-blocked tool use is abnormal flow — warn so it
                # surfaces in runs that don't have DEBUG enabled.
                content_lower = content_str.lower()
                if any(
                    phrase in content_lower
                    for phrase in ("permission", "not allowed", "requires approval", "denied", "blocked")
                ):
                    self._log.warning(
                        f"Tool use blocked: {cmd.tool_name} (id={tool_use_id}) "
                        + f"- permission denied. Error: {content_str[:200]}"
                    )

            if tool_use_id in processed_results:
                self._log.debug(f"Multiple results for tool_id={tool_use_id}. Last result wins.")
            processed_results.add(tool_use_id)
        else:
            self._log.warning(
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
    def _is_max_turns_result(summary: ResultSummary | None) -> bool:
        """True iff the captured ResultMessage indicates SDK-side max_turns exhaustion."""
        return summary is not None and summary.subtype == "error_max_turns"

    def _max_turns_short_circuit(self, summary: ResultSummary | None, branch_label: str) -> bool:
        """Fall through error branches to the clean-completion path on error_max_turns."""
        if not self._is_max_turns_result(summary):
            return False
        self._log.debug("%s is error_max_turns; treating as clean turn", branch_label)
        return True

    @staticmethod
    def _summarize_result(msg: Message) -> ResultSummary | None:
        """Build a ``ResultSummary`` from an SDK ResultMessage, or None.

        Returns None only when ``msg`` lacks the SDK ResultMessage shape
        (``session_id`` + ``usage``). For real ResultMessages the SDK
        always provides ``subtype`` (it's a required dataclass field), so
        any missing/non-string value is treated as ``"unknown"`` rather
        than silently disabling the summary downstream.
        """
        if not _is_sdk_result_message(msg):
            return None
        subtype = getattr(msg, "subtype", None)
        stop_reason = getattr(msg, "stop_reason", None)
        result = getattr(msg, "result", None)
        return ResultSummary(
            is_error=bool(getattr(msg, "is_error", False)),
            subtype=subtype if isinstance(subtype, str) else "unknown",
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
            result=result if isinstance(result, str) else None,
        )

    @staticmethod
    def _format_error_summary(summary: ResultSummary | None) -> str | None:
        """Format an errored ``ResultSummary`` for surfacing to the user.

        Prefers free-form ``result`` text; falls back to the
        ``subtype``/``stop_reason`` classification when ``result`` is
        unset (which is the common shape on hard CLI crashes). Returns
        None when there is nothing useful to surface, so callers can
        decide whether to fall back to stderr.
        """
        if summary is None or not summary.is_error:
            return None
        if summary.result:
            return summary.result[:200]
        parts = [p for p in (summary.subtype, summary.stop_reason) if p]
        if parts:
            return f"Result[is_error=True]: {' / '.join(parts)}"
        return None

    def _format_messages(self, messages: list[Message]) -> str:
        # isinstance, not type-name equality: SystemMessage subclasses must hit
        # the SystemMessage arm per their drop-in contract. Unknown types emit
        # the bare tag only — including a truncated ``__repr__`` can leak
        # unmatched braces into the transcript persisted to ``task.json``.
        formatted_parts = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                continue

            if isinstance(msg, UserMessage):
                continue

            if isinstance(msg, AssistantMessage):
                content = getattr(msg, "content", "")
                if isinstance(content, list):
                    for block in content:
                        text = getattr(block, "text", None)
                        if text:
                            formatted_parts.append(f"[ASSISTANT] {text}")
                elif content:
                    formatted_parts.append(f"[ASSISTANT] {content}")
                continue

            if isinstance(msg, ResultMessage):
                result_text = getattr(msg, "result", "") or ""
                is_error = getattr(msg, "is_error", False)
                status = "ERROR" if is_error else "SUCCESS"
                formatted_parts.append(f"[RESULT - {status}] {result_text}")
                continue

            # StreamEvent isn't exported by the SDK — duck-type on ``type``.
            event_type = getattr(msg, "type", None)
            if event_type == "tool_use":
                tool_name = getattr(msg, "name", "unknown")
                formatted_parts.append(f"[TOOL USE] {tool_name}")
                continue

            type_name = type(msg).__name__
            # StreamEvent is a known SDK type used for token-delta capture
            # elsewhere; don't surface it as an "unhandled" warning here.
            if type_name == "StreamEvent":
                continue
            if type_name not in self._warned_unknown_types:
                self._warned_unknown_types.add(type_name)
                self._log.warning(
                    "Unhandled SDK message type %s in _format_messages — "
                    "extend the isinstance chain when the SDK adds new types.",
                    type_name,
                )
            formatted_parts.append(f"[{type_name}]")

        return "\n".join(formatted_parts) if formatted_parts else "[No output]"

    def _update_state_from_messages(self, messages: list[Message]) -> None:
        """Update agent state based on received messages.

        Args:
            messages: List of messages from the agent (SDK objects)
        """
        # Check for explicit error messages (use getattr for safe access).
        # ResultMessage.is_error is intentionally NOT a state-change trigger —
        # the agent may recover from a tool error on a later turn.
        for msg in messages:
            if getattr(msg, "error", None):
                self._state = AgentState.ERROR
                return

        # If no errors, agent is working normally
        self._state = AgentState.WORKING
