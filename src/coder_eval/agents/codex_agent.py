"""Codex agent implementation using the official OpenAI Codex SDK."""

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._logging import PrefixedAdapter, log_raw_sdk_event
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import ThreadedWatchdog
from coder_eval.config import settings
from coder_eval.errors import (
    AgentCrashError,
    TurnTimeoutError,
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
    Enforcement,
    HarnessContract,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
    UsageGranularity,
)
from coder_eval.orchestration.plugin_staging import link_or_copy
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StopReason,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
    end_status_for,
)
from coder_eval.timing import close_window


logger = logging.getLogger(__name__)

# Approval mode — the SAME for every permission mode. Despite the name this is
# the "run autonomously, never prompt, no reviewer" mode: in-sandbox operations
# execute directly, and only escalations BEYOND the sandbox are refused. The
# alternative puts a server-side reviewer in the loop that can flake.
# Rationale: .claude/notes/agents.md § Codex runs full-access on every permission mode
_CODEX_APPROVAL_MODE = "deny_all"

# Provider id registered in thread config when CODEX_BASE_URL routes to a
# custom endpoint.
_CUSTOM_PROVIDER_ID = "custom"

# apply_patch statuses meaning the patch did not apply. Used ONLY to classify the
# Write telemetry honestly, never to fail or retry the turn: "declined" needs an
# approval reviewer, which no permission mode configures, and "failed" (a diff
# context mismatch) is self-healed by the model within the turn. Grading checks
# the actual files regardless.
_FILE_CHANGE_FAILURE_STATUSES = frozenset({"failed", "declined"})

# Thread-item types carrying transcript CONTENT or session metadata rather than a
# tool call. Everything ELSE streamed as item/started+item/completed is treated as
# a tool call, so a new Codex tool kind is captured automatically instead of being
# silently dropped.
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

# Codex item type -> the canonical (Claude) vocabulary every criterion is written
# against. Unknown types fall back to the raw item type, so they still surface.
# Rationale: .claude/notes/agents.md § Tool-name and argument normalization
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

# The collabAgentToolCall.tool value that spawns a NEW sub-agent; "wait" and
# messaging act on an already-spawned one, so only spawns register a child thread
# to recover. Lowercased to match _status_value's normalization.
_COLLAB_SPAWN_TOOL = "spawnagent"

# Tool names for the raw ResponseItem function calls in a sub-agent's on-disk
# rollout, which is the only place its inner tool calls survive.
# Rationale: .claude/notes/agents.md § Codex rollout rebuild
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

# A default rather than catching StopIteration: one raised inside
# ``asyncio.to_thread`` is converted by asyncio into a TypeError that escapes
# ``except StopIteration``, masking the real turn-failure reason.
_STREAM_DONE = object()


def _ms_to_dt(ms: int | None) -> datetime:
    """Convert a Codex Unix-millisecond timestamp to a datetime (now() if absent)."""
    if ms is None:
        return datetime.now()
    return datetime.fromtimestamp(ms / 1000)


class _ItemTiming(NamedTuple):
    """When a Codex tool item ran, as the four fields CommandTelemetry records."""

    timestamp: datetime
    execution_started_at: datetime | None
    execution_completed_at: datetime | None
    duration_ms: float | None


def _item_timing(started_ms: int | None, completed_ms: int | None, sdk_duration_ms: float | None) -> _ItemTiming:
    """Resolve a tool item's timing from the SDK's millisecond stamps.

    One helper for all three telemetry builders, so a command, a file change and
    an MCP call cannot disagree about what a missing stamp means.

    BOTH stamps or neither: pairing a real stamp with ``_ms_to_dt(None)`` — which
    is ``datetime.now()`` — fabricates an interval out of one reading and the
    current time, so the raw values are checked BEFORE conversion.

    Without them, the SDK item's own ``duration_ms`` is used only when it reports
    something. A ``0`` there is an UNREPORTED duration, not an instant command (70
    of 211 commands in one nightly reported ``0`` for calls the message gaps show
    took seconds), so it becomes ``None`` (CE058).

    ``generation_completed_at`` is deliberately absent for all three: it means
    "when the model finished emitting the ``tool_use`` block", which Codex's
    stream does not carry per tool, and the flush time would be a guess.
    """
    if started_ms is not None and completed_ms is not None:
        started = _ms_to_dt(started_ms)
        return _ItemTiming(
            timestamp=started,
            execution_started_at=started,
            execution_completed_at=_ms_to_dt(completed_ms),
            # Clamped for clock skew; both bounds stay as reported so the
            # anomaly remains visible in the record.
            duration_ms=max(0.0, float(completed_ms - started_ms)),
        )
    # Explicit `is None or <= 0`, never `sdk_duration_ms or None`: that is the
    # CE058 coalesce read backwards, and hides the decision being made.
    duration = None if sdk_duration_ms is None or sdk_duration_ms <= 0 else float(sdk_duration_ms)
    return _ItemTiming(
        timestamp=datetime.now(),
        execution_started_at=None,
        execution_completed_at=None,
        duration_ms=duration,
    )


def _status_value(status: Any) -> str:
    """Normalize a Codex status (enum or str) to its lowercase string value."""
    value = getattr(status, "value", status)
    return str(value).lower() if value is not None else ""


def _fresh_input_tokens(raw_input: int, cached: int) -> int:
    """The fresh (uncached) prompt slice = tokens written to cache this call.

    Single definition of the OpenAI cache-write convention, shared by the
    per-message and per-turn paths so they cannot drift.
    """
    return max(raw_input - cached, 0)


class _ThreadTotals(NamedTuple):
    """A snapshot of the Codex SDK's thread-cumulative ``ThreadTokenUsage.total``.

    Held across turns (the thread outlives the turn) so each turn reports its own
    slice, not the running total. ``input`` is the full prompt count, cached
    prefix INCLUDED — the SDK's convention, not ours.

    Rationale: .claude/notes/agents.md § Codex rollout rebuild
    """

    input: int = 0
    output: int = 0
    cached: int = 0

    def since(self, baseline: "_ThreadTotals") -> "_ThreadTotals":
        """This turn's tokens = the cumulative snapshot minus the previous one.

        A total that moved BACKWARDS means the thread restarted, so the snapshot
        is already turn-local: return it whole rather than clamping to zero and
        losing the turn.
        """
        if self.input < baseline.input or self.output < baseline.output or self.cached < baseline.cached:
            return self
        return _ThreadTotals(
            input=self.input - baseline.input,
            output=self.output - baseline.output,
            cached=self.cached - baseline.cached,
        )


def _message_uncached_input(m: AssistantMessage) -> int:
    """A captured generation's fresh (uncached) input.

    Codex children carry 0 ``cache_creation`` (no cache-write fee), but both are
    folded defensively so nothing is dropped if that changes.
    """
    return m.input_tokens + m.cache_creation_tokens


# Wire protocol for the custom model provider. The pinned codex binary only
# supports the Responses API (it rejects `wire_api = "chat"` as "no longer
# supported"), so this is a fixed constant, not an operator knob.
_CODEX_WIRE_API = "responses"

# Login-shell profiles generated into the per-task HOME. ``.bash_profile`` is
# what ``bash -l`` reads and ``.profile`` covers sh/dash; the zsh trio covers
# macOS, where ``.zshenv`` runs in EVERY zsh, ``.zprofile`` in login shells
# (AFTER /etc/zprofile's path_helper) and ``.zshrc`` in interactive ones,
# including codex's shell snapshot.
# Rationale: .claude/notes/agents.md § Codex login-shell PATH restoration
_LOGIN_PROFILE_NAMES = (".bash_profile", ".profile", ".zshenv", ".zprofile", ".zshrc")
_ZSH_PROFILE_NAMES = frozenset({".zshenv", ".zprofile", ".zshrc"})


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


