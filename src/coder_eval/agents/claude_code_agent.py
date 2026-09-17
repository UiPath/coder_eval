"""Claude Code agent implementation using the Claude Agent SDK."""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    Message,
    ProcessError,
    TaskNotificationMessage,
    query,
)

# Private SDK import: the public `query()` API does not expose the subprocess
# handle, which the watchdog needs to SIGKILL on timeout. If this breaks on an
# SDK upgrade the watchdog loses its kill target and timeouts stop being
# enforced at the agent layer.
# Rationale: .claude/notes/agents.md § The threaded watchdog
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

# SystemPromptPreset is not re-exported from the SDK root, so claude_agent_sdk.types
# is the only import route (same treatment as evaluation/verdict_tool.py).
from claude_agent_sdk.types import SdkPluginConfig, SystemPromptPreset

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._logging import PrefixedAdapter, log_raw_sdk_event
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import WatchdogFired, run_with_watchdog
from coder_eval.config import settings
from coder_eval.errors import format_timeout_reason
from coder_eval.formatting import format_messages, format_payload
from coder_eval.models import (
    CANONICAL_TOOL_NAMES,
    AgentKind,
    ApiRoute,
    BedrockRoute,
    ClaudeCodeAgentConfig,
    ContentBlock,
    DirectRoute,
    Enforcement,
    HarnessContract,
    LiteLLMRoute,
    PermissionMode,
    ResultSummary,
    SystemPromptSemantics,
    TimingBasis,
    TokenUsage,
    ToolNameMap,
    TranscriptMessage,
    UsageGranularity,
    to_bedrock_inference_profile,
)
from coder_eval.models import (
    AssistantMessage as AssistantMessageTelemetry,
)
from coder_eval.orchestration.plugin_staging import staged_plugin_dirs
from coder_eval.pricing import price_turn
from coder_eval.streaming.callbacks import StreamCallback
from coder_eval.streaming.emitter import Generation, TurnEmitter, TurnOutcome
from coder_eval.streaming.events import (
    AgentEndStatus,
    StopReason,
    ToolEndStatus,
    TurnEndStatus,
    end_status_for,
)
from coder_eval.timing import close_window
from coder_eval.utils import dump_dataclass


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

    The API reports output_tokens per API *call*, but the CLI surfaces one call
    as several per-block emissions, so the total is apportioned by a
    content-length proxy rather than dumped on the first block.

    Largest-remainder (Hamilton), so the returned integers sum EXACTLY to
    ``total`` and per-message output stays reconcilable with the aggregate. Falls
    back to an even split when all weights are zero.
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


