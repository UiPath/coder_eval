"""Claude Code agent implementation using the Claude Agent SDK."""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

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
from claude_agent_sdk.types import SystemPromptPreset

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._logging import PrefixedAdapter, log_raw_sdk_event
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import ThreadedWatchdog
from coder_eval.config import settings
from coder_eval.errors import (
    TurnTimeoutError,
    format_timeout_reason,
)
from coder_eval.formatting import format_messages, format_payload
from coder_eval.models import (
    AgentKind,
    ApiRoute,
    BedrockRoute,
    ClaudeCodeAgentConfig,
    CommandTelemetry,
    ContentBlock,
    DirectRoute,
    LiteLLMRoute,
    ResultSummary,
    SystemPromptSemantics,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
    to_bedrock_inference_profile,
)
from coder_eval.models import (
    AssistantMessage as AssistantMessageTelemetry,
)
from coder_eval.pricing import calculate_cost
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
from coder_eval.timing import TurnClock, close_window
from coder_eval.utils import dump_dataclass, process_plugins


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


class _ClaudeTurnState:
    """Per-turn mutable scratch state for one ``ClaudeCodeAgent.communicate`` call.

    Holds every cross-branch local the SDK-stream pump mutates, with one method
    per message kind plus ``dispatch`` and ``finalize``. A back-reference to the
    agent lets it reuse the agent's helpers.

    The two raw lists are DISTINCT and must stay so: ``messages`` holds raw SDK
    ``Message`` objects and ``sdk_messages`` holds telemetry
    ``TranscriptMessage`` objects, carried on ``AgentEndEvent``.
    """

    def __init__(
        self,
        agent: "ClaudeCodeAgent",
        *,
        emit: CompositeStreamCallback,
        collector: EventCollector,
        task_id: str,
        user_input: str,
        iteration: int,
        max_turns: int | None,
        log: PrefixedAdapter,
        turn_start_time: float,
        deadline: float | None,
        clock: TurnClock | None = None,
    ) -> None:
        self._agent = agent
        self.emit = emit
        self.collector = collector
        self.task_id = task_id
        self.user_input = user_input
        self.iteration = iteration
        self.max_turns = max_turns
        self.log = log
        self.turn_start_time = turn_start_time
        self.deadline = deadline
        # Set True by the in-loop deadline break OR the watchdog callback.
        self.timeout_hit = False
        # Set True by the in-loop cooperative-stop break (early-stop-on-criterion).
        # Distinct from timeout_hit: a clean, non-crash stop that must NOT raise.
        self.stopped_early_hit = False
        # Resolved by _build_claude_query, set on the state before any finalize
        # path. Stays None if we crash before setup (finalize reads it for cost
        # backfill).
        self.effective_model: str | None = None

        # Two distinct lists (do NOT merge): raw SDK objects vs. telemetry.
        self.messages: list[Message] = []
        self.sdk_messages: list[TranscriptMessage] = []

        # Two-phase command tracking (tool_id -> {telemetry, command_start_time}).
        self.pending_commands: dict[str, dict[str, Any]] = {}
        self.processed_results: set[str] = set()
        self.sequence_number = 0

        self.last_assistant_message_index: int | None = None
        # ONE clock per turn, and every wall stamp this turn records derives from
        # it, so the central subtraction clips WALL tool spans to WALL window
        # bounds. Injectable so a test supplies a fake rather than monkeypatching
        # this module's `datetime`, which a derived stamp silently escapes.
        # `turn_start_time` stays raw monotonic: a deadline must not move when the
        # wall clock steps.
        self.clock = clock or TurnClock()
        self.last_event_wall: datetime = self.clock.now()
        # Re-seeded ONCE, at the first observed model output. See
        # `_seed_first_generation_window`.
        self.first_output_seen: bool = False

        # SDK ResultMessage capture.
        self.sdk_result_usage: dict[str, Any] | None = None
        self.sdk_result_model_usage: dict[str, Any] | None = None
        self.sdk_result_cost: float | None = None
        self.num_turns: int | None = None
        self.sdk_result_summary: ResultSummary | None = None

        self.sdk_model_used: str | None = None

        # Per-emission output_tokens recovery from raw stream events.
        self.pending_delta_output_tokens: int | None = None
        self.current_stream_message_id: str | None = None
        self.emissions_by_id: dict[str, list[AssistantMessageTelemetry]] = {}
        self.emission_proxies_by_id: dict[str, list[int]] = {}

        # Dedup of multi-emission API calls sharing a message_id.
        self.seen_message_ids: set[str] = set()
        self.last_message_had_id: bool = False

        self.assistant_turn_count = 0

        # Turn/tool bracketing (self-describing event tree).
        self.current_turn_id: str | None = None
        self.tool_turn_ids: dict[str, str] = {}  # tool_id -> spawning turn_id
        self.emitted_tool_ends: set[str] = set()  # tool_ids already closed
        self.finalized = False

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

    def dispatch(self, message: Message) -> None:
        """Record the raw message and route it to its per-kind handler.

        ORDER IS LOAD-BEARING: ``_is_sdk_result_message`` before
        ``_is_user_message``, and the TaskNotification guard before both.
        """
        self.messages.append(message)
        msg_type = type(message).__name__
        log_raw_sdk_event(self.log, repr_target=message, type=msg_type)

        if _is_assistant_message(message):
            self.on_assistant_message(message)
        elif _is_task_notification(message):
            self.on_task_notification(message)
        elif _is_sdk_result_message(message):
            self.on_result_message(message)
        elif isinstance(getattr(message, "event", None), dict):
            self.on_stream_event(message)
        elif _is_user_message(message):
            self.on_user_message(message)

    def on_assistant_message(self, message: Message) -> None:
        """Capture ToolUseBlocks + build the AssistantMessage telemetry record."""
        message_arrival_wall = self.clock.now()
        generation_started_wall = self.last_event_wall

        current_turn_index = len(self.sdk_messages)
        self.assistant_turn_count += 1
        model_attr = getattr(message, "model", None)
        if isinstance(model_attr, str):
            self.sdk_model_used = model_attr

        # Inner-turn boundary: one TurnStart per new message_id (one API call).
        raw_mid = getattr(message, "message_id", None)
        turn_id = raw_mid if isinstance(raw_mid, str) else f"turn-{self.assistant_turn_count}"
        if turn_id != self.current_turn_id:
            if self.current_turn_id is not None:
                self.emit.on_event(
                    TurnEndEvent(
                        task_id=self.task_id,
                        turn_id=self.current_turn_id,
                        status=TurnEndStatus.COMPLETED,
                        tokens=self.turn_tokens(self.current_turn_id),
                    )
                )
            self.current_turn_id = turn_id
            self.emit.on_event(TurnStartEvent(task_id=self.task_id, turn_id=turn_id, model=self.sdk_model_used))

        content = getattr(message, "content", None)
        turn_content_blocks: list[ContentBlock] = []
        turn_tool_use_ids: list[str] = []
        emission_content_chars = 0

        if content and isinstance(content, list):
            for block in content:
                block_seq = len(turn_content_blocks)

                if _is_tool_use_block(block):
                    tool_args = block.input if isinstance(block.input, dict) else {"raw": block.input}
                    emission_content_chars += len(str(getattr(block, "name", "") or "")) + len(
                        json.dumps(tool_args, default=str)
                    )
                    command_start_time = time.monotonic()

                    telemetry = CommandTelemetry(
                        tool_name=block.name,
                        tool_id=block.id,
                        timestamp=message_arrival_wall,
                        generation_completed_at=message_arrival_wall,
                        assistant_turn_index=current_turn_index,
                        parameters=block.input if isinstance(block.input, dict) else {"raw": block.input},
                        sequence_number=self.sequence_number,
                        result_status=None,
                        duration_ms=None,
                    )

                    self.pending_commands[block.id] = {
                        "telemetry": telemetry,
                        "command_start_time": command_start_time,
                    }
                    self.sequence_number += 1

                    turn_content_blocks.append(
                        ContentBlock(block_type="tool_use", sequence=block_seq, tool_use_id=block.id)
                    )
                    turn_tool_use_ids.append(block.id)

                    self.tool_turn_ids[block.id] = self.current_turn_id or ""
                    self.emit.on_event(
                        ToolStartEvent(task_id=self.task_id, turn_id=self.current_turn_id or "", tool=telemetry)
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
                    turn_content_blocks.append(ContentBlock(block_type="text", sequence=block_seq, text=text_value))
                    self.emit.on_event(
                        TextChunkEvent(task_id=self.task_id, turn_id=self.current_turn_id or "", text=text_value)
                    )

        msg_usage = getattr(message, "usage", None) or {}
        message_id = getattr(message, "message_id", None)
        parent_tool_use_id = getattr(message, "parent_tool_use_id", None)
        is_duplicate_emission = isinstance(message_id, str) and message_id in self.seen_message_ids
        if isinstance(message_id, str):
            self.seen_message_ids.add(message_id)
            self.last_message_had_id = True
        else:
            self.last_message_had_id = False

        if is_duplicate_emission:
            in_tok = out_tok = cw_tok = cr_tok = rt_tok = 0
        else:
            in_tok = int(msg_usage.get("input_tokens", 0) or 0)
            cw_tok = int(msg_usage.get("cache_creation_input_tokens", 0) or 0)
            cr_tok = int(msg_usage.get("cache_read_input_tokens", 0) or 0)
            rt_tok = int(msg_usage.get("reasoning_tokens", 0) or 0)
            if self.pending_delta_output_tokens is not None:
                out_tok = self.pending_delta_output_tokens
            else:
                out_tok = int(msg_usage.get("output_tokens", 0) or 0)
            self.pending_delta_output_tokens = None

        # The RAW window. `started` is the mark, since this stream carries no
        # per-emission item start to pull the window open to.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        started, raw_generation_ms = close_window(mark=generation_started_wall, now=message_arrival_wall)
        assistant_telemetry = AssistantMessageTelemetry(
            started_at=started,
            completed_at=message_arrival_wall,
            generation_duration_ms=raw_generation_ms,
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
            model=self.sdk_model_used,
            message_id=message_id if isinstance(message_id, str) else None,
            parent_tool_use_id=(parent_tool_use_id if isinstance(parent_tool_use_id, str) else None),
        )
        self.sdk_messages.append(assistant_telemetry)
        if isinstance(message_id, str):
            self.emissions_by_id.setdefault(message_id, []).append(assistant_telemetry)
            self.emission_proxies_by_id.setdefault(message_id, []).append(emission_content_chars)
        self.last_assistant_message_index = len(self.sdk_messages) - 1

        self.last_event_wall = message_arrival_wall

    def on_task_notification(self, message: Message) -> None:
        """TaskNotification carries LOSSY per-sub-agent usage, captured from the
        Agent tool-result instead. This exists only to keep
        ``_is_sdk_result_message`` from misreading it."""
        pass

    def on_result_message(self, message: Message) -> None:
        """Capture the SDK ResultMessage usage/cost/session + the id-less backfill."""
        self.sdk_result_usage = getattr(message, "usage", None)
        self.sdk_result_model_usage = getattr(message, "model_usage", None)
        self.sdk_result_cost = getattr(message, "total_cost_usd", None)
        self.num_turns = getattr(message, "num_turns", None)
        self.sdk_result_summary = self._agent._summarize_result(message)
        # Only advance session_id on clean turns.
        new_session_id = getattr(message, "session_id", None)
        if self.sdk_result_summary is not None and self.sdk_result_summary.is_error:
            self.log.debug(
                "is_error ResultMessage; not advancing session_id (kept %s)",
                self._agent._session_id,
            )
        else:
            if new_session_id != self._agent._session_id:
                self.log.debug("session_id changed: %s -> %s", self._agent._session_id, new_session_id)
            self._agent._session_id = new_session_id

        # Retro-populate the last AssistantMessage from ResultMessage.usage when
        # per-message capture was not in effect for THAT message (no message_id).
        if self.last_assistant_message_index is not None and self.sdk_result_usage and not self.last_message_had_id:
            last_msg = self.sdk_messages[self.last_assistant_message_index]
            if isinstance(last_msg, AssistantMessageTelemetry):
                last_msg.input_tokens = int(self.sdk_result_usage.get("input_tokens", 0) or 0)
                last_msg.output_tokens = int(self.sdk_result_usage.get("output_tokens", 0) or 0)
                last_msg.cache_creation_tokens = int(self.sdk_result_usage.get("cache_creation_input_tokens", 0) or 0)
                last_msg.cache_read_tokens = int(self.sdk_result_usage.get("cache_read_input_tokens", 0) or 0)
                last_msg.reasoning_tokens = int(self.sdk_result_usage.get("reasoning_tokens", 0) or 0)

    def _seed_first_generation_window(self) -> None:
        """Move the first window's mark to the first observed model output.

        ONCE PER TURN, and that is the whole contract: re-seeding on every
        ``message_start`` would stop the windows tiling and drop the gap before
        the next emission into no bucket. The flag needs no reset — a fresh
        ``_ClaudeTurnState`` is built per ``communicate()``. A turn with no
        ``message_start`` keeps the turn-entry mark and clamps to ``0.0``, which is
        the correct degradation.

        One route to that degradation is OPERATOR-REACHABLE:
        ``-D agent.sdk_options.include_partial_messages=false`` turns the raw
        stream off and with it this re-seed. Nothing warns.

        Rationale: .claude/notes/agents.md § First-generation window seeding
        """
        if self.first_output_seen:
            return
        self.first_output_seen = True
        self.last_event_wall = self.clock.now()

    def on_stream_event(self, message: Message) -> None:
        """Recover cumulative output_tokens from raw ``message_start`` /
        ``message_delta`` stream events."""
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
        """Process tool results (and a sub-agent's terminal generation) from a
        tool-result UserMessage. The sub-agent message is appended BEFORE the
        tool-result loop — its position in ``sdk_messages`` is observable.

        The generation mark is DELIBERATELY NOT advanced here: leaving it where
        ``on_assistant_message`` put it is what makes the windows tile.

        Rationale: .claude/notes/agents.md § Per-harness generation marks
        """
        sub_msg = self._agent._synthesize_subagent_terminal_message(message, self.sdk_model_used)
        if sub_msg is not None:
            self.sdk_messages.append(sub_msg)

        content = getattr(message, "content", None)
        if content and isinstance(content, list):
            for block in content:
                if _is_tool_result_block(block):
                    tool_name = ""
                    if block.tool_use_id in self.pending_commands:
                        tool_name = self.pending_commands[block.tool_use_id]["telemetry"].tool_name
                    self._agent._resolve_pending_command(
                        block.tool_use_id,
                        getattr(block, "is_error", False) or False,
                        block.content,
                        self.pending_commands,
                        self.processed_results,
                        now=self.clock.now(),
                    )
                    is_error_flag = getattr(block, "is_error", False) or False
                    resolved = self.pending_commands.get(block.tool_use_id, {}).get("telemetry")
                    tool_for_event = resolved or CommandTelemetry(
                        tool_name=tool_name or "unknown",
                        tool_id=block.tool_use_id,
                        timestamp=self.clock.now(),
                        result_status="error" if is_error_flag else "success",
                        result_summary=format_payload(block.content),
                    )
                    status = self._agent._tool_end_status(is_error_flag, block.content)
                    self.emitted_tool_ends.add(block.tool_use_id)
                    self.emit.on_event(
                        ToolEndEvent(
                            task_id=self.task_id,
                            turn_id=self.tool_turn_ids.get(block.tool_use_id, self.current_turn_id or ""),
                            tool=tool_for_event,
                            status=status,
                        )
                    )

    def _finalize_token_usage(self) -> TokenUsage:
        """Build the turn's cumulative TokenUsage, repricing for LiteLLM.

        Extracted from ``finalize`` so the LiteLLM repricing *wiring*, not just
        the helper, is directly testable.

        Rationale: .claude/notes/agents.md § Cost: the stream versus the rate card
        """
        usage = (
            self._agent._build_token_usage(
                self.sdk_messages,
                self.sdk_result_usage,
                self.sdk_result_cost,
                self.sdk_result_model_usage,
                self.effective_model,
            )
            or TokenUsage()
        )
        if isinstance(self._agent.route, LiteLLMRoute):
            self._agent._reprice_for_litellm(usage, self.effective_model)
        return usage

    def finalize(self, status: AgentEndStatus, *, crashed: bool = False, crash_reason: str | None = None) -> None:
        """Close orphaned tools + the open turn, emit the terminal AgentEndEvent,
        and on a crash build the partial TurnRecord. Idempotent."""
        if self.finalized:
            return
        self.finalized = True

        commands = self._agent._finalize_commands(self.pending_commands, self.messages)
        for cmd in commands:
            if cmd.tool_id in self.emitted_tool_ends:
                continue
            self.emitted_tool_ends.add(cmd.tool_id)
            self.emit.on_event(
                ToolEndEvent(
                    task_id=self.task_id,
                    turn_id=self.tool_turn_ids.get(cmd.tool_id, self.current_turn_id or ""),
                    tool=cmd,
                    status=ToolEndStatus.UNRESOLVED,
                )
            )

        max_turns_exhausted = not crashed and (
            self._agent._is_max_turns_result(self.sdk_result_summary)
            or (self.max_turns is not None and self.num_turns is not None and self.num_turns > self.max_turns)
        )
        if max_turns_exhausted and status == AgentEndStatus.COMPLETED:
            status = AgentEndStatus.MAX_TURNS_EXHAUSTED
            self.log.warning("Agent exhausted max_turns (%s); turn ended without completing", self.max_turns)

        if self.current_turn_id is not None:
            self.emit.on_event(
                TurnEndEvent(
                    task_id=self.task_id,
                    turn_id=self.current_turn_id,
                    status=TurnEndStatus(status.value),
                    tokens=self.turn_tokens(self.current_turn_id),
                )
            )
            self.current_turn_id = None

        usage = self._finalize_token_usage()

        try:
            agent_output = self._agent._format_messages(self.messages)
        except Exception as fmt_err:
            logger.warning("Failed to format messages for AgentEndEvent; using placeholder", exc_info=True)
            agent_output = f"<partial record: message formatting failed: {type(fmt_err).__name__}: {fmt_err}>"

        self.emit.on_event(
            AgentEndEvent(
                task_id=self.task_id,
                status=status,
                usage=usage,
                iteration=self.iteration,
                user_input=self.user_input,
                agent_output=agent_output,
                model_used=self.sdk_model_used,
                assistant_turn_count=self.assistant_turn_count,
                messages=list(self.sdk_messages),
                num_turns=self.num_turns,
                max_turns_exhausted=max_turns_exhausted,
                result_summary=self.sdk_result_summary,
                crashed=crashed,
                crash_reason=crash_reason,
                duration_seconds=time.monotonic() - self.turn_start_time,
                # One basis with the window bounds — see the AgentStartEvent
                # site in `communicate`.
                timestamp=self.clock.now(),
            )
        )

        if crashed:
            self._agent._capture_partial_turn(self.collector)


@AgentRegistry.register(AgentKind.CLAUDE_CODE, ClaudeCodeAgentConfig)
class ClaudeCodeAgent(Agent[ClaudeCodeAgentConfig]):
    """Implementation of the Agent interface for Claude Code using the SDK."""

    # The message loop has a between-messages guard where `should_stop` runs.
    supports_cooperative_stop: ClassVar[bool] = True

    # __init__ accepts cost_log_tags and stamps them into ANTHROPIC_CUSTOM_HEADERS
    # for the proxy-side actual-cost join (LiteLLM backend).
    supports_cost_log_tags: ClassVar[bool] = True

    # One warning per agent for a replace-mode config with no prompt: the resolver
    # runs on every query, and a per-turn repeat would bury the rest of task.log.
    _warned_prompt_mode_downgrade: bool = False

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
        self.config = config
        self.route = route or DirectRoute()
        self._extra_mcp_servers = extra_mcp_servers or {}
        # Correlation headers stamped on every SDK->proxy request (LiteLLM only).
        # This turn's iteration is appended per-communicate().
        self._cost_log_tags = cost_log_tags
        self.client: ClaudeSDKClient | None = None
        self.working_directory: Path | None = None
        # Turn-lifecycle bookkeeping lives on the Agent base class.
        self._sdk_options_dump: dict[str, Any] | None = None
        self._session_id: str | None = None
        # Held only while a communicate() call is in flight, so kill() can reach
        # the CLI subprocess when the SDK swallows asyncio cancellation.
        self._active_transport: SubprocessCLITransport | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
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
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        """Send a message to Claude and receive its response.

        Args:
            user_input: The message/prompt to send
            stream_callback: Optional callback for real-time event streaming
            timeout: Hard wall-clock deadline in seconds. A watchdog force-kills
                the CLI subprocess when it elapses — the SDK's anyio task groups
                suppress cooperative cancellation, so `asyncio.wait_for` is not
                sufficient.
            max_turns: Hard cap on inner-loop turns. None defers to the SDK.
            should_stop: Cooperative early-stop poll, checked after each dispatched
                message; the first True finalizes cleanly as STOPPED_EARLY
                (``crashed=False``, no raise) at the next boundary.

        Returns:
            TurnRecord containing the complete interaction

        Raises:
            RuntimeError: If agent is not started.
            TurnTimeoutError: Watchdog/wall-clock fired; carries a partial TurnRecord.
            AgentCrashError: SDK/CLI failed mid-turn; carries a partial TurnRecord.
        """
        if not self.working_directory:
            raise RuntimeError("Agent not started. Call start() first.")

        # Every constructor path sets it; assert so the streaming-event sites
        # below can use `str(self.config.type)`.
        assert self.config.type is not None, "ClaudeCodeAgent requires AgentConfig.type to be set before communicate()"

        # Reset the pending slot + bump the iteration counter (shared lifecycle).
        self._begin_turn()

        turn_start_time = time.monotonic()
        deadline = turn_start_time + timeout if timeout is not None else None

        # The agent is the SOLE emitter: events fan out to an internal
        # EventCollector and the caller's stream_callback.
        task_id = str(self.config.type)  # str() so a plugin subclass with a non-enum kind also works
        collector = EventCollector()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])

        # Built BEFORE the try so except/finally can finalize even when setup
        # crashes. `timeout_hit` is written by both the in-loop deadline break and
        # the watchdog callback; bool assignment is atomic under the GIL.
        state = _ClaudeTurnState(
            self,
            emit=emit,
            collector=collector,
            task_id=task_id,
            user_input=user_input,
            iteration=self._iteration,
            max_turns=max_turns,
            log=self._log,
            turn_start_time=turn_start_time,
            deadline=deadline,
        )

        # STAYS a communicate local: it is wired into the SDK options during
        # setup, which the state (built first) would order-invert.
        stderr_lines: list[str] = []

        def capture_stderr(line: str) -> None:
            stderr_lines.append(line)

        try:
            options, transport, effective_model = self._build_claude_query(
                user_input, timeout, max_turns, capture_stderr
            )
            # Set on the state BEFORE the AgentStart emit and any finalize path
            # (finalize reads it for cost backfill); stays None if setup crashed.
            state.effective_model = effective_model
            if transport is not None:
                self._active_transport = transport

            # Agent lifecycle opens here (the agent — not the orchestrator — owns it).
            emit.on_event(
                AgentStartEvent(
                    task_id=task_id,
                    prompt=user_input,
                    iteration=self._iteration,
                    model=effective_model,
                    # From the TURN CLOCK, not the model's raw `datetime.now()`
                    # default: this bound is subtracted against window bounds the
                    # same clock produced, and two bases in one subtraction publish
                    # a clamped inversion as a measured 0.0 (CE058).
                    timestamp=state.clock.now(),
                )
            )

            # Captured in the CLOSURE, not read from self._active_transport, so a
            # stale watchdog from an earlier turn cannot kill this turn's process.
            watchdog_target = transport

            def _on_turn_timeout() -> None:
                state.timeout_hit = True
                self._kill_transport(watchdog_target)

            # Only when one was built, so mocks with strict (prompt, options)
            # signatures keep working on the no-timeout path.
            query_kwargs: dict[str, Any] = {"prompt": user_input, "options": options}
            if transport is not None:
                query_kwargs["transport"] = transport
            self._log.debug("Starting agent query stream...")
            # OS-thread watchdog: fires regardless of event-loop liveness, and is
            # immune to anyio cancel-scope suppression.
            with ThreadedWatchdog(
                timeout_seconds=timeout,
                on_timeout=_on_turn_timeout,
                asyncio_task_to_cancel=asyncio.current_task(),
                label=f"Turn timeout ({timeout:g}s)" if timeout else "turn_timeout",
            ):
                await self._pump_messages(state, query_kwargs, deadline, should_stop)

            self._log.debug("Agent query stream ended")

        except asyncio.CancelledError:
            # A cancel that landed BECAUSE of the timeout re-raises as
            # TurnTimeoutError, so the retry system sees a terminal timeout rather
            # than a transient cancel. External cancels propagate unchanged.
            if self._timed_out(state.timeout_hit, deadline):
                assert timeout is not None
                self._finalize_and_raise_timeout(state.finalize, timeout)
            # Cancelled from outside: park the telemetry on `pending_turn`, or the
            # `finally` finalizes as COMPLETED and keeps no record.
            if not state.finalized:
                self._finalize_external_cancel(state.finalize)
            raise
        except ProcessError as e:
            # A watchdog SIGKILL surfaces as ProcessError (exit -9); classify it as
            # a timeout so the retry system does not treat it as AGENT_CRASH.
            if self._timed_out(state.timeout_hit, deadline):
                assert timeout is not None
                self._finalize_and_raise_timeout(state.finalize, timeout, cause=e)
            if not self._max_turns_short_circuit(state.sdk_result_summary, f"ProcessError(exit={e.exit_code})"):
                stderr = self._build_stderr_message(e.stderr, stderr_lines)
                error_info = self._format_error_summary(state.sdk_result_summary)
                detail = error_info or stderr
                message = f"CLI process failed (exit code {e.exit_code}): {detail}"
                self._finalize_and_raise_crash(state.finalize, message, cause=e)
        except Exception as e:
            # The SDK may re-raise a watchdog kill as a generic Exception. Check
            # both the flag AND the wall clock, in case the flip races this catch.
            if self._timed_out(state.timeout_hit, deadline):
                assert timeout is not None
                self._finalize_and_raise_timeout(state.finalize, timeout, cause=e)
            if not self._max_turns_short_circuit(state.sdk_result_summary, "Generic Exception"):
                # The SDK wraps ProcessError as a generic Exception via the
                # message stream; read the ResultMessage summary for context.
                error_info = self._format_error_summary(state.sdk_result_summary)
                cause_stderr = self._extract_cause_stderr(e)
                stderr = self._build_stderr_message(cause_stderr, stderr_lines)
                error_details = self._clean_error_message(str(e))
                if error_info:
                    error_details += f"\nDetails: {error_info}"
                elif stderr:
                    error_details += f"\nStderr output:\n{stderr}"
                message = f"Communication with agent failed: {error_details}"
                self._finalize_and_raise_crash(state.finalize, message, cause=e)
        finally:
            # Auto-finalize any path the except blocks did not. Idempotent, so
            # exactly one AgentEndEvent is produced on every exit path.
            if not state.finalized:
                if state.timeout_hit:
                    assert timeout is not None
                    state.finalize(AgentEndStatus.TIMEOUT, crashed=True, crash_reason=format_timeout_reason(timeout))
                elif state.stopped_early_hit:
                    # NOT a crash, NOT a timeout. The max_turns promotion in
                    # finalize() only fires for COMPLETED, so this survives.
                    state.finalize(AgentEndStatus.STOPPED_EARLY, crashed=False, crash_reason=None)
                else:
                    state.finalize(AgentEndStatus.COMPLETED, crashed=False, crash_reason=None)
            self._active_transport = None

        # Only the flag here, never the wall clock: a drift during post-loop
        # cleanup would misclassify a successful turn as a timeout.
        if state.timeout_hit:
            assert timeout is not None
            raise TurnTimeoutError(timeout, iteration=self._iteration)

        self._update_state_from_messages(state.messages)

        # This turn completed successfully — the iteration increment stands.
        self._end_turn_ok()

        # The collector's reduction of the events emitted above.
        return collector.build_turn_record()

    async def _pump_messages(
        self,
        state: _ClaudeTurnState,
        query_kwargs: dict[str, Any],
        deadline: float | None,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        """Drive the SDK message stream for one turn (extracted from ``communicate``).

        Kept separate so the cooperative-stop check keeps ``communicate`` under
        ruff's statement cap. ``query`` is still resolved as a module global at
        call time, so ``patch("...claude_code_agent.query", ...)`` mocks work.

        Two break conditions, and the ORDER MATTERS:

        - The wall-clock guard runs at the TOP, so an over-deadline message is
          DISCARDED — no append, no events. Do NOT move it to a post-loop check.
        - The cooperative stop runs AFTER ``state.dispatch(message)``, so a watcher
          can flip its flag on THIS message and the next is never pulled.
        """
        async for message in query(**query_kwargs):
            if deadline is not None and time.monotonic() > deadline:
                state.timeout_hit = True
                self._log.warning("Turn timeout reached mid-stream; breaking out of message loop")
                break
            state.dispatch(message)
            if should_stop is not None and should_stop():
                state.stopped_early_hit = True
                self._log.debug("Cooperative stop requested; ending message loop at this boundary")
                break

    def _build_claude_query(
        self,
        user_input: str,
        timeout: float | None,
        max_turns: int | None,
        stderr_callback: Callable[[str], None],
    ) -> tuple[ClaudeAgentOptions, SubprocessCLITransport | None, str | None]:
        """Build the SDK options (+ a timeout-only transport) for one turn.

        ``transport`` is None unless a ``timeout`` is set: it is pre-constructed
        only so the watchdog can hard-kill the subprocess. ``effective_model`` may
        be None on a DirectRoute with no configured model. ``stderr_callback`` is
        wired in here but owned by ``communicate``.
        """
        assert self.working_directory is not None  # guaranteed by communicate's guard above

        # Process plugins: copy from config and replace env vars in paths.
        plugins = process_plugins(self.config.plugins or [], log=self._log)  # type: ignore[arg-type]

        # Per-turn cost-correlation headers (LiteLLM only): the run/task tag plus
        # this turn's iteration, so the proxy-side cost log joins to the turn.
        cost_log_tags: dict[str, str] | None = None
        if self._cost_log_tags is not None:
            cost_log_tags = {**self._cost_log_tags, "x-ce-iteration": str(self._iteration)}
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
            max_turns=max_turns,
            plugins=plugins,  # type: ignore[arg-type]
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
        overrides the base ClassVar with the resolved value.

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

    def _finalize_commands(
        self, pending_commands: dict[str, dict[str, Any]], messages: list[Message]
    ) -> list[CommandTelemetry]:
        """Convert pending commands to a finalized list, marking unresolved ones as unknown."""
        commands: list[CommandTelemetry] = []
        unknown_status_count = 0

        for tool_id, cmd_data in pending_commands.items():
            cmd = cmd_data["telemetry"]
            if cmd.result_status is None:
                # Unknown status and unknown duration are one fact: nothing
                # resolved this command, so nothing timed it. `duration_ms` is
                # deliberately left None (CE058).
                # Rationale: .claude/notes/agents.md § Why only a RESOLVED tool is timed
                cmd.result_status = "unknown"
                unknown_status_count += 1
                self._log.warning(
                    f"Command {cmd.tool_name}:{tool_id} completed without tool result. "
                    + "Status set to 'unknown'. This may indicate agent interruption or SDK issue."
                )
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
    def _price_from_buckets(usage: TokenUsage, model: str | None) -> float | None:
        """Price the four token buckets at ``model``'s list rate.

        ``None`` when ``model`` is unset or absent from the rate card.
        """
        if not model:
            return None
        return calculate_cost(
            model,
            uncached_input_tokens=usage.uncached_input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_input_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
        )

    @staticmethod
    def _backfill_cost(usage: TokenUsage, model: str | None) -> TokenUsage:
        """Price the token buckets when the SDK gave no cost (timeout / kill).

        A timed-out or killed turn has no terminal ``ResultMessage``, so the cost
        is absent even though the tokens are fully captured. A no-op when the cost
        is already set or the model is unpriced.
        """
        if usage.total_cost_usd is not None or not model:
            return usage
        cost = ClaudeCodeAgent._price_from_buckets(usage, model)
        if cost is not None:
            usage.total_cost_usd = cost
        else:
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

        An unpriced model sets the cost to ``None`` **and warns**: a silent
        ``None`` makes the orchestrator skip the ``max_usd`` gate with no
        diagnostic.

        Rationale: .claude/notes/agents.md § Cost: the stream versus the rate card
        """
        cost = ClaudeCodeAgent._price_from_buckets(usage, model)
        usage.total_cost_usd = cost
        if cost is None:
            logger.warning(
                "No pricing for litellm model %r; turn cost left unset (max_usd gate will be skipped)", model
            )

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
        """Classify a tool result into a ToolEndStatus (promotes the old string-scan)."""
        if not is_error:
            return ToolEndStatus.OK
        text = str(content).lower() if content is not None else ""
        if any(phrase in text for phrase in cls._PERMISSION_PHRASES):
            return ToolEndStatus.PERMISSION_DENIED
        return ToolEndStatus.ERROR

    @staticmethod
    def _synthesize_subagent_terminal_message(message: Any, model: str | None) -> AssistantMessageTelemetry | None:
        """Materialize a sub-agent's TERMINAL generation as an AssistantMessage.

        A sub-agent's intermediate generations bubble into the parent stream as
        ``parent_tool_use_id``-tagged messages, but its terminal one is delivered
        as the Agent tool RESULT and never streamed. Synthesizing it puts the
        sub-agent's full lifecycle in the transcript, so per-sub-agent usage is
        recoverable by grouping on that id.

        ``tool_use_result.usage`` is the terminal call's own breakdown — complete,
        and terminal-only, so it does NOT overlap the bubbled intermediates.
        Returns None for a non-sub-agent tool result (no ``agentId``).

        The token total is unaffected: it derives from ``model_usage``, which
        ignores this transcript, so the synthetic message is purely additive.
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

        # Never streamed, so no window exists to measure: None (unknown), not 0.0.
        # These bounds are an admitted PLACEHOLDER, which is why they are
        # deliberately NOT on the turn's `TurnClock` — the only wall stamp in this
        # harness that is not. `generation_duration_ms is None` plus a set
        # `parent_tool_use_id` is exactly what excludes this message from the
        # subtraction and the head/tail bracket, so the stamp is read by no
        # arithmetic and has no basis to share.
        now = datetime.now()
        return AssistantMessageTelemetry(
            started_at=now,
            completed_at=now,
            generation_duration_ms=None,
            content_blocks=([ContentBlock(block_type="text", sequence=0, text=result_text)] if result_text else []),
            tool_use_ids=[],
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
            cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
            reasoning_tokens=0,
            model=model,
            message_id=f"subagent-{tool_use_id}",
            parent_tool_use_id=tool_use_id,
        )

    def _resolve_pending_command(
        self,
        tool_use_id: str,
        is_error: bool,
        content: Any,
        pending_commands: dict[str, dict[str, Any]],
        processed_results: set[str],
        *,
        now: datetime,
    ) -> None:
        """Match a tool result back to its pending command and update status/duration.

        Args:
            tool_use_id: The tool use ID from the result
            is_error: Whether the tool execution resulted in an error
            content: The result content (string or structured)
            pending_commands: Map of tool_id -> {telemetry, command_start_time}
            processed_results: Set of already-processed tool IDs (for duplicate detection)
            now: This turn's ``TurnClock`` reading, passed in rather than read
                here: the span stamped below is clipped against window bounds the
                same clock produced.
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

            # `execution_started_at` is RECONSTRUCTED by subtracting the measured
            # monotonic duration from the turn clock's reading, which is exact
            # because the turn clock is itself monotonic-derived.
            cmd.execution_completed_at = now
            cmd.execution_started_at = cmd.execution_completed_at - timedelta(milliseconds=duration_ms)

            if is_error:
                cmd.error_message = content_str

                # Abnormal flow: warn so it surfaces without DEBUG enabled.
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