class _CodexTurnState:
    """Per-turn mutable scratch state for one ``CodexAgent.communicate`` call.

    Holds the stream-pump locals and the transcript reconstruction buffers, with
    one method per notification kind plus ``dispatch`` (True on ``turn/completed``
    to break the pump), ``_flush_message`` and ``finalize``.

    ``commands`` and ``messages`` are the SAME list objects ``communicate`` owns,
    held by identity (no copy), so a mid-turn crash keeps the partial transcript.

    The finalize inputs are COMMITTED by ``communicate`` only after the pump
    returns cleanly, defaulting to None/None/"" — so a crashed turn finalizes from
    the captured messages, and the live pump scratch is intentionally NOT what
    finalize reads.
    """

    def __init__(
        self,
        agent: "CodexAgent",
        *,
        emit: CompositeStreamCallback,
        task_id: str,
        turn_id: str,
        collector: EventCollector,
        commands: list[CommandTelemetry],
        messages: list[TranscriptMessage],
        user_input: str,
        iteration: int,
        turn_start_time: float,
    ) -> None:
        self._agent = agent
        self.emit = emit
        self.task_id = task_id
        self.turn_id = turn_id
        self.collector = collector
        self.commands = commands
        self.messages = messages
        self.user_input = user_input
        self.iteration = iteration
        self.turn_start_time = turn_start_time
        self.timeout_hit = False
        self.stop_reason: StopReason | None = None
        self.finalized = False

        # Live pump scratch (set during streaming).
        self.turn_result: Any = None
        self.latest_token_usage: Any = None
        self.agent_message_chunks: list[str] = []
        # Sequence per executable item, assigned at item/started and reused at
        # item/completed via this id->seq map.
        self.next_sequence = 0
        self.seq_by_id: dict[str, int] = {}
        # Spawned sub-agent child thread id -> spawning Agent call tool_use_id.
        self.collab_spawn_by_thread: dict[str, str] = {}
        # (child_thread_id, spawning Agent tool_use_id, spawned model) per spawn.
        self.spawned_children: list[tuple[str, str, str | None]] = []
        # child thread id -> returned message (fallback when the rollout is absent).
        self.collab_results: dict[str, str] = {}

        # Assistant-transcript reconstruction buffers (one AssistantMessage per gen).
        self.open_blocks: list[ContentBlock] = []
        self.open_start_ms: int | None = None
        self.open_end_ms: int | None = None
        # Where the NEXT generation window starts: the previous flush's end. None
        # until the first flush, which falls back to its own first item — the SDK
        # gives no "turn began" stamp, and inventing one from time.time() would
        # mix our clock with the SDK's inside one subtraction.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.gen_mark_ms: int | None = None
        self.start_ms_by_id: dict[str, int] = {}
        self.blocks_by_id: dict[str, ContentBlock] = {}
        # Tools that emitted item/started but not item/completed; whatever remains
        # at turn end is an orphan, force-closed unresolved.
        self.open_tools: dict[str, CommandTelemetry] = {}
        # Text-less reasoning blocks, resolved at flush once reasoning tokens known.
        self.reasoning_placeholders: list[ContentBlock] = []
        self.gen_index = 0

        # Finalize inputs, COMMITTED by communicate after a clean pump return.
        # Defaults are the crash values (no terminal usage; format from messages).
        self.sdk_token_usage: Any = None
        self.result_turn: Any = None
        self.result_text: str = ""

    def _record_block(self, block: ContentBlock, item_id: str, completed_ms: int | None) -> None:
        self.open_blocks.append(block)
        start_ms = self.start_ms_by_id.get(item_id)
        if start_ms is not None and (self.open_start_ms is None or start_ms < self.open_start_ms):
            self.open_start_ms = start_ms
        if completed_ms is not None and (self.open_end_ms is None or completed_ms > self.open_end_ms):
            self.open_end_ms = completed_ms

    def _flush_message(self, last: Any) -> None:
        """Cut the open buffer into AssistantMessage(s) for one generation.

        ``last`` is the SDK breakdown for the generation that produced these
        blocks (None for a safety flush). Emits ONE sub-message per block kind,
        all sharing this generation's ``message_id``.

        Rationale: .claude/notes/agents.md § Why the generation is split into sub-messages
        """
        if not self.open_blocks:
            self.reasoning_placeholders = []
            return
        # Per-generation tokens from the matching tokenUsage `last` delta.
        cached = (getattr(last, "cached_input_tokens", 0) or 0) if last else 0
        raw_input = (getattr(last, "input_tokens", 0) or 0) if last else 0
        total_output = (getattr(last, "output_tokens", 0) or 0) if last else 0
        # OpenAI bills no separate cache-write fee, so cache_creation is 0.
        gen_input = _fresh_input_tokens(raw_input, cached)
        gen_cache_write = 0
        reasoning_tok = (getattr(last, "reasoning_output_tokens", 0) or 0) if last else 0
        # A text-less reasoning block becomes a placeholder when reasoning was
        # billed, and is dropped otherwise.
        if self.reasoning_placeholders:
            if reasoning_tok > 0:
                for blk in self.reasoning_placeholders:
                    blk.thinking = "_Reasoning hidden by OpenAI policy_"
            else:
                for blk in self.reasoning_placeholders:
                    if blk in self.open_blocks:
                        self.open_blocks.remove(blk)
            self.reasoning_placeholders = []
        if not self.open_blocks:
            self.open_start_ms = self.open_end_ms = None
            return

        thinking_blocks = [b for b in self.open_blocks if b.block_type == "thinking"]
        action_blocks = [b for b in self.open_blocks if b.block_type != "thinking"]
        # From the PREVIOUS flush's end, not this generation's first item: the
        # SDK stamps an item with the moment it began EXECUTING, so seeding there
        # discards the model time that produced it.
        mark_ms = self.gen_mark_ms if self.gen_mark_ms is not None else self.open_start_ms
        window_end_ms = self.open_end_ms if self.open_end_ms is not None else self.open_start_ms
        mark = _ms_to_dt(mark_ms)
        completed = _ms_to_dt(window_end_ms)
        # The RAW window, extended to the LAST item's completion — so a
        # generation containing a tool call already CONTAINS its execution, and
        # the collector takes it back out. That is also what makes the sub-message
        # split safe: the two specs SHARE these bounds, so the overlap is
        # subtracted once rather than once per part.
        started, gen_ms = close_window(
            mark=mark,
            now=completed,
            item_start=_ms_to_dt(self.open_start_ms) if self.open_start_ms is not None else None,
        )
        message_id = f"{self.turn_id}-msg-{self.gen_index}"

        # Output split: reasoning portion to the thinking row, the remainder to
        # the action row. With only one kind present, that kind gets all output.
        think_out = reasoning_tok if action_blocks else total_output
        action_out = max(total_output - reasoning_tok, 0) if thinking_blocks else total_output

        # Thinking first. The FIRST carries the gen's input/cache: per-CALL
        # billing figures that must not be split. Generation TIME is a property
        # of the content, so it IS apportioned below.
        specs: list[tuple[list[ContentBlock], int, int]] = []
        if thinking_blocks:
            specs.append((thinking_blocks, think_out, reasoning_tok))
        if action_blocks:
            specs.append((action_blocks, action_out, 0))

        # By OUTPUT-TOKEN share, the last taking the remainder so the parts
        # reconstruct gen_ms to float precision (shares round to 1e-6 ms, so do
        # not assert exact equality on a measured window). With no output anywhere,
        # split evenly. The evalboard's twin weighs by CONTENT SIZE instead, which
        # is deliberate, not an oversight to unify.
        out_total = sum(out_tok for _, out_tok, _ in specs)
        gen_parts: list[float] = []
        assigned = 0.0
        for idx, (_, out_tok, _) in enumerate(specs):
            if idx == len(specs) - 1:
                gen_parts.append(gen_ms - assigned)
            else:
                share = round(gen_ms * (out_tok / out_total if out_total > 0 else 1 / len(specs)), 6)
                gen_parts.append(share)
                assigned += share

        for idx, (blocks, out_tok, reas_tok) in enumerate(specs):
            for i, blk in enumerate(blocks):
                blk.sequence = i
            first = idx == 0
            self.messages.append(
                AssistantMessage(
                    started_at=started,
                    completed_at=completed,
                    generation_duration_ms=gen_parts[idx],
                    content_blocks=blocks,
                    tool_use_ids=[b.tool_use_id for b in blocks if b.block_type == "tool_use" and b.tool_use_id],
                    input_tokens=gen_input if first else 0,
                    output_tokens=out_tok,
                    cache_creation_tokens=gen_cache_write if first else 0,
                    cache_read_tokens=cached if first else 0,
                    reasoning_tokens=reas_tok,
                    model=self._agent._effective_model(),
                    message_id=message_id,
                )
            )
        self.gen_index += 1
        # A message was appended, so the next window starts where this one
        # ended. Both early returns above leave the mark alone on purpose.
        if window_end_ms is not None:
            self.gen_mark_ms = window_end_ms
        self.open_blocks = []
        self.open_start_ms = None
        self.open_end_ms = None

    @property
    def ended_cleanly(self) -> bool:
        """True once the pump broke on a ``should_stop`` reason.

        A non-crash termination, so an exception raised while tearing the stream
        down afterwards must not be escalated into a retry.
        """
        return self.stop_reason is not None

    def dispatch(self, notification: Any) -> bool:
        """Route a notification to its handler. Returns True on ``turn/completed``
        (a valid TurnCompletedNotification) so the pump loop breaks."""
        root = _get_item_root(notification)
        method = notification.method
        log_raw_sdk_event(
            self._agent._log,
            repr_target=notification,
            attr_target=root,
            method=method,
            root_type=getattr(root, "type", None),
        )
        if method == "item/started":
            self.on_item_started(notification)
        elif method == "item/completed":
            self.on_item_completed(notification)
        elif method == "item/agentMessage/delta":
            self.on_agent_message_delta(notification)
        elif method == "thread/tokenUsage/updated":
            self.on_token_usage_updated(notification)
        elif method == "turn/completed":
            return self.on_turn_completed(notification)
        return False

    def on_item_started(self, notification: Any) -> None:
        """Emit ToolStartEvent + record the tool_use block for every tool-like item."""
        root = _get_item_root(notification)
        if root is None:
            return
        # Record the start time for every item kind so flushed messages get real timing.
        item_id = getattr(root, "id", None)
        started_at_ms = getattr(notification.payload, "started_at_ms", None)
        if item_id is not None and started_at_ms is not None:
            self.start_ms_by_id[item_id] = started_at_ms
        root_type = getattr(root, "type", None)
        # Any item that isn't transcript content is a tool call (generic capture).
        if root_type is not None and root_type not in _CONTENT_ITEM_TYPES:
            tool_id = item_id or f"{root_type}_{self.next_sequence}"
            self.seq_by_id[tool_id] = self.next_sequence
            # Recorded on the START telemetry too: close_open_tools publishes
            # this object verbatim for an orphan, and without it an unresolved
            # call cannot be placed on a timeline at all.
            started_at = _ms_to_dt(started_at_ms) if started_at_ms is not None else None
            start_tel = CommandTelemetry(
                tool_name=self._agent._tool_name(root_type),
                tool_id=tool_id,
                timestamp=started_at or datetime.now(),
                execution_started_at=started_at,
                parameters=self._agent._tool_parameters(root, root_type),
                sequence_number=self.next_sequence,
            )
            self.open_tools[tool_id] = start_tel
            self.emit.on_event(ToolStartEvent(task_id=self.task_id, turn_id=self.turn_id, tool=start_tel))
            self.next_sequence += 1
            # is_error is patched at item/completed even after the message is
            # flushed, because the block is held by reference.
            block = ContentBlock(block_type="tool_use", sequence=0, tool_use_id=tool_id)
            self.blocks_by_id[tool_id] = block
            self._record_block(block, tool_id, None)

    def on_item_completed(self, notification: Any) -> None:
        """Emit ToolEndEvent + capture telemetry / sub-agents / transcript blocks."""
        root = _get_item_root(notification)
        if root is None:
            return
        completed_ms = getattr(notification.payload, "completed_at_ms", None)
        root_type = getattr(root, "type", None)
        if root_type is not None and root_type not in _CONTENT_ITEM_TYPES:
            tool_id = getattr(root, "id", None) or f"{root_type}_{self.next_sequence}"
            seq = self.seq_by_id.get(tool_id, self.next_sequence)
            # This tool is now resolved — drop it from the orphan set.
            self.open_tools.pop(tool_id, None)

            # `on_item_started` banked the start; passing both in is what lets
            # the builders record real bounds instead of the SDK's frequent 0.
            telemetry, is_error = self._agent._telemetry_for_item(
                root,
                root_type,
                tool_id,
                seq,
                started_ms=self.start_ms_by_id.get(tool_id),
                completed_ms=completed_ms,
            )
            if telemetry:
                self.commands.append(telemetry)
            self.emit.on_event(
                ToolEndEvent(
                    task_id=self.task_id,
                    turn_id=self.turn_id,
                    tool=telemetry
                    or CommandTelemetry(
                        tool_name=self._agent._tool_name(root_type),
                        tool_id=tool_id,
                        timestamp=datetime.now(),
                        sequence_number=seq,
                    ),
                    status=ToolEndStatus.ERROR if is_error else ToolEndStatus.OK,
                )
            )
            # Patch the block recorded at item/started, and extend the still-open
            # message's end time.
            if tool_id in self.blocks_by_id:
                self.blocks_by_id[tool_id].is_error = is_error
            if (
                self.open_blocks
                and completed_ms is not None
                and (self.open_end_ms is None or completed_ms > self.open_end_ms)
            ):
                self.open_end_ms = completed_ms

            # Codex's native multi-agent calls land here as collabAgentToolCall items.
            if root_type == "collabAgentToolCall":
                self._agent._handle_collab_completion(
                    root,
                    tool_id,
                    self.collab_spawn_by_thread,
                    self.spawned_children,
                    self.collab_results,
                )

        elif root_type == "reasoning":
            # OpenAI never returns raw CoT, so a text-less item becomes a
            # placeholder, resolved with its token count at flush.
            reasoning_id = getattr(root, "id", f"reasoning_{self.next_sequence}")
            parts = getattr(root, "content", None) or getattr(root, "summary", None) or []
            text = "\n".join(p for p in parts if p)
            block = ContentBlock(block_type="thinking", sequence=0, thinking=text or None)
            self._record_block(block, reasoning_id, completed_ms)
            if not text:
                self.reasoning_placeholders.append(block)

        elif root_type == "agentMessage":
            # The message is cut at the following tokenUsage event (the
            # generation boundary), not here.
            message_item_id = getattr(root, "id", f"msg_{self.next_sequence}")
            text = getattr(root, "text", "") or ""
            if text:
                self._record_block(
                    ContentBlock(block_type="text", sequence=0, text=text),
                    message_item_id,
                    completed_ms,
                )

    def on_agent_message_delta(self, notification: Any) -> None:
        """Emit TextChunkEvent for streaming assistant text."""
        if notification.payload:
            delta = getattr(notification.payload, "delta", None)
            if delta:
                self.agent_message_chunks.append(delta)
                self.emit.on_event(TextChunkEvent(task_id=self.task_id, turn_id=self.turn_id, text=delta))

    def on_token_usage_updated(self, notification: Any) -> None:
        """One per generation → cut a message. Carries `total` (cumulative over the
        whole THREAD, i.e. every turn so far) and `last` (this generation's delta)."""
        if notification.payload:
            self.latest_token_usage = getattr(notification.payload, "token_usage", None)
            self._flush_message(getattr(self.latest_token_usage, "last", None))

    def on_turn_completed(self, notification: Any) -> bool:
        """Capture the final Turn. Returns True (break the pump) iff the payload is
        a valid TurnCompletedNotification."""
        from openai_codex.generated.v2_all import TurnCompletedNotification

        if isinstance(notification.payload, TurnCompletedNotification):
            self.turn_result = notification.payload.turn
            return True
        return False

    def close_open_tools(self) -> None:
        """Force-close any tool that started but never completed (an orphan) as
        ``unresolved``, so its transcript block keeps a real tool name + count."""
        for start_tel in sorted(self.open_tools.values(), key=lambda t: getattr(t, "sequence_number", 0)):
            start_tel.result_status = "unknown"
            self.emit.on_event(
                ToolEndEvent(
                    task_id=self.task_id,
                    turn_id=self.turn_id,
                    tool=start_tel,
                    status=ToolEndStatus.UNRESOLVED,
                )
            )
        self.open_tools.clear()

    def finalize(self, status: AgentEndStatus, *, crashed: bool = False, crash_reason: str | None = None) -> None:
        """Emit the terminal TurnEnd + AgentEnd and, on a crash, build the partial
        TurnRecord. Idempotent. Reads the COMMITTED finalize inputs (None/"" on a
        crash) so a crashed turn under-reports nothing it didn't actually commit."""
        if self.finalized:
            return
        self.finalized = True

        # On crash/timeout the SDK total stays None, so fall back to the
        # per-generation tokens on the messages — but the thread baseline still
        # has to move past them, or the NEXT turn's delta re-books this one.
        token_usage = self._agent._token_usage_from_sdk(self.sdk_token_usage)
        if token_usage is None:
            token_usage = self._agent._token_usage_from_messages(self.messages)
            self._agent._advance_usage_baseline(token_usage)
        # AFTER the baseline advance: the SDK total covers the parent thread only,
        # so child tokens must not shift the parent's baseline.
        token_usage = self._agent._fold_subagent_tokens(token_usage, self.messages)

        self.emit.on_event(
            TurnEndEvent(
                task_id=self.task_id,
                turn_id=self.turn_id,
                status=TurnEndStatus(status.value),
                tokens=token_usage,
            )
        )

        model_used = getattr(self.result_turn, "model", None) or self._agent.config.model
        usage = token_usage or TokenUsage()
        # Real assistant text arrives as agentMessage deltas; fall back to the raw
        # Turn dump only when nothing streamed.
        agent_output = self.result_text or (
            self._agent._format_turn_result(self.result_turn) if self.result_turn is not None else ""
        )

        self.emit.on_event(
            AgentEndEvent(
                task_id=self.task_id,
                status=status,
                usage=usage,
                iteration=self.iteration,
                user_input=self.user_input,
                agent_output=agent_output,
                model_used=model_used,
                assistant_turn_count=1,
                messages=self.messages,
                num_turns=1,
                crashed=crashed,
                crash_reason=crash_reason,
                duration_seconds=time.monotonic() - self.turn_start_time,
            )
        )

        if crashed:
            self._agent._capture_partial_turn(self.collector)