def _tool_result_text(content: Any) -> str:
    """Best-effort text of a ToolResultBlock's content (str, or list of text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _is_task_notification(message: Any) -> bool:
    """Check if message is a TaskNotificationMessage (sub-agent terminal event).

    It carries ``session_id`` + ``usage``, so it would otherwise be misread as
    the final ResultMessage — hence this guard, checked BEFORE
    ``_is_sdk_result_message``. Identified by the
    SDK type or ``subtype`` rather than attribute-presence sniffing, so it cannot
    misfire on a mock; the ``subtype`` fallback is what lets a duck-typed mock be
    recognized.
    """
    return isinstance(message, TaskNotificationMessage) or getattr(message, "subtype", None) == "task_notification"


def _is_sdk_result_message(message: Any) -> bool:
    """Check if message is the SDK's final ResultMessage (with usage/cost data).

    Distinct from ToolResultBlock which has tool_use_id, and from
    TaskNotificationMessage which also carries session_id + usage (excluded).
    """
    return hasattr(message, "session_id") and hasattr(message, "usage") and not _is_task_notification(message)


_JSON_START_SEARCH_LIMIT = 200


class _ClaudeDecoder:
    """Per-turn decoder: receives SDK messages for one ``communicate`` call and reports them to the emitter.

    One inner turn per message id; a message with ``parent_tool_use_id`` is a sub-agent's, so
    its turn, generation and tools are nested under that id. ``sdk_model_used`` follows the
    main thread only.
    """

    def __init__(self, agent: "ClaudeCodeAgent", emitter: TurnEmitter, *, effective_model: str | None) -> None:
        self._agent = agent
        self.emitter = emitter
        self.effective_model = effective_model
        self.log = agent._log
        # Set by the in-loop deadline break OR the watchdog callback.
        self.timeout_hit = False
        self.stop_reason: StopReason | None = None
        self.messages: list[Message] = []

        self.opened_tools: dict[str, str] = {}
        self.transcript: list[AssistantMessageTelemetry] = []
        self.processed_results: set[str] = set()

        self.last_event_wall: datetime = emitter.now()
        self.first_output_seen = False

        self.sdk_result_usage: dict[str, Any] | None = None
        self.sdk_result_model_usage: dict[str, Any] | None = None
        self.sdk_result_cost: float | None = None
        self.num_turns: int | None = None
        self.sdk_result_summary: ResultSummary | None = None
        self.sdk_model_used: str | None = None
        # The last model each sub-agent streamed, keyed by its spawning tool_use_id.
        self.subagent_models: dict[str, str] = {}

        self.pending_delta_output_tokens: int | None = None
        self.current_stream_message_id: str | None = None
        self.emissions_by_id: dict[str, list[AssistantMessageTelemetry]] = {}
        self.emission_proxies_by_id: dict[str, list[int]] = {}
        self.seen_message_ids: set[str] = set()
        self.last_message: AssistantMessageTelemetry | None = None
        self.last_message_had_id = False

        self.assistant_turn_count = 0
        self.current_turn_id: str | None = None
        # Tokens each message id already reported on a TurnEndEvent: an id can
        # resume after another id's emission, and must report only what is new.
        self.reported_tokens_by_id: dict[str, TokenUsage] = {}

    def turn_tokens(self, turn_id: str) -> TokenUsage | None:
        """Best-effort per-turn tokens, summed over that call's block emissions."""
        records = self.emissions_by_id.get(turn_id)
        if not records:
            return None
        total = TokenUsage()
        for rec in records:
            total = total + TokenUsage(
                uncached_input_tokens=rec.input_tokens,
                output_tokens=rec.output_tokens,
                cache_creation_input_tokens=rec.cache_creation_tokens,
                cache_read_input_tokens=rec.cache_read_tokens,
            )
        return total

    def unreported_turn_tokens(self, turn_id: str) -> TokenUsage | None:
        """The part of ``turn_tokens`` no earlier ``TurnEndEvent`` for this id reported."""
        total = self.turn_tokens(turn_id)
        if total is None:
            return None
        previous = self.reported_tokens_by_id.get(turn_id)
        self.reported_tokens_by_id[turn_id] = total
        if previous is None:
            return total
        return TokenUsage(
            uncached_input_tokens=total.uncached_input_tokens - previous.uncached_input_tokens,
            output_tokens=total.output_tokens - previous.output_tokens,
            cache_creation_input_tokens=total.cache_creation_input_tokens - previous.cache_creation_input_tokens,
            cache_read_input_tokens=total.cache_read_input_tokens - previous.cache_read_input_tokens,
        )

    def __call__(self, message: Message) -> None:
        """Record the raw message and route it to its per-kind handler.

        ORDER IS LOAD-BEARING: ``_is_sdk_result_message`` before
        ``_is_user_message``, and the TaskNotification guard before both.
        """
        self.messages.append(message)
        log_raw_sdk_event(self.log, repr_target=message, type=type(message).__name__)

        if _is_assistant_message(message):
            self.on_assistant_message(message)
        elif _is_task_notification(message):
            # Its per-sub-agent usage is LOSSY and is captured from the Agent tool
            # result instead; the branch only keeps it from reading as a result.
            pass
        elif _is_sdk_result_message(message):
            self.on_result_message(message)
        elif isinstance(getattr(message, "event", None), dict):
            self.on_stream_event(message)
        elif _is_user_message(message):
            self.on_user_message(message)

    def _switch_inner_turn(self, turn_id: str, model: str | None, parent: str | None) -> None:
        if turn_id == self.current_turn_id:
            return
        if self.current_turn_id is not None and self.emitter.inner_turn_open:
            self.emitter.end_inner_turn(tokens=self.unreported_turn_tokens(self.current_turn_id))
        self.current_turn_id = turn_id
        self.emitter.begin_inner_turn(turn_id, model, parent_tool_id=parent)

    def on_assistant_message(self, message: Message) -> None:
        """Open the message's inner turn and tools, then add its generation."""
        arrival = self.emitter.now()
        mark = self.last_event_wall
        raw_parent = getattr(message, "parent_tool_use_id", None)
        parent = raw_parent if isinstance(raw_parent, str) else None
        model_attr = getattr(message, "model", None)
        message_model = model_attr if isinstance(model_attr, str) else None
        if message_model is not None:
            if parent is None:
                self.sdk_model_used = message_model
            else:
                self.subagent_models[parent] = message_model
        model = self.sdk_model_used if parent is None else message_model
        self.assistant_turn_count += 1

        raw_mid = getattr(message, "message_id", None)
        message_id = raw_mid if isinstance(raw_mid, str) else None
        self._switch_inner_turn(message_id or f"turn-{self.assistant_turn_count}", model, parent)

        blocks, proxy = self._blocks(getattr(message, "content", None), parent)
        tokens, reasoning = self._emission_tokens(getattr(message, "usage", None) or {}, message_id)
        stop_reason = getattr(message, "stop_reason", None)
        # The RAW window, opened at the mark, since this stream carries no
        # per-emission item start to pull the window open to.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        (record,) = self.emitter.add_generation(
            message_id=message_id,
            window=close_window(mark=mark, now=arrival),
            parts=[
                Generation(
                    blocks=blocks,
                    tokens=tokens,
                    reasoning_tokens=reasoning,
                    stop_reason=stop_reason if isinstance(stop_reason, str) else None,
                )
            ],
            model=model,
            parent_tool_id=parent,
        )
        self.transcript.append(record)
        if message_id is not None:
            self.emissions_by_id.setdefault(message_id, []).append(record)
            self.emission_proxies_by_id.setdefault(message_id, []).append(proxy)
        self.last_message = record
        self.last_event_wall = arrival

    def _blocks(self, content: Any, parent: str | None) -> tuple[list[ContentBlock], int]:
        """The message's content blocks, opening each tool; also its content-length proxy."""
        blocks: list[ContentBlock] = []
        proxy = 0
        if not isinstance(content, list):
            return blocks, proxy
        for block in content:
            sequence = len(blocks)
            if _is_tool_use_block(block):
                params = block.input if isinstance(block.input, dict) else {"raw": block.input}
                proxy += len(str(getattr(block, "name", "") or "")) + len(json.dumps(params, default=str))
                blocks.append(ContentBlock(block_type="tool_use", sequence=sequence, tool_use_id=block.id))
                self.opened_tools[block.id] = block.name
                self.emitter.open_tool(block.id, block.name, params, parent_tool_id=parent, generation_completed=True)
            elif _is_thinking_block(block):
                thinking = getattr(block, "thinking", None)
                if thinking:
                    proxy += len(str(thinking))
                blocks.append(
                    ContentBlock(
                        block_type="thinking",
                        sequence=sequence,
                        thinking=str(thinking) if thinking else None,
                        signature=getattr(block, "signature", None),
                    )
                )
            elif _is_text_block(block):
                text = str(block.text)
                proxy += len(text)
                blocks.append(ContentBlock(block_type="text", sequence=sequence, text=text))
                self.emitter.text(text, parent_tool_id=parent)
        return blocks, proxy

    def _emission_tokens(self, usage: dict[str, Any], message_id: str | None) -> tuple[TokenUsage, int]:
        """This emission's tokens; a repeated message id's emission carries none."""
        duplicate = message_id is not None and message_id in self.seen_message_ids
        self.last_message_had_id = message_id is not None
        if message_id is not None:
            self.seen_message_ids.add(message_id)
        if duplicate:
            return TokenUsage(), 0
        if self.pending_delta_output_tokens is not None:
            output = self.pending_delta_output_tokens
        else:
            output = int(usage.get("output_tokens", 0) or 0)
        self.pending_delta_output_tokens = None
        return (
            TokenUsage(
                uncached_input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=output,
                cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
                cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            ),
            int(usage.get("reasoning_tokens", 0) or 0),
        )

    def on_result_message(self, message: Message) -> None:
        """Capture the SDK ResultMessage usage/cost/session + the id-less backfill."""
        agent = self._agent
        self.sdk_result_usage = getattr(message, "usage", None)
        self.sdk_result_model_usage = getattr(message, "model_usage", None)
        self.sdk_result_cost = getattr(message, "total_cost_usd", None)
        self.num_turns = getattr(message, "num_turns", None)
        self.sdk_result_summary = agent._summarize_result(message)
        new_session_id = getattr(message, "session_id", None)
        if self.sdk_result_summary is not None and self.sdk_result_summary.is_error:
            self.log.debug("is_error ResultMessage; not advancing session_id (kept %s)", agent._session_id)
        else:
            if new_session_id != agent._session_id:
                self.log.debug("session_id changed: %s -> %s", agent._session_id, new_session_id)
            agent._session_id = new_session_id

        # Retro-populate the last generation from ResultMessage.usage when
        # per-message capture was not in effect for it (no message_id).
        last, usage = self.last_message, self.sdk_result_usage
        if last is not None and usage and not self.last_message_had_id:
            last.input_tokens = int(usage.get("input_tokens", 0) or 0)
            last.output_tokens = int(usage.get("output_tokens", 0) or 0)
            last.cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
            last.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
            last.reasoning_tokens = int(usage.get("reasoning_tokens", 0) or 0)

    def _seed_first_generation_window(self) -> None:
        """Move the first window's mark to the first observed model output, ONCE per turn.

        Re-seeding on every ``message_start`` would stop the windows tiling. A turn with
        no ``message_start`` (including ``-D agent.sdk_options.include_partial_messages=false``)
        keeps the turn-entry mark and clamps to ``0.0``, silently.

        Rationale: .claude/notes/agents.md § First-generation window seeding
        """
        if self.first_output_seen:
            return
        self.first_output_seen = True
        self.last_event_wall = self.emitter.now()

    def on_stream_event(self, message: Message) -> None:
        """Recover cumulative output_tokens from raw ``message_start`` / ``message_delta`` events."""
        evt: dict[str, Any] = getattr(message, "event", None) or {}
        evt_type = evt.get("type")
        if evt_type == "message_start":
            self._seed_first_generation_window()
            mid = (evt.get("message") or {}).get("id")
            self.current_stream_message_id = mid if isinstance(mid, str) else None
        elif evt_type == "message_delta":
            usage = evt.get("usage") or {}
            ot = usage.get("output_tokens")
            if isinstance(ot, int):
                records: list[AssistantMessageTelemetry] | None = None
                proxies: list[int] = []
                if self.current_stream_message_id is not None:
                    records = self.emissions_by_id.get(self.current_stream_message_id)
                    proxies = self.emission_proxies_by_id.get(self.current_stream_message_id, [])
                if records:
                    shares = _distribute_output_tokens(ot, proxies)
                    for record, share in zip(records, shares, strict=False):
                        record.output_tokens = share
                else:
                    self.pending_delta_output_tokens = ot

    def on_user_message(self, message: Message) -> None:
        """Add a sub-agent's terminal generation, then close each tool the message resolves.

        The generation mark is DELIBERATELY NOT advanced here: leaving it where
        ``on_assistant_message`` put it is what makes the windows tile.

        Rationale: .claude/notes/agents.md § Per-harness generation marks
        """
        terminal = self._agent._subagent_terminal_part(message)
        if terminal is not None:
            tool_use_id, part = terminal
            self.transcript.append(
                self.emitter.add_unmeasured_generation(
                    message_id=f"subagent-{tool_use_id}",
                    part=part,
                    model=self.subagent_models.get(tool_use_id, self.sdk_model_used),
                    parent_tool_id=tool_use_id,
                )
            )
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return
        for block in content:
            if _is_tool_result_block(block):
                self._close_tool(block.tool_use_id, bool(getattr(block, "is_error", False)), block.content)

    def _close_tool(self, tool_use_id: str, is_error: bool, content: Any) -> None:
        if tool_use_id in self.processed_results:
            self.log.debug("Multiple results for tool_id=%s; the first result stands.", tool_use_id)
            return
        self.processed_results.add(tool_use_id)
        agent = self._agent
        content_str = str(content) if content is not None else ""
        status = agent._tool_end_status(is_error, content)
        if tool_use_id not in self.opened_tools:
            self.log.warning(
                "Tool result received for unknown tool_use_id=%s. No matching ToolUseBlock found.", tool_use_id
            )
            self.emitter.close_tool(tool_use_id, status=status, summary=format_payload(content))
            return
        if status is ToolEndStatus.PERMISSION_DENIED:
            self.log.warning(
                "Tool use blocked: %s (id=%s) - permission denied. Error: %s",
                self.opened_tools[tool_use_id],
                tool_use_id,
                content_str[:200],
            )
        self.emitter.close_tool(
            tool_use_id,
            status=status,
            summary=content_str or None,
            error=content_str if is_error else None,
            result_data=agent._try_parse_json_value(content),
        )

    def _finalize_token_usage(self) -> TokenUsage:
        """The turn's cumulative TokenUsage, repriced for LiteLLM.

        Rationale: .claude/notes/agents.md § Cost: the stream versus the rate card
        """
        agent = self._agent
        usage = (
            agent._build_token_usage(
                self.transcript,
                self.sdk_result_usage,
                self.sdk_result_cost,
                self.sdk_result_model_usage,
                self.effective_model,
            )
            or TokenUsage()
        )
        if isinstance(agent.route, LiteLLMRoute):
            agent._reprice_for_litellm(usage, self.effective_model)
        return usage

    def end(self, status: AgentEndStatus, *, reason: str | None = None) -> TurnOutcome:
        """Close the open inner turn with its unreported tokens and end the turn."""
        if self.current_turn_id is not None and self.emitter.inner_turn_open:
            self.emitter.end_inner_turn(
                TurnEndStatus(status.value), tokens=self.unreported_turn_tokens(self.current_turn_id)
            )
        unresolved = sorted(set(self.opened_tools) - self.processed_results)
        if unresolved:
            counts: dict[str, int] = {}
            for msg in self.messages:
                counts[type(msg).__name__] = counts.get(type(msg).__name__, 0) + 1
            self.log.warning(
                "Turn ended with %d tool call(s) without a result (%s). Messages received: [%s].",
                len(unresolved),
                ", ".join(unresolved),
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
            )
        usage = self._finalize_token_usage()
        try:
            agent_output = self._agent._format_messages(self.messages)
        except Exception as fmt_err:
            logger.warning("Failed to format messages for AgentEndEvent; using placeholder", exc_info=True)
            agent_output = f"<partial record: message formatting failed: {type(fmt_err).__name__}: {fmt_err}>"
        if status is AgentEndStatus.CRASHED or status is AgentEndStatus.TIMEOUT:
            return self.emitter.fail(
                status,
                reason or status.value,
                usage=usage,
                agent_output=agent_output,
                model_used=self.sdk_model_used,
                assistant_turn_count=self.assistant_turn_count,
                num_turns=self.num_turns,
            )
        if self.sdk_result_summary is None:
            # A stop broke the loop before any ResultMessage: the emitter's default summary applies.
            return self.emitter.finalize(
                status,
                usage=usage,
                agent_output=agent_output,
                model_used=self.sdk_model_used,
                assistant_turn_count=self.assistant_turn_count,
                num_turns=self.num_turns,
            )
        return self.emitter.finalize(
            status,
            usage=usage,
            agent_output=agent_output,
            model_used=self.sdk_model_used,
            assistant_turn_count=self.assistant_turn_count,
            num_turns=self.num_turns,
            result_summary=self.sdk_result_summary,
        )