@AgentRegistry.register(AgentKind.CODEX, CodexAgentConfig)
class CodexAgent(Agent[CodexAgentConfig]):
    """Implementation of the Agent interface for OpenAI Codex using the Codex SDK."""

    # The pump has a between-items guard where `should_stop` runs; `system_prompt`
    # maps to developer_instructions, ON TOP of the base prompt.
    # Rationale: .claude/notes/agents.md § The system_prompt_semantics marker
    contract = HarnessContract(
        system_prompt=Enforcement.ENFORCED,
        system_prompt_semantics="append",
        plugin_skills=Enforcement.ENFORCED,
        permission_mode=Enforcement.UNSUPPORTED,
        allowed_tools=Enforcement.UNSUPPORTED,
        disallowed_tools=Enforcement.UNSUPPORTED,
        cooperative_stop=True,
        usage_granularity=UsageGranularity.TURN,
    )

    def __init__(
        self,
        config: CodexAgentConfig,
        route: ApiRoute | None = None,
        *,
        instance_name: str = "codex",
        cost_log_tags: dict[str, str] | None = None,
    ):
        """Initialize the Codex agent.

        Args:
            config: Agent configuration
            route: API routing configuration (unused for Codex, kept for interface compatibility)
            instance_name: Short label used to prefix this instance's log records
            cost_log_tags: LiteLLM correlation headers; accepted for factory parity, unused
        """
        super().__init__(config, route or DirectRoute(), cost_log_tags=cost_log_tags)
        self.codex_client: Any = None
        self.thread: Any = None
        # Thread-cumulative snapshot as of the END of the last finalized turn:
        # what makes each turn's usage its own delta, not the running total.
        self._thread_usage_baseline = _ThreadTotals()
        self.working_directory: Path | None = None
        self._env_path_prepend: list[str] = []
        self._login_shell_home: Path | None = None
        # _state / _iteration / _iteration_was_incremented / pending_turn lifecycle
        # bookkeeping lives on the Agent base class (shared defaults + helpers).
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})
        # Live handle to the in-flight turn, so kill()/kill_sync() can interrupt a
        # stuck one: the watchdog's task.cancel() lands only at an await point,
        # which the to_thread offload is what creates.
        self._active_turn_handle: Any = None

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        plugin_root: Path | None = None,
    ) -> None:
        """Initialize and start the Codex agent.

        Args:
            working_directory: Path to the working directory
            env_path_prepend: Absolute directories to prepend to PATH for the
                Codex app-server (typically the resolved
                ``SandboxConfig.mock_path_dirs``), so mock CLIs shadow the
                real ones — same semantics as the Claude agent.
            plugin_tools_dir: Accepted for the ``Agent.start`` signature; Codex does not use it.
            plugin_root: The staged plugin root whose skills are linked into ``.agents/skills/``.
        """
        self.working_directory = Path(working_directory)
        self._env_path_prepend = list(env_path_prepend or [])
        self._setup_login_shell_home()
        self._state = AgentState.WORKING

        try:
            from openai_codex import Codex, CodexConfig

            # Build CodexConfig with environment variables for custom API configuration
            env_override = self._build_codex_env()
            config = CodexConfig(env=env_override) if env_override else None

            # Close any prior client FIRST: start() runs through
            # execute_with_retry, so a retried start would otherwise orphan the
            # previous app-server subprocess and its reader threads.
            self._close_client()
            self.codex_client = Codex(config=config)
            self._log.debug("Codex client initialized")

            # Without this the app-server falls back to an existing ChatGPT
            # login, so headless API-key runs (CI) fail to authenticate.
            api_key = os.getenv("CODEX_API_KEY")
            if api_key:
                try:
                    await self._run_async(self.codex_client.login_api_key, api_key)
                except Exception as exc:
                    self._log.warning(
                        "CodexAgent: login_api_key failed — agent will fall back to env-based auth: %s", exc
                    )

            self._setup_skills(plugin_root)

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
        should_stop: Callable[[], StopReason | None] | None = None,
    ) -> TurnRecord:
        """Send a message to Codex and receive its response.

        Args:
            user_input: The message/prompt to send
            stream_callback: Optional callback for real-time event streaming
            timeout: Hard wall-clock deadline in seconds
            should_stop: The run's stop poll, called after each dispatched
                notification. On a reason the pump breaks, the in-flight turn is
                interrupted (best-effort) and the turn finalizes cleanly with
                ``end_status_for(reason)`` (``crashed=False``).

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

        # The agent is the SOLE emitter: events fan out to an internal
        # EventCollector and the caller's stream_callback.
        task_id = str(self.config.type)  # str() so a plugin subclass with a non-enum kind also works
        collector = EventCollector()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])

        # Codex has no per-API-call boundary: one thread.turn() == one turn_id.
        turn_id = f"codex-{self._iteration}"
        # The same commands/messages lists flow through the pump and finalize.
        # `timeout_hit` is written by the watchdog callback (atomic bool).
        state = _CodexTurnState(
            self,
            emit=emit,
            task_id=task_id,
            turn_id=turn_id,
            collector=collector,
            commands=[],
            messages=[],
            user_input=user_input,
            iteration=self._iteration,
            turn_start_time=turn_start_time,
        )

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
                # A fresh thread counts its cumulative total from zero.
                self._thread_usage_baseline = _ThreadTotals()

            def _on_turn_timeout() -> None:
                state.timeout_hit = True

            with ThreadedWatchdog(
                timeout_seconds=timeout,
                on_timeout=_on_turn_timeout,
                asyncio_task_to_cancel=asyncio.current_task(),
                label=f"Turn timeout ({timeout:g}s)" if timeout else "turn_timeout",
            ):
                self._log.debug("Starting Codex turn...")
                emit.on_event(TurnStartEvent(task_id=task_id, turn_id=turn_id, model=self._effective_model()))

                try:
                    # Committed only on a CLEAN return; a crash skips this, so
                    # finalize reads the defaults and falls back to the messages.
                    state.result_turn, state.sdk_token_usage, state.result_text = await self._run_turn_with_streaming(
                        state, should_stop
                    )
                except asyncio.CancelledError:
                    if state.timeout_hit:
                        self._finalize_and_raise_timeout(state.finalize, timeout or 0)
                    raise
                except Exception as e:
                    if state.timeout_hit:
                        self._finalize_and_raise_timeout(state.finalize, timeout or 0, cause=e)
                    if state.ended_cleanly:
                        # Already stopped on purpose — do not escalate.
                        # Rationale: .claude/notes/agents.md § Why a post-stop exception is not a crash
                        self._log.warning("Ignoring post-stop exception; finalizing cleanly: %s", e)
                    else:
                        self._finalize_and_raise_crash(
                            state.finalize, truncate_crash_message(f"Codex turn failed: {e!s}"), cause=e
                        )

            if state.timeout_hit:
                # Watchdog fired but the pump finished before the cancel landed.
                # Routed through the shared kernel so this path sets _state=ERROR
                # like every other timeout path.
                assert timeout is not None
                self._finalize_and_raise_timeout(state.finalize, timeout)
        except (AgentCrashError, TurnTimeoutError):
            # Already funneled through finalize by the inner handlers.
            raise
        except asyncio.CancelledError:
            # External, or during thread_start before the watchdog block. Close
            # the AgentStart so the event tree stays balanced; finalize is
            # idempotent, so the timeout case is a no-op here.
            if not state.finalized:
                self._finalize_external_cancel(state.finalize)
            raise
        except Exception as e:
            # Failures OUTSIDE the inner turn block, notably thread_start. Without
            # this they escape bare: the orchestrator never drains pending_turn
            # and _iteration stays incremented.
            if state.ended_cleanly and not state.timeout_hit:
                # Same retry-poisoning guard as the inner handler.
                self._log.warning("Ignoring post-stop exception; finalizing cleanly: %s", e)
            else:
                self._finalize_and_raise_crash(
                    state.finalize, truncate_crash_message(f"Codex turn failed: {e!s}"), cause=e
                )

        self._state = AgentState.WORKING
        self._end_turn_ok()

        # Precedence: timeout (raised above) > the stop reason > done.
        # Rationale: .claude/notes/agents.md § Shared turn lifecycle
        status = end_status_for(state.stop_reason) if state.stop_reason is not None else AgentEndStatus.COMPLETED
        state.finalize(status, crashed=False, crash_reason=None)
        return collector.build_turn_record()

    async def stop(self) -> None:
        """Stop the agent and tear down the Codex SDK session.

        ``Codex(config=...)`` eagerly spawns an app-server subprocess plus reader
        threads, so close before nulling the reference or a batch run leaks one
        set per task.
        """
        self._close_client()
        self.thread = None
        self._thread_usage_baseline = _ThreadTotals()
        self._active_turn_handle = None
        self._cleanup_login_shell_home()
        self._mark_stopped()

    async def kill(self) -> None:
        """Force-terminate the agent: interrupt any in-flight turn, then tear down."""
        self._interrupt_active_turn()
        await self.stop()

    def kill_sync(self) -> None:
        """Synchronous abort for the watchdog thread (cannot await coroutines).

        Best-effort: interrupt the in-flight turn so the blocked stream iteration
        unblocks, then close the client. Idempotent.
        """
        self._interrupt_active_turn()
        self._close_client()
        self._cleanup_login_shell_home()

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

        The routing keys are added only under a custom endpoint, where the model is
        an operator-chosen alias (a deployment name on Azure) and two operators'
        ``gpt-5-codex`` deployments are otherwise indistinguishable in run
        artifacts. The HOST, not the full URL, is recorded, so an embedded
        credential cannot leak; the API key is never recorded.
        """
        info: dict[str, Any] = dict(super().get_environment_info())
        base_url = self._resolve_base_url()
        if not base_url:
            return info
        return {
            **info,
            "codex_base_url_host": urlparse(base_url).hostname or "",
            "codex_wire_api": _CODEX_WIRE_API,
            "codex_api_version": self._resolve_api_version() or "",
            "codex_model_is_deployment": True,
        }

    def _setup_skills(self, plugin_root: Path | None) -> None:
        """Link each staged skill into ``.agents/skills/``, where Codex auto-discovers skills.

        Codex scans ``.agents/skills/`` from the working directory up to the repo root,
        so each ``<plugin_root>/skills/<name>`` is symlinked (or copied) into it.

        Rationale: .claude/notes/agents.md § Skills, per harness
        """
        if not self.working_directory or plugin_root is None:
            return
        agents_skills_dir = self.working_directory / ".agents" / "skills"
        agents_skills_dir.mkdir(parents=True, exist_ok=True)
        for skill_dir in sorted((plugin_root / "skills").iterdir()):
            target = agents_skills_dir / skill_dir.name
            if target.is_symlink() and not target.exists():
                target.unlink()
            if not target.exists():
                link_or_copy(skill_dir.resolve(), target)
        self._log.debug("Linked the staged skills into %s", agents_skills_dir)

    @staticmethod
    def _resolve_base_url() -> str | None:
        """Custom Codex endpoint base URL from CODEX_BASE_URL, or None."""
        return os.getenv("CODEX_BASE_URL") or None

    @staticmethod
    def _resolve_api_version() -> str | None:
        """Azure OpenAI ``api-version`` from CODEX_API_VERSION, or None.

        Azure's Responses endpoint requires it on every request, injected as the
        provider's ``query_params``. Other endpoints leave it unset.
        """
        return os.getenv("CODEX_API_VERSION") or None

    def _effective_model(self) -> str | None:
        """Resolve the model: task/CLI ``agent.model`` wins, else CODEX_MODEL.

        Mirrors the Claude agent's precedence, with CODEX_MODEL as the fallback.
        """
        return self.config.model or settings.codex_model

    def _build_codex_env(self) -> dict[str, str] | None:
        """Build the environment passed to the Codex app-server.

        Carries ``CODEX_API_KEY`` and, when the sandbox resolved mock dirs, a PATH
        with those prepended. The base URL is NOT an env var the binary honors —
        it goes through the model provider config instead.

        The SDK merges this PARTIAL dict over ``os.environ`` (normalizing the PATH
        key case-insensitively), so a full PATH value here safely replaces the
        inherited one.
        """
        env: dict[str, str] = {}
        api_key = os.getenv("CODEX_API_KEY")
        if api_key:
            env["CODEX_API_KEY"] = api_key
            self._log.debug("CODEX_API_KEY configured")
        if self._env_path_prepend:
            path_key = next((k for k in os.environ if k.upper() == "PATH"), "PATH")
            env[path_key] = os.pathsep.join([*self._env_path_prepend, os.environ.get(path_key, "")])
            self._log.debug(f"PATH prepend: {os.pathsep.join(self._env_path_prepend)}")
        if self._login_shell_home is not None:
            # Login shells point at the generated profile dir while codex state
            # stays pinned to its real location. HOME steers bash/sh; ZDOTDIR
            # steers zsh, which ignores HOME for dotfile selection when set.
            env["HOME"] = str(self._login_shell_home)
            env["ZDOTDIR"] = str(self._login_shell_home)
            # The binary hard-errors on an explicitly set CODEX_HOME that does
            # not exist, and a host that auths via CODEX_API_KEY never ran
            # `codex login`. Create it before pinning.
            codex_home = self._codex_home()
            codex_home.mkdir(parents=True, exist_ok=True)
            env["CODEX_HOME"] = str(codex_home)
        return env if env else None

    @staticmethod
    def _login_shell_profiles_supported() -> bool:
        """Whether to generate login-shell profile shims (POSIX shells only)."""
        return os.name == "posix"

    def _setup_login_shell_home(self) -> None:
        """Create a per-task HOME whose profiles restore the mock PATH prepend.

        Codex issues every shell command through a LOGIN shell, which re-sources
        the system profile chain and unconditionally RESETS PATH — silently
        dropping the mock-CLI prepend, so bare commands resolve to the REAL CLIs
        (real-tenant contamination). The per-user dotfiles are sourced AFTER that
        chain, so a generated per-task HOME gets the last word.

        Per-task rather than the user's real dotfiles, so parallel tasks with
        different mocks cannot collide. No-op without mock dirs or on non-POSIX
        hosts, where no profile chain resets PATH.

        **Known residual gap:** a NESTED bash/sh login shell inside a command
        re-reads the real profiles and loses the prepend again. Nested zsh keeps
        it, because ZDOTDIR stays exported.

        Rationale: .claude/notes/agents.md § Codex login-shell PATH restoration
        """
        self._cleanup_login_shell_home()
        if not (self._env_path_prepend and self._login_shell_profiles_supported()):
            return
        original_home = os.environ.get("HOME", "")
        # The user's REAL zsh dotfile dir: their own ZDOTDIR, else their home.
        original_zdotdir = os.environ.get("ZDOTDIR", "") or original_home
        # Executed only under a POSIX shell, so ':' regardless of the host.
        quoted_prepend = shlex.quote(":".join(self._env_path_prepend))
        export_line = f'export PATH={quoted_prepend}:"$PATH"'
        # Tracked BEFORE writing, so a failed write cannot orphan it.
        home = Path(tempfile.mkdtemp(prefix="coder-eval-codex-home-"))
        self._login_shell_home = home
        try:
            for name in _LOGIN_PROFILE_NAMES:
                content = self._login_profile_content(
                    name,
                    original_home,
                    export_line,
                    original_zdotdir=original_zdotdir,
                    generated_home=str(home),
                )
                # LF-only no matter which host builds it, or bash sees a literal
                # carriage return at end of line.
                (home / name).write_text(content, encoding="utf-8", newline="\n")
        except Exception:
            self._cleanup_login_shell_home()
            raise
        self._log.debug(f"Login-shell mock-PATH home: {home}")

    @staticmethod
    def _login_profile_content(
        profile_name: str,
        original_home: str,
        export_line: str,
        *,
        original_zdotdir: str = "",
        generated_home: str = "",
    ) -> str:
        """One generated profile: restore the ORIGINAL ``$HOME``, source the
        user's own counterpart, then re-prepend the mock dirs.

        The env HOME pointing at the generated dir exists ONLY so bash selects this
        file; exporting the original back on the first line keeps every ``$HOME``
        consumer on the real home.

        ``.bash_profile`` mimics bash's first-found chain; the ``.profile`` twin
        sources only ``.profile``, since the bash-specific files may contain
        bashisms a POSIX shell would choke on. The zsh files each source their
        EXACT counterpart, because zsh reads ALL of its startup files rather than a
        first-found chain, and ``.zshenv`` re-pins ZDOTDIR after sourcing in case
        the user's own redefined it. Each zsh file re-prepends: /etc/zprofile
        resets PATH between ``.zshenv`` and ``.zprofile``, and a duplicate PATH
        entry is harmless where a lost prepend is contamination.

        Rationale: .claude/notes/agents.md § Codex login-shell PATH restoration
        """
        lines = [
            "# Generated by coder_eval (CodexAgent): the system profile chain resets",
            "# PATH in login shells, dropping the mock-CLI prepend - this restores it.",
        ]
        if original_home:
            orig = shlex.quote(original_home)
            lines.append(f"export HOME={orig}")
        if profile_name in _ZSH_PROFILE_NAMES:
            if original_zdotdir:
                origz = shlex.quote(original_zdotdir)
                lines += [
                    f"if [ -r {origz}/{profile_name} ]; then . {origz}/{profile_name}",
                    "fi",
                ]
            if profile_name == ".zshenv" and generated_home:
                lines.append(f"export ZDOTDIR={shlex.quote(generated_home)}")
        elif original_home:
            orig = shlex.quote(original_home)
            if profile_name == ".bash_profile":
                lines += [
                    f"if [ -r {orig}/.bash_profile ]; then . {orig}/.bash_profile",
                    f"elif [ -r {orig}/.bash_login ]; then . {orig}/.bash_login",
                    f"elif [ -r {orig}/.profile ]; then . {orig}/.profile",
                    "fi",
                ]
            else:
                lines += [
                    f"if [ -r {orig}/.profile ]; then . {orig}/.profile",
                    "fi",
                ]
        lines.append(export_line)
        return "\n".join(lines) + "\n"

    def _cleanup_login_shell_home(self) -> None:
        """Remove the generated per-task HOME (best-effort, idempotent)."""
        home, self._login_shell_home = self._login_shell_home, None
        if home is None:
            return
        shutil.rmtree(home, ignore_errors=True)

    def _build_thread_options(self) -> dict[str, Any]:
        """Build thread_start options from agent config.

        Rationale: .claude/notes/agents.md § Codex runs full-access on every permission mode
        """
        from openai_codex.api import ApprovalMode, Sandbox  # pyright: ignore[reportPrivateImportUsage]

        options: dict[str, Any] = {}

        # Pin the model, or Codex picks its default and two runs silently differ.
        effective_model = self._effective_model()
        if effective_model:
            options["model"] = effective_model
            self._log.debug(f"Codex model pinned to {effective_model}")

        # ON TOP of Codex's base prompt, matching the append-only contract of the
        # shared config field. `base_instructions` (full replacement) is
        # deliberately not exposed.
        if self.config.system_prompt is not None:
            options["developer_instructions"] = self.config.system_prompt

        # ALWAYS full-access: Codex honors no permission_mode (its contract rejects
        # the field). The docker driver is the only OS-level write boundary; the
        # tempdir/host driver is a working directory, not a confinement boundary, so
        # adversarial or untrusted evals belong on docker.
        # Rationale: .claude/notes/agents.md § Codex runs full-access on every permission mode
        options["sandbox"] = Sandbox.full_access
        options["approval_mode"] = ApprovalMode(_CODEX_APPROVAL_MODE)

        tool_config: dict[str, Any] = {}

        # The codex binary has no base-URL env var: a model provider must be
        # defined in config and selected, with env_key naming the key's variable.
        # For Azure, CODEX_API_VERSION adds the required query param and
        # CODEX_MODEL is the deployment name.
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
                # Fixed, not configurable: the pinned binary rejects
                # `wire_api = "chat"` as "no longer supported".
                "wire_api": _CODEX_WIRE_API,
            }
            api_version = self._resolve_api_version()
            if api_version:
                # Azure requires ?api-version=... on every request.
                provider["query_params"] = {"api-version": api_version}
            tool_config["model_providers"] = {_CUSTOM_PROVIDER_ID: provider}
            self._log.debug(
                f"Codex routed via custom provider (host={urlparse(base_url).hostname or '(unknown)'}, "
                + f"api_version={'set' if api_version else 'unset'})"
            )

        if tool_config:
            options["config"] = tool_config

        return options

    def _format_turn_result(self, turn_result: Any) -> str:
        """Format a Codex Turn to a readable string — fallback when no text streamed.

        The Turn payload has no ``final_response`` field, so this only fires when
        streaming produced nothing, dumping the raw Turn for debugging.
        """
        try:
            result_dict = turn_result.model_dump() if hasattr(turn_result, "model_dump") else vars(turn_result)
            return json.dumps(result_dict, indent=2, default=str)
        except Exception as e:
            self._log.warning(f"Failed to format turn result: {e}")
            return str(turn_result)

    async def _run_turn_with_streaming(
        self, state: _CodexTurnState, should_stop: Callable[[], StopReason | None] | None = None
    ) -> tuple[Any, Any, str]:
        """Drive ``turn.stream()`` through the per-turn state, emitting the standard
        event protocol; returns ``(turn_result, latest_token_usage, agent_text)``.

        ``communicate()`` owns the TurnStart/TurnEnd/AgentEnd boundaries; this
        drives the inner pump. ``state`` is mutated IN PLACE, so a mid-turn crash
        keeps the partial.

        ``should_stop`` runs AFTER ``state.dispatch`` (the emission the monitor
        latches on) and BEFORE the next notification is pulled.
        """
        # Starts the turn without blocking, and opens the event stream.
        turn_handle = await self._run_async(self.thread.turn, state.user_input)
        self._active_turn_handle = turn_handle
        stream = await self._run_async(turn_handle.stream)

        stream_iter = iter(stream)
        try:
            while True:
                # Offloaded so the event loop stays free (parallel agents do not
                # serialize) and the watchdog's task.cancel() can land here.
                notification: Any = await asyncio.to_thread(next, stream_iter, _STREAM_DONE)
                if notification is _STREAM_DONE:
                    break
                if state.dispatch(notification):  # True on a valid turn/completed
                    break
                reason = should_stop() if should_stop is not None else None
                if reason is not None:
                    state.stop_reason = reason
                    self._log.debug("Stop requested (%s); ending notification pump at this boundary", reason.value)
                    self._interrupt_active_turn()  # best-effort; stops server-side spend
                    break
        finally:
            self._active_turn_handle = None
            # Close orphan tools, flush trailing blocks no tokenUsage event
            # closed, then close the stream. Runs on EVERY exit path.
            state.close_open_tools()
            state._flush_message(None)
            with contextlib.suppress(Exception):
                await self._run_async(stream.close)

        if state.turn_result is None and not state.ended_cleanly:
            raise RuntimeError("Turn did not complete (no turn/completed notification received)")

        # If streaming surfaced no transcript, rebuild it from the terminal
        # Turn's ordered item list.
        if not state.messages:
            state.messages.extend(self._messages_from_items(getattr(state.turn_result, "items", None), state.turn_id))

        # RUNS on a cap or budget stop, because recovery is also the only writer of
        # the `parent_tool_use_id`-tagged messages `_fold_subagent_tokens` sums — so
        # skipping it drops the child threads' spend from the run's cost entirely.
        # Still SKIPPED on an early-criterion stop: an armed gate has already
        # decided the run, and children may have no rollout yet.
        # Rationale: .claude/notes/agents.md § Codex rollout rebuild
        if state.spawned_children and state.stop_reason is not StopReason.EARLY_CRITERION:
            await self._recover_subagent_tool_calls(
                state.spawned_children,
                state.collab_results,
                state.messages,
                state.commands,
                state.emit,
                state.task_id,
                state.turn_id,
            )

        return state.turn_result, state.latest_token_usage, "".join(state.agent_message_chunks)

    def _messages_from_items(self, items: Any, turn_id: str) -> list[AssistantMessage]:
        """Rebuild the assistant transcript from a Turn's ``items`` list (fallback).

        Same item->block mapping as the streaming path, but Turn items carry no
        per-item timestamps, so there is no window to measure: the bounds fall back
        to now() and ``generation_duration_ms`` is None, never 0.0 (CE058).
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
                    generation_duration_ms=None,
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
                # Any tool-like item, mirroring the streaming path's broad capture.
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

        Per-kind for the items we understand; an empty dict for an unknown kind,
        which is still emitted, just without parameters.
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
        self,
        root: Any,
        root_type: str | None,
        tool_id: str,
        seq: int,
        *,
        started_ms: int | None = None,
        completed_ms: int | None = None,
    ) -> tuple[CommandTelemetry | None, bool]:
        """Build (telemetry, is_error) for a completed tool item.

        commandExecution/fileChange keep their rich extractors; every other kind
        routes through the generic builder so it still produces countable
        telemetry. The SDK's millisecond stamps arrive as ARGUMENTS rather than
        being read back out of the reducer, so each builder stays pure.
        """
        if root_type == "commandExecution":
            exit_code = getattr(root, "exit_code", None)
            return self._extract_command_telemetry(root, seq, started_ms, completed_ms), exit_code != 0
        if root_type == "fileChange":
            changes = getattr(root, "changes", []) or []
            status_str = _status_value(getattr(root, "status", "completed"))
            failed = status_str in _FILE_CHANGE_FAILURE_STATUSES
            return (
                self._extract_file_change_telemetry(tool_id, changes, status_str, seq, started_ms, completed_ms),
                failed,
            )
        return self._extract_generic_telemetry(root, root_type, tool_id, seq, started_ms, completed_ms)

    def _extract_generic_telemetry(
        self,
        root: Any,
        root_type: str | None,
        tool_id: str,
        seq: int,
        started_ms: int | None = None,
        completed_ms: int | None = None,
    ) -> tuple[CommandTelemetry | None, bool]:
        """CommandTelemetry for any tool item without a dedicated extractor.

        Reads status / duration / error generically, so MCP calls, web searches,
        collab-agent spawns and future kinds all render and count uniformly.
        """
        try:
            status_str = _status_value(getattr(root, "status", "") or "")
            err = getattr(root, "error", None)
            success = getattr(root, "success", None)
            is_error = bool(err) or success is False or status_str in _FILE_CHANGE_FAILURE_STATUSES
            timing = _item_timing(started_ms, completed_ms, getattr(root, "duration_ms", None))
            return (
                CommandTelemetry(
                    tool_name=self._tool_name(root_type),
                    tool_id=tool_id,
                    timestamp=timing.timestamp,
                    execution_started_at=timing.execution_started_at,
                    execution_completed_at=timing.execution_completed_at,
                    duration_ms=timing.duration_ms,
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

        1. SPAWN: remember which Agent call owns each spawned child thread, and the
           spawned model. Follow-up ``wait``/messaging calls reuse the same thread
           and are NOT new sub-agents.
        2. RESULT: stash the child's returned message as a FALLBACK, used only when
           the child's rollout cannot be found later.

        Rationale: .claude/notes/agents.md § Codex rollout rebuild
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

        Per inner call, emits one ``CommandTelemetry`` (so the tool row resolves)
        plus one nested ``AssistantMessage`` parented to the spawning Agent call
        (so the evalboard renders it as an expandable child), carrying that
        generation's real tokens. ``finalize`` folds those into the turn total.

        Best-effort: any failure is swallowed, so a recovery hiccup never fails the
        turn.

        Rationale: .claude/notes/agents.md § Codex rollout rebuild
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

        The recorder flushes on a background task, so the file can lag the parent
        ``turn/completed`` by a beat. A missing ``<home>/sessions`` bails
        immediately rather than polling for a flush that can never land.
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

        A ``token_count`` event marks each generation boundary. Tool CALLS are
        paired with their OUTPUT by ``call_id``, since the output can be emitted a
        generation later. Trailing items with no closing ``token_count`` flush as a
        final token-less generation; corrupt lines are skipped.
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

        # Pass 1: tool OUTPUTs by call_id.
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

        Shell-style calls are normalized to ``{"command": ...}`` so the transcript
        renders the command line; everything else passes through.
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

        Each tool call gets a ``tool_use`` block whose id matches a
        ``CommandTelemetry``, so the evalboard tool row resolves. Inner ids are
        THREAD-PREFIXED to stay unique across the parent's own tools.
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
        come from the child's per-generation ``token_count``.
        """
        raw_input, cached, output, reasoning = gen["tokens"] or (0, 0, 0, 0)
        fresh = _fresh_input_tokens(raw_input, cached)
        now = datetime.now()
        return AssistantMessage(
            started_at=now,
            completed_at=now,
            generation_duration_ms=None,
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

        Used only when the child's rollout cannot be found. ``model`` is the
        SPAWNED sub-agent's model, not the parent's, matching the other path."""
        now = datetime.now()
        return AssistantMessage(
            started_at=now,
            completed_at=now,
            generation_duration_ms=None,
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

    def _extract_command_telemetry(
        self,
        command_item: Any,
        sequence: int,
        started_ms: int | None = None,
        completed_ms: int | None = None,
    ) -> CommandTelemetry | None:
        """Extract CommandTelemetry from a CommandExecutionThreadItem.

        Rationale: .claude/notes/agents.md § Tool-name and argument normalization
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

            # Store the output WHOLE: result_summary is the untruncated tool-result
            # body and its length drives result_tokens, so trimming here
            # under-reports tool-output size for every command (CE043). Display
            # trimming belongs in the renderers, not capture.
            summary_parts = [f"Exit code: {exit_code}" if exit_code is not None else "Command executed"]
            if output and len(output.strip()) > 0:
                summary_parts.append(f"Output: {output}")
            result_summary = " | ".join(summary_parts)

            # Try to parse output as JSON
            result_data = None
            if output:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    result_data = json.loads(output)

            # Build parameters from command string
            parameters = {"command": command}

            timing = _item_timing(started_ms, completed_ms, duration_ms)
            return CommandTelemetry(
                tool_name="Bash",
                tool_id=command_id,
                timestamp=timing.timestamp,
                execution_started_at=timing.execution_started_at,
                execution_completed_at=timing.execution_completed_at,
                duration_ms=timing.duration_ms,
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
        self,
        change_id: str,
        changes: Any,
        status: Any,
        sequence: int,
        started_ms: int | None = None,
        completed_ms: int | None = None,
    ) -> CommandTelemetry | None:
        """Build CommandTelemetry for a Codex fileChange item.

        Recorded as a ``Write`` so cross-agent criteria see the same signal they
        get from Claude's Write/Edit calls. A failed or declined apply_patch is an
        ``error``, never a successful write.
        """
        try:
            paths = [str(c.path) for c in changes if hasattr(c, "path")] if changes else []
            status_str = _status_value(status)
            failed = status_str in _FILE_CHANGE_FAILURE_STATUSES
            timing = _item_timing(started_ms, completed_ms, None)
            return CommandTelemetry(
                tool_name="Write",
                tool_id=change_id,
                timestamp=timing.timestamp,
                execution_started_at=timing.execution_started_at,
                execution_completed_at=timing.execution_completed_at,
                duration_ms=timing.duration_ms,
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
        """This turn's own slice of the Codex SDK's thread-cumulative total.

        Single conversion site for both the TurnEndEvent and the AgentEndEvent, so
        cached-input tokens cannot be captured in one path and dropped in the
        other. The SDK surfaces no cost, so it is rate-carded.

        ``ThreadTokenUsage.total`` counts the whole THREAD, and the thread is
        reused for every turn — so the baseline captured at the end of the previous
        turn is subtracted to leave just this one.

        Cache-bucket convention (Codex/OpenAI): ``input_tokens`` is the FULL prompt
        count, INCLUSIVE of the cached prefix, and there is no separate
        cache-write fee. So ``uncached = input - cached``, ``cache_creation = 0``,
        ``cache_read = cached``.

        Rationale: .claude/notes/agents.md § Codex rollout rebuild
        """
        if not sdk_token_usage:
            return None
        total = getattr(sdk_token_usage, "total", None)
        if not total:
            return None
        cumulative = _ThreadTotals(
            input=getattr(total, "input_tokens", 0) or 0,
            output=getattr(total, "output_tokens", 0) or 0,
            cached=getattr(total, "cached_input_tokens", 0) or 0,
        )
        turn = cumulative.since(self._thread_usage_baseline)
        self._thread_usage_baseline = cumulative
        # Fresh slice = full prompt minus the cached prefix.
        uncached = _fresh_input_tokens(turn.input, turn.cached)
        cost = calculate_cost(
            self._effective_model() or "",
            uncached_input_tokens=uncached,
            output_tokens=turn.output,
            cache_read_tokens=turn.cached,
        )
        return TokenUsage(
            uncached_input_tokens=uncached,
            output_tokens=turn.output,
            cache_read_input_tokens=turn.cached,
            total_cost_usd=cost,
        )

    def _advance_usage_baseline(self, usage: TokenUsage | None) -> None:
        """Move the thread baseline past a turn whose SDK total never arrived.

        The crash fallback reads per-generation tokens off the flushed messages,
        so the crashed turn itself is right — but the thread's cumulative total
        kept climbing, and without advancing past it the NEXT turn's delta re-books
        everything this one already reported.
        """
        if usage is None:
            return
        base = self._thread_usage_baseline
        # SDK ``input_tokens`` is the full prompt, so the input baseline advances
        # by uncached + cache_read.
        self._thread_usage_baseline = _ThreadTotals(
            input=base.input + usage.uncached_input_tokens + usage.cache_read_input_tokens,
            output=base.output + usage.output_tokens,
            cached=base.cached + usage.cache_read_input_tokens,
        )

    def _fold_subagent_tokens(self, parent: TokenUsage | None, messages: list[TranscriptMessage]) -> TokenUsage | None:
        """Add recovered sub-agent (child-thread) tokens to the parent turn total.

        Codex bills children on separate threads, so the parent's streamed total
        omits them. Summing the recovered ``parent_tool_use_id``-tagged messages
        here, priced PER CHILD MODEL (sub-agents may run a different one), makes
        the turn total all-inclusive — the same end state Claude reaches naturally.
        A no-op when nothing was recovered.
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

        # Each child generation on its own model, then sum.
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

        Crash/timeout fallback: when the stream raises before returning the SDK
        ``total``, the per-generation tokens were already recorded on the flushed
        messages, so summing them recovers what the crashed turn actually spent.
        None when nothing was captured, matching the SDK path's empty contract.
        """
        # PARENT-thread messages ONLY: sub-agent tokens are added by
        # _fold_subagent_tokens, and summing them here would double-count.
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