@AgentRegistry.register(AgentKind.CLAUDE_CODE, ClaudeCodeAgentConfig)
class ClaudeCodeAgent(Agent[ClaudeCodeAgentConfig]):
    """Implementation of the Agent interface for Claude Code using the SDK."""

    # The message loop has a between-messages guard where `should_stop` runs.
    contract = HarnessContract(
        system_prompt=Enforcement.ENFORCED,
        system_prompt_semantics="append",
        plugin_skills=Enforcement.ENFORCED,
        permission_mode=Enforcement.ENFORCED,
        allowed_tools=Enforcement.ENFORCED,
        disallowed_tools=Enforcement.ENFORCED,
        cooperative_stop=True,
        usage_granularity=UsageGranularity.GENERATION,
        timing_basis=TimingBasis.TURN_CLOCK,
        permission_modes=frozenset(PermissionMode),
    )
    tool_names = ToolNameMap(names={name: (name,) for name in CANONICAL_TOOL_NAMES}, mcp_names=True)

    # One warning per agent for a replace-mode config with no prompt: the resolver
    # runs on every query, and a per-turn repeat would bury the rest of task.log.
    _warned_prompt_mode_downgrade: bool = False

    # Narrowed from the base: __init__ defaults a missing route to DirectRoute.
    route: ApiRoute

    def __init__(
        self,
        config: ClaudeCodeAgentConfig,
        route: ApiRoute | None = None,
        *,
        instance_name: str = "coder",
        extra_mcp_servers: dict[str, Any] | None = None,
        cost_log_tags: dict[str, str] | None = None,
    ):
        """Initialize the Claude Code agent.

        Args:
            config: Agent configuration
            route: API routing configuration. If None, uses DirectRoute.
            instance_name: Short label prefixing this instance's log records, so
                the coding agent and the user-simulator are distinguishable in
                ``task.log`` when both run in the same process.
            extra_mcp_servers: Runtime-only in-process MCP servers merged into
                ``ClaudeAgentOptions.mcp_servers``. NOT sourced from YAML —
                ``mcp_servers`` is explicitly denied via ``sdk_options`` for
                security. The judge criterion is the only caller today.
            cost_log_tags: LiteLLM-only correlation headers stamped into
                ``ANTHROPIC_CUSTOM_HEADERS`` so a proxy-side cost callback can
                attribute each call's real cost back to this run. None on
                Direct/Bedrock.
        """
        super().__init__(config, route or DirectRoute(), cost_log_tags=cost_log_tags)
        self._extra_mcp_servers = extra_mcp_servers or {}
        self.client: ClaudeSDKClient | None = None
        self.working_directory: Path | None = None
        self._sdk_options_dump: dict[str, Any] | None = None
        self._session_id: str | None = None
        # Held only while a communicate() call is in flight, so kill() can reach
        # the CLI subprocess when the SDK swallows asyncio cancellation.
        self._active_transport: SubprocessCLITransport | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        self._plugin_root: Path | None = None
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})
        # Dedupe "unhandled SDK message type" warnings: _format_messages runs
        # many times per task and the types are stable for a session.
        self._warned_unknown_types: set[str] = set()

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        plugin_root: Path | None = None,
    ) -> None:
        """Initialize and start the Claude Code agent.

        Args:
            working_directory: Path to the working directory
            env_path_prepend: Absolute directories to prepend to PATH for the SDK
                subprocess (typically the resolved ``SandboxConfig.mock_path_dirs``).
            plugin_tools_dir: Canonical ``node_modules/@uipath`` to export as
                ``PLUGIN_TOOLS_DIR``. An external env-var pin still wins.
            plugin_root: The staged plugin root; each ``plugins/<name>`` is loaded as one local plugin.
        """
        self.working_directory = Path(working_directory)
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        self._plugin_root = plugin_root
        self._state = AgentState.WORKING
        # Note: Client is created per-communication to avoid transport issues

    @staticmethod
    def _build_sdk_env(
        route: ApiRoute,
        path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        cost_log_tags: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], str | None]:
        """Build SDK environment variables and resolve effective model for the given route.

        Args:
            route: API routing configuration.
            path_prepend: Directories prepended (in order) to PATH so they shadow
                same-named binaries. Resolved by the sandbox manager; the agent
                does no filesystem inspection of its own.
            plugin_tools_dir: Fallback ``PLUGIN_TOOLS_DIR``; an external one wins.
            cost_log_tags: LiteLLM-only correlation headers stamped
                (newline-separated ``Name: Value``) into
                ``ANTHROPIC_CUSTOM_HEADERS``. Values MUST be single-line ASCII,
                validated here — the header block is CR/LF-delimited.

        Returns:
            Tuple of (env_vars_dict, model_override_or_None).
        """
        base_env: dict[str, str] = {}
        if path := os.environ.get("PATH"):
            base_env["PATH"] = path

        if path_prepend:
            prefix = os.pathsep.join(path_prepend)
            base_env["PATH"] = f"{prefix}{os.pathsep}{base_env.get('PATH', '')}"

        # Pin plugin discovery for the SDK subprocess; external env wins so
        # operators can override.
        if tools_dir := os.environ.get("PLUGIN_TOOLS_DIR"):
            base_env["PLUGIN_TOOLS_DIR"] = tools_dir
        elif plugin_tools_dir:
            base_env["PLUGIN_TOOLS_DIR"] = plugin_tools_dir

        match route:
            case BedrockRoute() as br:
                # `or ""` rather than asserting: reaching a BedrockRoute implies the
                # token was confirmed upstream, and this is a pure env-dict builder
                # with no error-reporting seam. Do not "fix" to an assert without
                # deciding how the AssertionError should surface.
                env: dict[str, str] = {
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "AWS_BEARER_TOKEN_BEDROCK": settings.aws_bearer_token_bedrock or "",
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

            case DirectRoute() as dr:
                # Neutralize inherited Bedrock creds: the CLI auto-selects Bedrock
                # DIRECT whenever AWS_BEARER_TOKEN_BEDROCK is inherited, so an
                # explicit `route: direct` would otherwise silently spend the
                # operator's Bedrock token instead of ANTHROPIC_API_KEY.
                env = {
                    "AWS_BEARER_TOKEN_BEDROCK": "",
                    "CLAUDE_CODE_USE_BEDROCK": "",
                }
                if dr.model:
                    env["ANTHROPIC_MODEL"] = dr.model
                return {**base_env, **env}, dr.model

            case LiteLLMRoute() as cr:
                # Point the SDK at the custom Anthropic-compatible endpoint. These
                # override any inherited value: the SDK merges
                # {**os.environ, ..., **options.env} at spawn.
                env = {
                    "ANTHROPIC_BASE_URL": settings.litellm_base_url or "",
                    "ANTHROPIC_AUTH_TOKEN": settings.litellm_auth_token or "",
                    # Auth here is the bearer ANTHROPIC_AUTH_TOKEN; a stray
                    # x-api-key would conflict with the gateway's key auth.
                    "ANTHROPIC_API_KEY": "",
                    # The attribution metadata Claude Code attaches is rejected by
                    # Bedrock's requestMetadata regex (HTTP 400) once LiteLLM
                    # forwards it.
                    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
                    # Same auto-selection as the DirectRoute arm, and that token IS
                    # forwarded into docker task containers by the default
                    # env-passthrough allowlist — so without blanking it the CLI
                    # bypasses the LiteLLM proxy entirely. Empty is falsy there.
                    "AWS_BEARER_TOKEN_BEDROCK": "",
                    "CLAUDE_CODE_USE_BEDROCK": "",
                }
                if cr.model:
                    env["ANTHROPIC_MODEL"] = cr.model
                if cr.small_model:
                    env["ANTHROPIC_SMALL_FAST_MODEL"] = cr.small_model
                if cost_log_tags:
                    # SANITIZE AT THE SEAM. x-ce-task-id carries the author-defined
                    # task_id/variant_id, and Claude Code forwards this block
                    # verbatim — so a CR/LF would inject extra headers into every
                    # SDK->proxy request (forged cost attribution, or an auth /
                    # routing override). Non-ASCII breaks the latin-1 encoding.
                    # Reject both loudly rather than emit them.
                    for name, value in cost_log_tags.items():
                        joined = f"{name}{value}"
                        if "\r" in joined or "\n" in joined or not joined.isascii():
                            raise ValueError(f"cost_log_tags {name!r} must be single-line ASCII (got {value!r})")
                    env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(f"{k}: {v}" for k, v in cost_log_tags.items())
                return {**base_env, **env}, cr.model

        raise AssertionError(f"Unhandled route type: {type(route).__name__}")

    def _resolve_effective_model(
        self, config_model: str | None, env: dict[str, str], route_model: str | None
    ) -> str | None:
        """Resolve the effective model and sync subprocess env on Bedrock.

        Precedence: ``config_model`` wins over the route default. On Bedrock a
        bare alias is auto-qualified with the region's inference-profile prefix so
        one value works across regions, and the resolved value is written to
        ``ANTHROPIC_MODEL`` so the subprocess sees the same model as the options.
        """
        if isinstance(self.route, BedrockRoute):
            if config_model is not None:
                config_model = to_bedrock_inference_profile(config_model, self.route.region)
            effective = config_model or route_model
            if effective:
                env["ANTHROPIC_MODEL"] = effective
            return effective
        if isinstance(self.route, LiteLLMRoute):
            # Same env-sync, but verbatim: the gateway maps the id itself.
            effective = config_model or route_model
            if effective:
                env["ANTHROPIC_MODEL"] = effective
            return effective
        return config_model or route_model

    async def communicate(
        self,
        user_input: str,
        *,
        iteration: int,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        should_stop: Callable[[], StopReason | None] | None = None,
    ) -> TurnOutcome:
        """Run one turn; see ``Agent.communicate``.

        ``timeout`` arms a watchdog that force-kills the CLI subprocess: the SDK's anyio
        task groups suppress cooperative cancellation. ``should_stop`` is polled after
        each dispatched message.
        """
        if not self.working_directory:
            raise RuntimeError("Agent not started. Call start() first.")
        assert self.config.type is not None, "ClaudeCodeAgent requires AgentConfig.type to be set before communicate()"
        task_id = str(self.config.type)  # str() so a plugin subclass with a non-enum kind also works

        stderr_lines: list[str] = []
        try:
            options, transport, effective_model = self._build_claude_query(
                user_input, iteration, timeout, stderr_lines.append
            )
        except Exception as e:
            emitter = self._open_emitter(
                prompt=user_input, iteration=iteration, model=None, task_id=task_id, stream_callback=stream_callback
            )
            emitter.begin()
            decoder = _ClaudeDecoder(self, emitter, effective_model=None)
            return self._fail(decoder, AgentEndStatus.CRASHED, self._crash_message(e, None, stderr_lines))

        emitter = self._open_emitter(
            prompt=user_input,
            iteration=iteration,
            model=effective_model,
            task_id=task_id,
            stream_callback=stream_callback,
        )
        emitter.begin()
        decoder = _ClaudeDecoder(self, emitter, effective_model=effective_model)
        deadline = time.monotonic() + timeout if timeout is not None else None

        def _on_turn_timeout() -> None:
            decoder.timeout_hit = True
            # The CAPTURED transport, so a stale watchdog cannot kill a later turn's process.
            self._kill_transport(transport)

        # Only when one was built, so mocks with strict (prompt, options) signatures keep working.
        query_kwargs: dict[str, Any] = {"prompt": user_input, "options": options}
        if transport is not None:
            query_kwargs["transport"] = transport
            self._active_transport = transport
        timed_out = format_timeout_reason(timeout or 0)
        try:
            self._log.debug("Starting agent query stream...")
            await run_with_watchdog(
                self._pump_messages(decoder, query_kwargs, deadline, should_stop),
                timeout_seconds=timeout,
                on_timeout=_on_turn_timeout,
                label=f"Turn timeout ({timeout:g}s)" if timeout else "turn_timeout",
            )
            self._log.debug("Agent query stream ended")
        except WatchdogFired:
            return self._fail(decoder, AgentEndStatus.TIMEOUT, timed_out)
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling() == 0:
                # Not a cancel from outside: the SDK raised it inside the turn body.
                if self._timed_out(decoder.timeout_hit, deadline):
                    return self._fail(decoder, AgentEndStatus.TIMEOUT, timed_out)
                return self._fail(decoder, AgentEndStatus.CRASHED, "Communication with agent failed: cancelled")
            self._state = AgentState.ERROR
            decoder.end(AgentEndStatus.CRASHED, reason="turn cancelled")
            raise
        except Exception as e:
            # A watchdog SIGKILL surfaces as ProcessError (exit -9) or a generic
            # Exception; both the flag and the wall clock, in case the flip races this catch.
            if self._timed_out(decoder.timeout_hit, deadline):
                return self._fail(decoder, AgentEndStatus.TIMEOUT, timed_out)
            label = f"ProcessError(exit={e.exit_code})" if isinstance(e, ProcessError) else "Generic Exception"
            if not self._max_turns_short_circuit(decoder.sdk_result_summary, label):
                return self._fail(
                    decoder, AgentEndStatus.CRASHED, self._crash_message(e, decoder.sdk_result_summary, stderr_lines)
                )
        finally:
            self._active_transport = None

        # Only the flag here, never the wall clock: a drift during post-loop
        # cleanup would misclassify a successful turn as a timeout.
        if decoder.timeout_hit:
            return self._fail(decoder, AgentEndStatus.TIMEOUT, timed_out)
        self._update_state_from_messages(decoder.messages)
        status = end_status_for(decoder.stop_reason) if decoder.stop_reason is not None else AgentEndStatus.COMPLETED
        return decoder.end(status)

    def _fail(self, decoder: _ClaudeDecoder, status: AgentEndStatus, reason: str) -> TurnOutcome:
        self._state = AgentState.ERROR
        return decoder.end(status, reason=reason)

    def _crash_message(self, error: Exception, summary: ResultSummary | None, stderr_lines: list[str]) -> str:
        """The crash reason for ``error``: the errored result summary, else stderr."""
        error_info = self._format_error_summary(summary)
        if isinstance(error, ProcessError):
            detail = error_info or self._build_stderr_message(error.stderr, stderr_lines)
            return f"CLI process failed (exit code {error.exit_code}): {detail}"
        # The SDK wraps ProcessError as a generic Exception via the message stream.
        details = self._clean_error_message(str(error))
        if error_info:
            details += f"\nDetails: {error_info}"
        else:
            stderr = self._build_stderr_message(self._extract_cause_stderr(error), stderr_lines)
            details += f"\nStderr output:\n{stderr}"
        return f"Communication with agent failed: {details}"

    async def _pump_messages(
        self,
        decoder: _ClaudeDecoder,
        query_kwargs: dict[str, Any],
        deadline: float | None,
        should_stop: Callable[[], StopReason | None] | None,
    ) -> None:
        """Drive the SDK message stream for one turn.

        ``query`` is resolved as a module global at call time, so
        ``patch("...claude_code_agent.query", ...)`` mocks work. The ORDER of the two
        breaks matters: the deadline guard runs at the TOP, so an over-deadline message
        is DISCARDED; the cooperative stop runs AFTER dispatch, so the monitor can flip
        its flag on THIS message and the next is never pulled.
        """
        async for message in query(**query_kwargs):
            if deadline is not None and time.monotonic() > deadline:
                decoder.timeout_hit = True
                self._log.warning("Turn timeout reached mid-stream; breaking out of message loop")
                break
            decoder(message)
            reason = should_stop() if should_stop is not None else None
            if reason is not None:
                decoder.stop_reason = reason
                self._log.debug("Stop requested (%s); ending message loop at this boundary", reason.value)
                break

    def _build_claude_query(
        self,
        user_input: str,
        iteration: int,
        timeout: float | None,
        stderr_callback: Callable[[str], None],
    ) -> tuple[ClaudeAgentOptions, SubprocessCLITransport | None, str | None]:
        """Build the SDK options (+ a timeout-only transport) for one turn.

        ``transport`` is None unless a ``timeout`` is set: it is pre-constructed
        only so the watchdog can hard-kill the subprocess. ``effective_model`` may
        be None on a DirectRoute with no configured model. ``stderr_callback`` is
        wired in here but owned by ``communicate``.
        """
        assert self.working_directory is not None  # guaranteed by communicate's guard above

        plugins: list[SdkPluginConfig] = (
            [{"type": "local", "path": str(d)} for d in staged_plugin_dirs(self._plugin_root)]
            if self._plugin_root is not None
            else []
        )

        # Per-turn cost-correlation headers (LiteLLM only): the run/task tag plus
        # this turn's iteration, so the proxy-side cost log joins to the turn.
        cost_log_tags: dict[str, str] | None = None
        if self.cost_log_tags is not None:
            cost_log_tags = {**self.cost_log_tags, "x-ce-iteration": str(iteration)}
        env, route_model = self._build_sdk_env(
            self.route,
            path_prepend=self._env_path_prepend,
            plugin_tools_dir=self._plugin_tools_dir,
            cost_log_tags=cost_log_tags,
        )
        effective_model = self._resolve_effective_model(self.config.model, env, route_model)

        disallowed_tools = list(self.config.disallowed_tools or [])
        # Do not allow ToolSearch. This is required to keep Bedrock backend in sync with the other backends.
        if "ToolSearch" not in disallowed_tools:
            disallowed_tools.append("ToolSearch")

        # ALWAYS the claude_code preset: the SDK maps `system_prompt=None` to an
        # explicit EMPTY prompt and a plain string to a full replacement, either
        # of which loses Claude Code's default behavioral guidance.
        # `exclude_dynamic_sections` keeps the prompt static across runs — the
        # per-run tempdir path would otherwise be baked in, breaking prompt caching
        # and comparability — and the SDK re-injects the stripped sections into the
        # first user message. `system_prompt_mode="replace"` opts out.
        system_prompt = self._resolve_system_prompt()

        # as_posix(), not str(): bash on Windows strips backslashes from unquoted
        # paths, so `> D:\foo\bar` writes to "Dfoobar".
        options = ClaudeAgentOptions(
            cwd=self.working_directory.as_posix(),
            permission_mode=self.config.permission_mode.value,
            allowed_tools=self.config.allowed_tools or [],
            disallowed_tools=disallowed_tools,
            model=effective_model,
            plugins=plugins,
            stderr=stderr_callback,  # Capture stderr for better error messages
            env=env,
            # Recovers the CUMULATIVE output_tokens per emission from
            # `message_delta.usage`: the CLI ships only a partial streaming
            # snapshot (anthropics/claude-code#22686), so summing per-message
            # values undercounts by 10x+. Without this the SDK suppresses
            # StreamEvents. It also gates the first-window re-seed.
            include_partial_messages=True,
            system_prompt=system_prompt,
            setting_sources=self.config.setting_sources if self.config.setting_sources is not None else ["project"],
            resume=self._session_id,
            settings=json.dumps(self.config.claude_settings)
            if isinstance(self.config.claude_settings, dict)
            else self.config.claude_settings,
            mcp_servers=self._extra_mcp_servers,
            **self.config.sdk_options,
        )

        # For later inspection: captures every field, defaults included.
        self._sdk_options_dump = dump_dataclass(options)

        # Pre-constructed only under a timeout, to retain the subprocess handle
        # for hard-kill. None otherwise, so the SDK uses its own default and tests
        # can mock query() without a real CLI.
        transport: SubprocessCLITransport | None = None
        if timeout is not None:
            transport = SubprocessCLITransport(prompt=user_input, options=options)

        return options, transport, effective_model

    def _resolve_system_prompt(self) -> str | SystemPromptPreset:
        """The system-prompt VALUE that actually goes on the wire.

        Single source of truth for the options builder AND the
        ``system_prompt_semantics`` run marker, which is derived from this value
        and never recomputed — so the persisted regime cannot disagree with what
        was sent.

        ``replace`` requires a configured prompt. The config validator rejects the
        pair at load, but a hand-built config falls open to the preset here.
        """
        if self.config.system_prompt_mode == "replace":
            if self.config.system_prompt is not None:
                return self.config.system_prompt
            if not self._warned_prompt_mode_downgrade:
                # Once per agent, so the downgrade is visible in task.log rather
                # than only inferable from run.json's marker.
                self._warned_prompt_mode_downgrade = True
                logger.warning(
                    "system_prompt_mode='replace' with no system_prompt — falling back to the claude_code "
                    + "preset (append regime). run.json records the regime actually used."
                )
        preset = SystemPromptPreset(type="preset", preset="claude_code", exclude_dynamic_sections=True)
        if self.config.system_prompt is not None:
            preset["append"] = self.config.system_prompt
        return preset

    def get_environment_info(self) -> dict[str, Any]:
        """Record which system-prompt regime built this run's prompts.

        ``append`` = the claude_code preset with the configured prompt appended;
        ``replace`` = the configured prompt IS the entire system prompt (judge
        sub-agents). Unlike the other agents this is per-config, not fixed, so it
        overrides the contract's class default with the resolved value.

        Rationale: .claude/notes/agents.md § The system_prompt_semantics marker
        """
        semantics: SystemPromptSemantics = "replace" if isinstance(self._resolve_system_prompt(), str) else "append"
        return {**super().get_environment_info(), "system_prompt_semantics": semantics}

    async def stop(self) -> None:
        """Stop the agent and clean up resources."""
        self.client = None
        self._mark_stopped()

    async def kill(self) -> None:
        """Force-terminate the in-flight Claude CLI subprocess, if any.

        Async wrapper around ``kill_sync``. The watchdog inside communicate() uses
        ``_kill_transport`` on a CAPTURED transport instead, to avoid a stale
        watchdog killing a later turn's subprocess.
        """
        self.kill_sync()

    def kill_sync(self) -> None:
        """Synchronously SIGKILL the in-flight Claude CLI subprocess, if any.

        Safe from a non-asyncio thread. Reads ``self._active_transport`` once; a
        no-op if a later turn already cleared it.
        """
        self._kill_transport(self._active_transport)

    @staticmethod
    def _timed_out(timeout_hit: bool, deadline: float | None) -> bool:
        """Return True if the turn has exceeded its deadline by either path.

        BOTH the watchdog flag and the wall clock: a flag-only check races the
        watchdog and misreports a timeout entered just before the flip.
        """
        if timeout_hit:
            return True
        return deadline is not None and time.monotonic() > deadline

    @staticmethod
    def _kill_transport(transport: SubprocessCLITransport | None) -> None:
        """SIGKILL the subprocess behind `transport`, if any.

        SIGKILL releases stdout/stdin, which unblocks the anyio readers so the
        async generator unwinds cleanly.

        Rationale: .claude/notes/agents.md § The threaded watchdog
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

    @staticmethod
    def _aggregate_model_usage(model_usage: dict[str, Any] | None) -> TokenUsage | None:
        """Sum the SDK ResultMessage ``model_usage`` into a cumulative TokenUsage.

        Maps each model id to its cumulative session billing (camelCase, unlike
        ``usage``). The SDK's authoritative cost breakdown: summed and priced it
        reconciles to ``total_cost_usd`` exactly, and it INCLUDES sub-agent
        consumption the stream under-reports. None when absent, so the caller can
        fall back.
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
            uncached_input_tokens=inp,
            output_tokens=out,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
            total_cost_usd=cost if any_cost else None,
        )

    @staticmethod
    def _build_token_usage(
        messages: Sequence[TranscriptMessage],
        sdk_result_usage: dict[str, Any] | None,
        sdk_result_cost: float | None,
        sdk_result_model_usage: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> TokenUsage | None:
        """Build the run's cumulative TokenUsage, or None if unavailable.

        Source-of-truth order:

        1. ``ResultMessage.model_usage`` — the SDK's cumulative per-model billing,
           authoritative and inclusive of sub-agent consumption. Prefer it.
        2. Per-call telemetry stream (sum) — used when ``model_usage`` is absent.
           Exact only when EVERY token-bearing emission carries a ``message_id``,
           since that is what the dedup keys on.
        3. ``ResultMessage.usage`` snapshot — last resort; it under-reports the
           cache-read cascade ~2-3x on multi-call runs.

        ``total_cost_usd`` comes from ``model_usage.costUSD``, else the
        ResultMessage total, else the priced buckets (a killed turn has no
        terminal ``ResultMessage``).

        **The BILLING view: summing ``messages`` does not reproduce it on its
        own.** ``EventCollector`` books the residual as one synthetic
        ``ReconciliationMessage``, so the stream still sums to this total. Do NOT
        smear by-difference tokens onto real generations.

        Rationale: .claude/notes/agents.md § Token accounting, per harness
        """
        from_models = ClaudeCodeAgent._aggregate_model_usage(sdk_result_model_usage)
        if from_models is not None:
            if from_models.total_cost_usd is None:
                from_models.total_cost_usd = sdk_result_cost
            return ClaudeCodeAgent._backfill_cost(from_models, model)

        assistant_msgs = [m for m in messages if isinstance(m, AssistantMessageTelemetry)]
        token_bearing = [
            m
            for m in assistant_msgs
            if m.input_tokens or m.output_tokens or m.cache_creation_tokens or m.cache_read_tokens
        ]
        # Exact only when every token-bearing emission has an id, so the dedup
        # applied and no ResultMessage backfill was mixed in.
        if token_bearing and all(m.message_id for m in token_bearing):
            return ClaudeCodeAgent._backfill_cost(
                TokenUsage(
                    uncached_input_tokens=sum(m.input_tokens for m in assistant_msgs),
                    output_tokens=sum(m.output_tokens for m in assistant_msgs),
                    cache_creation_input_tokens=sum(m.cache_creation_tokens for m in assistant_msgs),
                    cache_read_input_tokens=sum(m.cache_read_tokens for m in assistant_msgs),
                    total_cost_usd=sdk_result_cost,
                ),
                model,
            )
        if not sdk_result_usage:
            return None
        return ClaudeCodeAgent._backfill_cost(
            TokenUsage(
                uncached_input_tokens=sdk_result_usage.get("input_tokens", 0),
                output_tokens=sdk_result_usage.get("output_tokens", 0),
                cache_creation_input_tokens=sdk_result_usage.get("cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=sdk_result_usage.get("cache_read_input_tokens", 0) or 0,
                total_cost_usd=sdk_result_cost,
            ),
            model,
        )

    @staticmethod
    def _backfill_cost(usage: TokenUsage, model: str | None) -> TokenUsage:
        """Price the turn in place with ``pricing.price_turn`` and return it.

        A timed-out or killed turn has no terminal ``ResultMessage``, so the cost
        is absent even though the tokens are fully captured; the rate card fills it.
        """
        reported = usage.total_cost_usd
        usage.total_cost_usd = price_turn(usage, (model,))
        if usage.total_cost_usd is None and reported is None and model and not usage.is_empty():
            # Not in the rate card, so the turn reverts to a null cost. Surface
            # it, or a stale pricing table silently reads as "Cost = —".
            logger.warning("No pricing for model %r; timeout/kill turn cost left unset", model)
        return usage

    @staticmethod
    def _reprice_for_litellm(usage: TokenUsage, model: str | None) -> None:
        """Recompute the top-line cost for the LiteLLM backend, in place.

        The SDK's cost estimate assumes Anthropic pricing, so it is wrong behind
        LiteLLM. The token buckets are left UNTOUCHED, so the reconciliation
        invariant is unaffected — only the cost scalar changes.

        An unpriced model sets the cost to ``None`` **and warns**, so the log names
        the model when a ``max_usd`` task then finishes ``ERROR``.

        Rationale: .claude/notes/agents.md § Cost: the stream versus the rate card
        """
        cost = price_turn(usage.model_copy(update={"total_cost_usd": None}), (model,))
        usage.total_cost_usd = cost
        if cost is None and not usage.is_empty():
            logger.warning("No pricing for litellm model %r; turn cost left unset", model)

    def get_sdk_options(self) -> dict[str, Any] | None:
        """Get the raw SDK options used for the last agent query.

        Returns:
            Dictionary of SDK option field names to values, or None if communicate() hasn't been called.
        """
        return self._sdk_options_dump

    @staticmethod
    def _try_parse_json_value(content: Any) -> dict[str, Any] | list[Any] | None:
        """Return the parsed JSON object or array from content, else None.

        The STRICT telemetry-capture variant. ``formatting._extract_json`` is the
        lenient display-path twin — keep behaviour aligned, but they are
        intentionally separate: this one feeds ``CommandTelemetry.result_data``,
        where a false positive persists into ``task.json`` and downstream
        dashboards.

        Accepts a plain string or a list of content blocks (MCP tools use the
        latter), and parses with ``raw_decode`` from the first line whose first
        non-whitespace character is ``{`` or ``[``, so prefix noise and trailing
        garbage are tolerated. Requiring the brace to START A LINE is what stops
        an incidental ``[`` inside text matching.

        Rejected on purpose: a failed parse (no fragment fallback), bare ``{}`` /
        ``[]``, and bare primitives. Non-JSON tool output is normal, so failures
        are silent.
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
        # First 200 chars only: enough for a few prefix warning lines, not so lax
        # that a brace buried in long text reads as a structured payload.
        match = re.search(r"(?:^|\n)[^\S\n]*[{[]", content[:_JSON_START_SEARCH_LIMIT])
        if not match:
            return None
        try:
            parsed, _ = json.JSONDecoder().raw_decode(content, match.end() - 1)
        except ValueError:
            return None
        # A non-empty dict or list is evidence of real structured content; an
        # empty one is indistinguishable from an accidental match.
        if isinstance(parsed, (dict, list)) and parsed:
            return parsed
        return None

    _PERMISSION_PHRASES = ("permission", "not allowed", "requires approval", "denied", "blocked")

    @classmethod
    def _tool_end_status(cls, is_error: bool, content: Any) -> ToolEndStatus:
        """Classify a tool result into a ToolEndStatus."""
        if not is_error:
            return ToolEndStatus.OK
        text = str(content).lower() if content is not None else ""
        if any(phrase in text for phrase in cls._PERMISSION_PHRASES):
            return ToolEndStatus.PERMISSION_DENIED
        return ToolEndStatus.ERROR

    @staticmethod
    def _subagent_terminal_part(message: Any) -> tuple[str, Generation] | None:
        """A sub-agent's TERMINAL generation, as ``(spawning tool_use_id, part)``.

        A sub-agent's intermediate generations bubble into the parent stream as
        ``parent_tool_use_id``-tagged messages, but its terminal one is delivered as the
        Agent tool RESULT and never streamed, so it has no window to measure.
        ``tool_use_result.usage`` is that call's own breakdown and does not overlap the
        bubbled intermediates. None for a non-sub-agent tool result (no ``agentId``).
        The token total is unaffected: it derives from ``model_usage``.
        """
        tur = getattr(message, "tool_use_result", None)
        if not isinstance(tur, dict) or "agentId" not in tur:
            return None
        usage = tur.get("usage")
        if not isinstance(usage, dict):
            return None

        # The spawning Agent tool_use_id (and the returned text) live on the
        # ToolResultBlock, not on tool_use_result itself.
        tool_use_id: str | None = None
        result_text = ""
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                if _is_tool_result_block(block):
                    tool_use_id = getattr(block, "tool_use_id", None)
                    result_text = _tool_result_text(getattr(block, "content", None))
                    break
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return None

        def _int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        return tool_use_id, Generation(
            blocks=[ContentBlock(block_type="text", sequence=0, text=result_text)] if result_text else [],
            tokens=TokenUsage(
                uncached_input_tokens=_int(usage.get("input_tokens")),
                output_tokens=_int(usage.get("output_tokens")),
                cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
                cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
            ),
        )

    @staticmethod
    def _build_stderr_message(sdk_stderr: str | None, stderr_lines: list[str]) -> str:
        """Combine SDK stderr with captured stderr lines, filtering out placeholder text.

        The SDK often returns a hardcoded placeholder instead of real error
        content; the details are in the lines captured via the stderr callback.
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

        The SDK re-raises ProcessError as a generic Exception via the Query
        message stream, so the original stderr is only in the cause chain.
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

        None only when ``msg`` lacks the ResultMessage shape. ``subtype`` is a
        required dataclass field there, so a missing value becomes ``"unknown"``
        rather than silently disabling the summary downstream.
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

        Prefers free-form ``result`` text, falling back to the
        ``subtype``/``stop_reason`` classification — the common shape on a hard
        CLI crash. None when there is nothing useful, so the caller can fall back
        to stderr.
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
        return format_messages(messages, warned_unknown_types=self._warned_unknown_types, log=self._log)

    def _update_state_from_messages(self, messages: list[Message]) -> None:
        """Update agent state based on received messages.

        Args:
            messages: List of messages from the agent (SDK objects)
        """
        # `ResultMessage.is_error` is intentionally NOT a state-change trigger:
        # the agent may recover from a tool error on a later turn.
        for msg in messages:
            if getattr(msg, "error", None):
                self._state = AgentState.ERROR
                return

        # If no errors, agent is working normally
        self._state = AgentState.WORKING
