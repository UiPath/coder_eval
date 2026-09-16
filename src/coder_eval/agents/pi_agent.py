"""Pi agent implementation (the ``pi`` Node coding agent — https://pi.dev/).

Drives the ``pi`` CLI in JSON print mode, which streams newline-delimited JSON
events on stdout, and reduces that stream into the standardized coder_eval event
protocol so :class:`EventCollector` builds the ``TurnRecord``. The design mirrors
:mod:`coder_eval.agents.opencode_agent`.

Three grammar facts that are not obvious from the event names (``pi`` 0.84.4):

- ``agent_start`` can appear MORE THAN ONCE per invocation — Pi auto-retries a
  transient provider error internally — and ``agent_end`` is therefore NOT
  terminal. ``agent_settled`` (or EOF) is; the single ``AgentEndEvent`` is
  emitted there.
- ``turn_start`` is one per agent-loop step, and is the unit ``max_turns`` counts.
- ``message_end`` is ignored for token accounting: ``turn_end`` echoes the same
  assistant usage once per step, so reading both would double-count.

A per-agent ``--session-dir`` + stable ``--session-id``, replayed on every
``communicate()``, are what make dialog mode work across CLI invocations.

Rationale: .claude/notes/agents.md § Pi
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, NoReturn
from uuid import uuid4

from coder_eval.agent import Agent
from coder_eval.agents._skills import _plugin_skill_dirs  # shared plugin->skills resolver
from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.isolation.docker_runner import STDOUT_LINE_LIMIT_BYTES
from coder_eval.models import (
    AgentKind,
    AgentState,
    ApiRoute,
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    Enforcement,
    HarnessContract,
    PiAgentConfig,
    ResultSummary,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
)
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.callbacks import StreamCallback, safe_emit
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.timing import TurnClock, close_window

from .registry import AgentRegistry


logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when tearing down the CLI subprocess.
# Doubles as the post-EOF exit grace in _settle_turn when no turn deadline is set.
# Re-declared at OpenCode's value rather than shared — see the notes.
# Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
_TERM_GRACE_SECONDS = 5.0

# SIGKILL does not exist on Windows (where the process-group sweep is a no-op
# anyway); resolve it dynamically so the module imports and typechecks on every
# platform, falling back to SIGTERM for the direct-pid kill_sync path.
_SIGKILL: signal.Signals = getattr(signal, "SIGKILL", signal.SIGTERM)

# How long to keep draining stdout/stderr after the CLI has been reaped: a
# print-mode CLI may leave an inherited pipe open, so every post-exit read is
# bounded.
_DRAIN_SECONDS = 2.0

# How many distinct unrecognized event-type strings to retain for the crash
# message when the vocabulary check fails (diagnosis, not an exhaustive list).
_MAX_UNRECOGNIZED_TYPES = 8

# pi's native tool names -> the canonical (Claude) vocabulary every criterion is
# written against. Unknown tools pass through unchanged.
# Rationale: .claude/notes/agents.md § Tool-name and argument normalization
_TOOL_NAME_MAP: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "multiedit": "Edit",
    # Pi's search tool is `find` (glob-by-pattern); there is no `glob` in its set.
    "find": "Glob",
    "grep": "Grep",
    "list": "LS",
    "ls": "LS",
    "webfetch": "WebFetch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "task": "Agent",
}

# pi per-tool INPUT-arg key -> canonical (Claude) key, keyed by the canonical
# tool name (post _TOOL_NAME_MAP). The search tools keep `path`, which is already
# Claude's key. Unlisted keys pass through.
_PI_ARG_RENAME: dict[str, dict[str, str]] = {
    "Read": {"path": "file_path"},
    "Write": {"path": "file_path"},
    "Edit": {
        "path": "file_path",
        "oldString": "old_string",
        "newString": "new_string",
        "replaceAll": "replace_all",
    },
}

# Config fields Pi does NOT enforce. `experiments/default.yaml` sets
# `permission_mode` and `allowed_tools` on every task, so start() warns once
# rather than letting a task believe it constrained the agent. `system_prompt`
# and `plugins` ARE supported, so neither is here. Per-harness table:
# docs/agents/HARNESS_PARITY.md.
_UNSUPPORTED_CONFIG_FIELDS: tuple[str, ...] = (
    "permission_mode",
    "system_prompt_file",
    # Forwarding these to --tools would allowlist nonexistent tools and strip the
    # agent of ALL tools: Pi's built-ins are lowercase.
    # Rationale: .claude/notes/agents.md § Harness run-limit parity
    "allowed_tools",
    "disallowed_tools",
)

# The full recognized Pi vocabulary (from `pi` 0.84.4). A clean exit that
# recognized NOTHING from this set is vocabulary drift and is crashed, not scored.
# Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
_RECOGNIZED_EVENTS = frozenset(
    {
        "session",
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "turn_end",
        "agent_end",
        "agent_settled",
    }
)

# ToolEndStatus -> CommandTelemetry.result_status (the persisted tri-state).
_RESULT_STATUS: dict[ToolEndStatus, Literal["success", "error", "unknown"]] = {
    ToolEndStatus.OK: "success",
    ToolEndStatus.ERROR: "error",
    ToolEndStatus.PERMISSION_DENIED: "error",
    ToolEndStatus.UNRESOLVED: "unknown",
}


def _canonical_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Rename a tool call's argument keys to the canonical cross-agent vocabulary.

    Order is preserved and unlisted keys pass through untouched.
    """
    rename = _PI_ARG_RENAME.get(tool_name)
    if not rename:
        return params
    return {rename.get(key, key): value for key, value in params.items()}


def _result_text(result: Any) -> str | None:
    """Best-effort flatten of a Pi ``tool_execution_end.result`` to text.

    Pi results are ``{"content": [{"type": "text", "text": "..."}, ...]}`` (spike
    verified). Fall back to the string form for any other shape so a summary is
    never silently dropped.
    """
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
            joined = "".join(parts)
            if joined:
                return joined
    if result is None:
        return None
    return str(result)


class _PiTurnState:
    """Per-``communicate()`` accumulator: events in, finalization payload out.

    Owns everything the terminal ``AgentEndEvent`` must carry (transcript
    messages, summed usage, text output) plus the open-tool bookkeeping needed
    to force-close orphans when a turn dies mid-flight.
    """

    def __init__(
        self,
        *,
        task_id: str,
        iteration: int,
        user_input: str,
        model: str | None,
        clock: TurnClock | None = None,
    ) -> None:
        self.task_id = task_id
        self.iteration = iteration
        self.user_input = user_input
        self.model = model

        # ONE clock per turn, so the tool spans and the window bounds they are
        # subtracted from share a basis. Injectable so a test supplies a fake
        # rather than monkeypatching this module's `datetime` global, which a
        # derived stamp would silently escape.
        self.clock = clock or TurnClock()
        self.started_at = time.monotonic()
        self.thread_id: str | None = None

        # Cumulative turn totals (summed across every inner step).
        self.usage = TokenUsage()
        self.cost_usd: float = 0.0
        self.saw_cost = False

        self.messages: list[TranscriptMessage] = []
        self.text_parts: list[str] = []

        # Pi `turn_start` events counted; this is what max_turns caps.
        self.turn_count = 0
        self.turn_id: str = ""
        # True between a step's `turn_start` and its `turn_end`. `finalize` needs
        # it to close a TurnStartEvent the stream never got to close.
        self.turn_open = False
        self.turn_started_at: datetime | None = None
        self.turn_text_parts: list[str] = []
        self.turn_tool_ids: list[str] = []
        # Where the NEXT generation window starts: the previous turn's end.
        # None until the first turn finishes, and deliberately so — everything
        # before the first `turn_start` is CLI process spawn, not model time.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.gen_mark: datetime | None = None

        # toolCallId -> telemetry for tools awaiting a result.
        self.open_tools: dict[str, CommandTelemetry] = {}
        self.sequence = 0
        self.stop_reason: str | None = None
        self.error_message: str | None = None
        self.max_turns_exhausted = False
        # Guards the one-terminal-event rule; see finalize().
        self.finalized = False
        # Count of events matched against the recognized Pi vocabulary (drift check),
        # plus a bounded sample of the types that did NOT match — so the drift crash
        # message can name what it actually saw.
        self.recognized_events = 0
        self.unrecognized_types: set[str] = set()
        # Warn-once guard for token-accounting drift: the event-vocabulary check
        # cannot see inside `usage`.
        # Rationale: .claude/notes/agents.md § Why token-shape drift warns instead of raising
        self.warned_token_shape = False

        self._emit: Callable[[StreamEvent], None] = lambda _e: None

    def bind(self, emit: Callable[[StreamEvent], None]) -> None:
        self._emit = emit

    def emit(self, event: StreamEvent) -> None:
        self._emit(event)

    @property
    def agent_output(self) -> str:
        return "".join(self.text_parts)

    # --- event handlers ----------------------------------------------------

    def on_turn_start(self) -> None:
        # A prior step's `turn_start` with no `turn_end` — a generation aborted
        # mid-turn (the willRetry case). Close its dangling TurnStartEvent, or the
        # stream carries N starts and N-1 ends and breaks the one-pair-per-inner-turn
        # contract. `finalize` closes only the LAST open turn, so it cannot cover this.
        if self.turn_open:
            self.turn_open = False
            self.emit(
                TurnEndEvent(
                    task_id=self.task_id,
                    thread_id=self.thread_id,
                    turn_id=self.turn_id,
                    status=TurnEndStatus.CRASHED,
                    tokens=None,
                )
            )
        self.turn_count += 1
        self.turn_open = True
        self.turn_id = f"turn_{self.turn_count}"
        self.turn_started_at = self.clock.now()
        self.turn_text_parts = []
        self.turn_tool_ids = []
        # No per-turn span list to reset here any more: the collector subtracts
        # from final bounds with every span known.
        self.emit(
            TurnStartEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                model=self.model,
            )
        )

    def on_message_update(self, obj: dict[str, Any]) -> None:
        """Stream a ``text_delta`` as a ``TextChunkEvent`` (thinking/toolcall ignored)."""
        event = obj.get("assistantMessageEvent")
        if not isinstance(event, dict) or event.get("type") != "text_delta":
            return
        delta = event.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        self.text_parts.append(delta)
        self.turn_text_parts.append(delta)
        self.emit(TextChunkEvent(task_id=self.task_id, thread_id=self.thread_id, turn_id=self.turn_id, text=delta))

    def on_tool_execution_start(self, obj: dict[str, Any]) -> None:
        call_id = str(obj.get("toolCallId") or f"call_{self.sequence + 1}")
        if call_id in self.open_tools:
            return
        self.sequence += 1
        raw_tool = str(obj.get("toolName") or "unknown")
        tool_name = _TOOL_NAME_MAP.get(raw_tool.lower(), raw_tool)
        args = obj.get("args")
        params = args if isinstance(args, dict) else {}
        started = self.clock.now()
        telemetry = CommandTelemetry(
            tool_name=tool_name,
            tool_id=call_id,
            assistant_turn_index=self.turn_count,
            timestamp=started,
            execution_started_at=started,
            parameters=_canonical_params(tool_name, params),
            sequence_number=self.sequence,
        )
        self.open_tools[call_id] = telemetry
        self.turn_tool_ids.append(call_id)
        self.emit(ToolStartEvent(task_id=self.task_id, thread_id=self.thread_id, turn_id=self.turn_id, tool=telemetry))

    def on_tool_execution_end(self, obj: dict[str, Any]) -> None:
        call_id = str(obj.get("toolCallId") or "")
        summary = _result_text(obj.get("result"))
        is_error = bool(obj.get("isError"))
        if is_error:
            message = summary or "tool failed"
            # Best-effort: Pi does not tag permission denials, so infer from the
            # text. The persisted tri-state folds both to "error", so a
            # misclassification is cosmetic.
            denied = "permission" in message.lower() or "denied" in message.lower()
            status = ToolEndStatus.PERMISSION_DENIED if denied else ToolEndStatus.ERROR
        else:
            message = None
            status = ToolEndStatus.OK
        self._close_tool(call_id, status=status, summary=summary, error=message)

    def _close_tool(
        self,
        call_id: str,
        *,
        status: ToolEndStatus,
        summary: str | None,
        error: str | None,
    ) -> None:
        telemetry = self.open_tools.pop(call_id, None)
        if telemetry is None:
            # A result with no matching call (shouldn't happen, but never drop it).
            self.sequence += 1
            telemetry = CommandTelemetry(
                tool_name="unknown",
                tool_id=call_id,
                assistant_turn_index=self.turn_count,
                timestamp=self.clock.now(),
                sequence_number=self.sequence,
            )
        # Only a RESOLVED tool is timed: an orphan was never observed finishing,
        # so stamping it would manufacture a span the central subtraction then
        # takes out of a window it never occupied. `execution_started_at` IS
        # kept — one bound alone forms no span (CE058).
        # Rationale: .claude/notes/agents.md § Why only a RESOLVED tool is timed
        if status is not ToolEndStatus.UNRESOLVED:
            completed = self.clock.now()
            telemetry.execution_completed_at = completed
            if telemetry.execution_started_at is not None:
                telemetry.duration_ms = (completed - telemetry.execution_started_at).total_seconds() * 1000
        telemetry.result_status = _RESULT_STATUS[status]
        # Stored untruncated by design (sub-agent returns must survive whole).
        telemetry.result_summary = summary
        telemetry.error_message = error
        self.emit(
            ToolEndEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                tool=telemetry,
                status=status,
            )
        )

    def _warn_token_shape(self, message: str, *args: Any) -> None:
        """Log a token-accounting anomaly at most once per turn (not once per bucket/step)."""
        if self.warned_token_shape:
            return
        self.warned_token_shape = True
        logger.warning("pi: unexpected token accounting — " + message, *args)

    def _as_int(self, value: Any) -> int:
        """Coerce one stream-supplied token count; count a non-number as 0.

        A bool is never a token count (``int(True) == 1``).

        ``None`` is a legitimately-absent bucket (silent). Any OTHER unparseable
        value is schema drift and warns once.

        Rationale: .claude/notes/agents.md § Why token-shape drift warns instead of raising
        """
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            self._warn_token_shape("token count had unexpected type %s (%r); counted as 0", type(value).__name__, value)
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            self._warn_token_shape("token count %r was not parseable as an int; counted as 0", value)
            return 0

    def on_turn_end(self, obj: dict[str, Any]) -> None:
        """Accumulate this step's usage and append its assistant message.

        Usage is read from ``turn_end`` ONCE per step (not from every
        ``message_end``, which echoes the same numbers) so the turn total is the
        sum of the per-generation slices.
        """
        self.turn_open = False
        message = obj.get("message")
        message = message if isinstance(message, dict) else {}
        raw_usage = message.get("usage")
        if not isinstance(raw_usage, dict) or not raw_usage:
            # A completed step that booked no usage object at all: its tokens and
            # cost silently resolve to 0, so say so once.
            self._warn_token_shape("turn_end carried no usage object; this step's tokens/cost counted as 0")
        usage = raw_usage if isinstance(raw_usage, dict) else {}

        step_in = self._as_int(usage.get("input"))
        raw_out = self._as_int(usage.get("output"))
        step_reasoning = self._as_int(usage.get("reasoning"))
        step_cw = self._as_int(usage.get("cacheWrite"))
        step_cr = self._as_int(usage.get("cacheRead"))
        # Reasoning bills at the output rate but is reported apart from `output`;
        # fold it into the turn total (the per-message record keeps it separately).
        step_out = raw_out + step_reasoning

        # A usage object whose every bucket resolves to 0 is the drift shape the
        # whole-object check cannot see. Warn once; score, don't crash.
        if raw_usage and step_in == raw_out == step_reasoning == step_cw == step_cr == 0:
            self._warn_token_shape("turn_end usage object had all-zero token buckets; this step booked 0 tokens/cost")

        self.usage = TokenUsage(
            uncached_input_tokens=self.usage.uncached_input_tokens + step_in,
            output_tokens=self.usage.output_tokens + step_out,
            cache_creation_input_tokens=self.usage.cache_creation_input_tokens + step_cw,
            cache_read_input_tokens=self.usage.cache_read_input_tokens + step_cr,
        )
        # Cross-check the stream's OWN `totalTokens` against the summed buckets.
        # Pi's invariant is totalTokens == input + output + cacheRead + cacheWrite
        # — reasoning bills at the output rate but is EXCLUDED from this field, so
        # compare against raw_out, not step_out. Only when the field is present.
        reported_total = usage.get("totalTokens")
        # int OR float: a `123.0`-shaped total is itself a plausible drift, and
        # the compare below is exact for whole values.
        if isinstance(reported_total, int | float) and not isinstance(reported_total, bool):
            expected_total = step_in + raw_out + step_cw + step_cr
            if reported_total != expected_total:
                self._warn_token_shape(
                    "turn_end totalTokens=%d does not reconcile with input+output+cacheRead+cacheWrite=%d — "
                    + "a bucket may have been renamed or its meaning moved; re-check docs/agents/PI.md before "
                    + "trusting cost",
                    reported_total,
                    expected_total,
                )
        cost = usage.get("cost")
        if isinstance(cost, dict):
            total = cost.get("total")
            if isinstance(total, int | float) and not isinstance(total, bool):
                self.cost_usd += float(total)
                self.saw_cost = True

        finish = message.get("stopReason")
        if isinstance(finish, str) and finish:
            self.stop_reason = finish
        # Capture a terminal provider error so a `pi -p` that exits 0 after
        # exhausting retries still surfaces WHY (finalize reads error_message into
        # result_summary.result). Reset on a non-error turn so an intermediate
        # retry error that a later cycle recovered from never leaks into the result.
        if finish == "error":
            err = message.get("errorMessage")
            self.error_message = err if isinstance(err, str) and err else "pi reported stopReason=error"
        else:
            self.error_message = None

        completed = self.clock.now()
        blocks: list[ContentBlock] = []
        turn_text = "".join(self.turn_text_parts)
        if turn_text:
            blocks.append(ContentBlock(block_type="text", sequence=0, text=turn_text))
        for i, tool_id in enumerate(self.turn_tool_ids, start=len(blocks)):
            blocks.append(ContentBlock(block_type="tool_use", sequence=i, tool_use_id=tool_id))

        # Tile from the previous turn's end. The RAW window only.
        turn_start = self.turn_started_at if self.turn_started_at is not None else completed
        started, generation_ms = close_window(
            mark=self.gen_mark if self.gen_mark is not None else turn_start,
            now=completed,
            item_start=turn_start,
        )
        self.messages.append(
            AssistantMessage(
                started_at=started,
                completed_at=completed,
                generation_duration_ms=generation_ms,
                content_blocks=blocks,
                tool_use_ids=list(self.turn_tool_ids),
                input_tokens=step_in,
                output_tokens=step_out,
                cache_creation_tokens=step_cw,
                cache_read_tokens=step_cr,
                reasoning_tokens=step_reasoning,
                stop_reason=finish if isinstance(finish, str) else None,
                model=self.model,
                message_id=str(message.get("responseId") or "") or None,
            )
        )
        # A message was appended, so the next window starts where this one ended.
        # Only a FINISHED turn advances the mark.
        self.gen_mark = completed
        # SPENT state, reset HERE and not only in `on_turn_start`: a second
        # `turn_end` with no intervening start — a duplicate or replayed line,
        # which this reducer promises to survive — would otherwise republish this
        # turn's span, text and tool ids as the next turn's.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.turn_started_at = None
        self.turn_text_parts = []
        self.turn_tool_ids = []
        self.emit(
            TurnEndEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                status=TurnEndStatus.COMPLETED,
                tokens=TokenUsage(
                    uncached_input_tokens=step_in,
                    output_tokens=step_out,
                    cache_creation_input_tokens=step_cw,
                    cache_read_input_tokens=step_cr,
                ),
            )
        )

    def _rate_card_cost(self) -> float | None:
        if not self.model or self.usage.is_empty():
            return None
        return calculate_cost(
            self.model,
            uncached_input_tokens=self.usage.uncached_input_tokens,
            output_tokens=self.usage.output_tokens,
            cache_creation_tokens=self.usage.cache_creation_input_tokens,
            cache_read_tokens=self.usage.cache_read_input_tokens,
        )

    def _resolve_cost(self) -> float | None:
        """Decide the turn's cost: the stream's own accounting vs the rate card.

        Pi reports a real per-call ``cost.total``, which wins for any nonzero
        total. It falls back to the rate card when the stream reported no cost at
        all, or reported exactly ``$0`` on a model the rate card DOES price.

        Rationale: .claude/notes/agents.md § Cost: the stream versus the rate card
        """
        rate = self._rate_card_cost()
        if not self.saw_cost:
            return rate
        if self.cost_usd == 0.0 and rate:
            logger.debug(
                "pi: the stream reported $0 for a turn the rate card prices at $%.6f; using the rate card "
                + "so the run total is not understated.",
                rate,
            )
            return rate
        return self.cost_usd

    def close_open_tools(self) -> None:
        """Force-close every tool still awaiting a result (crash/timeout orphans)."""
        for call_id in list(self.open_tools):
            self._close_tool(call_id, status=ToolEndStatus.UNRESOLVED, summary=None, error="no result observed")

    def finalize(
        self,
        status: AgentEndStatus,
        *,
        crashed: bool = False,
        crash_reason: str | None = None,
    ) -> None:
        """Close orphaned tools and emit the terminal ``AgentEndEvent`` (idempotent)."""
        if self.finalized:
            return
        self.finalized = True
        self.close_open_tools()
        usage = self.usage
        cost = self._resolve_cost()
        if cost is not None:
            usage = usage.model_copy(update={"total_cost_usd": cost})
        # A turn still open never received its `turn_end`; close it or the
        # one-pair-per-inner-turn contract breaks.
        if self.turn_open:
            self.turn_open = False
            self.emit(
                TurnEndEvent(
                    task_id=self.task_id,
                    thread_id=self.thread_id,
                    turn_id=self.turn_id,
                    status=TurnEndStatus(status.value),
                    tokens=None,
                )
            )
        self.emit(
            AgentEndEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                status=status,
                usage=usage,
                iteration=self.iteration,
                user_input=self.user_input,
                agent_output=self.agent_output,
                model_used=self.model,
                assistant_turn_count=self.turn_count,
                messages=list(self.messages),
                num_turns=self.turn_count,
                max_turns_exhausted=self.max_turns_exhausted,
                result_summary=ResultSummary(
                    is_error=crashed,
                    subtype=status.value,
                    stop_reason=self.stop_reason,
                    result=crash_reason or self.error_message,
                ),
                crashed=crashed,
                crash_reason=crash_reason,
                duration_seconds=time.monotonic() - self.started_at,
                # One basis with the window bounds — see the AgentStartEvent
                # site in `communicate`.
                timestamp=self.clock.now(),
            )
        )


@AgentRegistry.register(AgentKind.PI, PiAgentConfig)
class PiAgent(Agent[PiAgentConfig]):
    """Runs the ``pi`` CLI as a subprocess, one invocation per turn."""

    # `should_stop` is polled at every event boundary (tool-call granularity);
    # `--append-system-prompt` appends to, never replaces, the CLI's own prompt.
    # Rationale: .claude/notes/agents.md § The system_prompt_semantics marker
    contract = HarnessContract(
        system_prompt=Enforcement.ENFORCED,
        system_prompt_semantics="append",
        plugin_skills=Enforcement.ENFORCED,
        permission_mode=Enforcement.UNSUPPORTED,
        allowed_tools=Enforcement.UNSUPPORTED,
        disallowed_tools=Enforcement.UNSUPPORTED,
        cooperative_stop=True,
    )

    def __init__(
        self,
        config: PiAgentConfig,
        route: ApiRoute | None = None,
        *,
        task_id: str = "unknown",
    ) -> None:
        """Every parameter the agent factory can pass is DECLARED, not absorbed.

        ``route`` is accepted for factory parity and deliberately unused: the CLI
        owns its own provider configuration. ``task_id`` only labels the event
        stream.

        Rationale: .claude/notes/agents.md § Why the constructors declare every kwarg
        """
        self.config = config
        self.route = route
        self.task_id = task_id
        self.working_directory: str | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        # Skills-parent dirs resolved from `agent.plugins`, passed to `pi --skill`
        # (Pi discovers `<name>/SKILL.md` recursively). Assigned in start().
        self._skill_dirs: list[str] = []
        # Per-agent session, reused across communicate() calls for multi-turn
        # continuity. Removed in stop(), deliberately NOT in kill().
        # Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
        self._session_id: str | None = None
        self._session_dir: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        # Process-group ids of every invocation this agent spawned, swept on
        # kill()/kill_sync()/stop().
        self._spawned_pgids: list[int] = []
        self._state = AgentState.WORKING

    # --- lifecycle ---------------------------------------------------------

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        if shutil.which("pi") is None:
            raise RuntimeError(
                "The 'pi' CLI was not found on PATH."
                + " Install it with `npm install -g @earendil-works/pi-coding-agent` (see https://pi.dev/)."
            )
        ignored = [f for f in _UNSUPPORTED_CONFIG_FIELDS if getattr(self.config, f, None)]
        if ignored:
            logger.warning(
                "pi: %s set but NOT enforced — the CLI has no equivalent knob in JSON print mode, so the run is "
                + "unconstrained by them; do not rely on them as a boundary (see docs/agents/PI.md).",
                ", ".join(ignored),
            )
        # Resolve `agent.plugins` -> skills dirs and load them via `pi --skill`.
        # Loudly logs when plugins were declared but nothing resolved (the run
        # would otherwise measure the model WITHOUT the skill under test).
        self._skill_dirs = _plugin_skill_dirs(self.config.plugins, log=logger, harness="pi")
        if self._skill_dirs:
            logger.info("pi: loading %d skill dir(s) via --skill: %s", len(self._skill_dirs), self._skill_dirs)
        elif self.config.plugins:
            logger.warning(
                "pi: %d plugin(s) declared but 0 skill dir(s) resolved — the agent will run WITHOUT them "
                + "(see docs/agents/PI.md).",
                len(self.config.plugins),
            )
        self.working_directory = working_directory
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        # A stable pre-assigned id (create-if-missing on turn 1, resume after).
        # The tempdir lives OUTSIDE the sandbox and staged reference dir, so it
        # never pollutes graded files. Drop a prior start()'s dir so re-starting
        # the same instance cannot leak one.
        self._cleanup_session_dir()
        # Sanitize task_id before it reaches pi's `--session-id`: a dataset row's
        # path-shaped id would resolve to a non-existent subdir under
        # `--session-dir` and fail the row before any work.
        safe_task_id = re.sub(r"[^A-Za-z0-9._-]", "_", self.task_id)
        self._session_id = f"coder-eval-{safe_task_id}-{uuid4().hex[:8]}"
        self._session_dir = tempfile.mkdtemp(prefix="pi-session-")
        self._state = AgentState.WORKING

    async def stop(self) -> None:
        await self.kill()
        self._cleanup_session_dir()
        self._mark_stopped()

    def _cleanup_session_dir(self) -> None:
        if self._session_dir is not None:
            shutil.rmtree(self._session_dir, ignore_errors=True)
            self._session_dir = None

    async def kill(self) -> None:
        proc = self._process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        self._sweep_process_groups()

    def kill_sync(self) -> None:
        """SIGKILL the in-flight CLI and its process group (watchdog thread; must not await)."""
        proc = self._process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(proc.pid, _SIGKILL)
        self._sweep_process_groups()

    def _sweep_process_groups(self) -> None:
        """SIGKILL every process group this agent spawned (POSIX only).

        Each invocation runs in its own session, so its pgid is the CLI's pid and
        the group holds ONLY what that invocation spawned.
        """
        if os.name != "posix":
            return
        for pgid in self._spawned_pgids:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, _SIGKILL)
        self._spawned_pgids.clear()

    def get_environment_info(self) -> dict[str, Any]:
        # Spread the base first so the `system_prompt_semantics` run marker is
        # always present (CE046).
        info: dict[str, Any] = {
            **super().get_environment_info(),
            "pi_model": self.config.model,
            "pi_thinking_level": self.config.thinking_level,
        }
        if self._session_id:
            info["pi_session_id"] = self._session_id
        if self._skill_dirs:
            # Recorded per task so a run's report can confirm the skills under test
            # actually reached the agent.
            info["pi_skill_paths"] = list(self._skill_dirs)
        return info

    # --- command construction ---------------------------------------------

    def _build_argv(self, user_input: str) -> list[str]:
        # -p exits after the run; --no-context-files + --no-approve isolate the
        # sandbox from host AGENTS.md/CLAUDE.md and project-local trust.
        # --session-dir + --session-id give cross-communicate() continuity — NOT
        # --no-session, which would defeat it. No --dir: the working dir is `cwd`.
        assert self._session_dir is not None and self._session_id is not None
        argv = [
            "pi",
            "-p",
            "--mode",
            "json",
            "--no-context-files",
            "--no-approve",
            "--session-dir",
            self._session_dir,
            "--session-id",
            self._session_id,
        ]
        if self.config.model:
            argv += ["--model", self.config.model]  # provider-prefixed form
        if self.config.thinking_level:
            argv += ["--thinking", self.config.thinking_level]
        for skill_dir in self._skill_dirs:
            # Additive skill load (from agent.plugins): Pi lists each skill's
            # name+description in the system prompt and reads SKILL.md on demand.
            argv += ["--skill", skill_dir]
        # allowed_tools / disallowed_tools are NOT forwarded — see
        # _UNSUPPORTED_CONFIG_FIELDS. Pi runs with its full native toolset.
        if self.config.system_prompt:
            argv += ["--append-system-prompt", self.config.system_prompt]
        # user_input is a distinct argv element after `--` (never shell-interpolated).
        argv += ["--", user_input]
        return argv

    def _build_env(self) -> dict[str, str]:
        """The CLI's full environment: the host's, plus the sandbox's contributions.

        The PATH prepend is the mock-shadowing contract (``Agent.start``): the
        sandbox's mock CLI directories must resolve BEFORE the real binaries.
        ``PLUGIN_TOOLS_DIR`` is advisory and never overrides an inherited value.
        Returns the WHOLE environment so the CLI keeps the host's provider
        credentials.
        """
        env = dict(os.environ)
        if self._env_path_prepend:
            env["PATH"] = os.pathsep.join([*self._env_path_prepend, env.get("PATH", "")])
        if self._plugin_tools_dir and "PLUGIN_TOOLS_DIR" not in env:
            env["PLUGIN_TOOLS_DIR"] = self._plugin_tools_dir
        return env

    # --- the turn ----------------------------------------------------------

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        if self.working_directory is None:
            raise RuntimeError("PiAgent.start() must be called before communicate()")

        self._begin_turn()
        collector = EventCollector()

        def emit(event: StreamEvent) -> None:
            collector.on_event(event)
            if stream_callback is not None:
                safe_emit(stream_callback, event)

        state = _PiTurnState(
            task_id=self.task_id,
            iteration=self._iteration,
            user_input=user_input,
            model=self.config.model,
        )
        state.bind(emit)

        emit(
            AgentStartEvent(
                task_id=self.task_id,
                prompt=user_input,
                iteration=self._iteration,
                model=self.config.model,
                # One basis with the window bounds this is subtracted against;
                # the model's raw `datetime.now()` default put two clocks inside
                # one subtraction (CE058).
                timestamp=state.clock.now(),
            )
        )

        # Deadlines stay on `time.monotonic()`, deliberately NOT the turn clock:
        # a deadline must not move when the wall clock steps.
        deadline = None if timeout is None else time.monotonic() + timeout
        stopped_early = False
        stderr_drain: asyncio.Future[bytes] | None = None
        # Bound OUTSIDE the try so `finally` can tell "never spawned" from
        # "spawned and possibly still running".
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_argv(user_input),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=self._build_env(),
                # One nd-JSON event can carry a whole tool result, past
                # StreamReader's default 64 KiB cap.
                limit=STDOUT_LINE_LIMIT_BYTES,
                # Own session/process group, so teardown can killpg a lingering
                # child without touching anything this invocation didn't spawn.
                start_new_session=os.name == "posix",
            )
            self._process = proc
            if os.name == "posix":
                self._spawned_pgids.append(proc.pid)
            assert proc.stdout is not None

            # Drain stderr CONCURRENTLY, or a child that fills the pipe blocks on
            # write and hangs the turn to its deadline.
            # Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
            if proc.stderr is not None:
                stderr_drain = asyncio.ensure_future(proc.stderr.read())

            # An inherited pipe may never reach EOF, so race each read against
            # process exit; a bounded drain then collects the tail.
            exit_waiter = asyncio.ensure_future(proc.wait())
            read_task: asyncio.Future[bytes] | None = None
            try:
                while True:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        await self._timeout_turn(state, collector, timeout or 0.0)

                    if read_task is None:
                        read_task = asyncio.ensure_future(proc.stdout.readline())
                    done, _pending = await asyncio.wait(
                        {read_task, exit_waiter},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        await self._timeout_turn(state, collector, timeout or 0.0)
                    if not read_task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(read_task), _DRAIN_SECONDS)
                        except TimeoutError:
                            break
                    line = read_task.result()
                    read_task = None
                    if not line:
                        break

                    self._handle_line(line, state)

                    if max_turns is not None and state.turn_count > max_turns:
                        state.max_turns_exhausted = True
                        await self.kill()
                        break
                    if should_stop is not None and should_stop():
                        stopped_early = True
                        await self.kill()
                        break
            finally:
                if read_task is not None:
                    read_task.cancel()
                exit_waiter.cancel()

            status = await self._settle_turn(
                proc,
                state,
                collector,
                stderr_drain,
                stopped_early=stopped_early,
                deadline=deadline,
                timeout=timeout,
            )
            state.finalize(status)
            # Build BEFORE marking the turn clean: a failure in the reduction is a
            # failed turn, and `_end_turn_ok` clears the rollback flag.
            record = collector.build_turn_record()
            self._end_turn_ok()
            return record

        except (AgentCrashError, TurnTimeoutError):
            # Already funneled through finalize by _crash_turn / _timeout_turn.
            raise
        except asyncio.CancelledError:
            self._finalize_external_cancel(state.finalize)
            self._capture_partial_turn(collector)
            raise
        except Exception as e:
            # A spawn failure, a StreamReader ValueError past `limit`, a malformed
            # payload, a pydantic error. Funnel to the pending-turn contract.
            self._crash_turn(state, collector, f"Pi turn failed: {e!s}", cause=e)
            raise  # unreachable (_crash_turn is NoReturn) — makes the no-fall-through explicit
        finally:
            if stderr_drain is not None:
                stderr_drain.cancel()
            self._reap_orphaned_cli(proc)
            self._process = None

    def _reap_orphaned_cli(self, proc: asyncio.subprocess.Process | None) -> None:
        """Kill a CLI still running as the turn unwinds. No-op otherwise.

        Synchronous (no await) so it survives a ``CancelledError`` in flight.
        ``proc`` is ``None`` when the spawn failed.

        Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
        """
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.kill()
        self._sweep_process_groups()

    async def _settle_turn(
        self,
        proc: asyncio.subprocess.Process,
        state: _PiTurnState,
        collector: EventCollector,
        stderr_drain: asyncio.Future[bytes] | None,
        *,
        stopped_early: bool,
        deadline: float | None,
        timeout: float | None,
    ) -> AgentEndStatus:
        """Reap the CLI once the read loop is done and decide the turn's end status.

        Raises ``AgentCrashError`` (via :meth:`_crash_turn`) when the process died
        with neither an intentional stop nor a recognized event stream. Raises
        ``TurnTimeoutError`` when the deadline elapses while waiting for the exit.
        """
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS if remaining is None else remaining)
        except TimeoutError:
            if remaining is not None:
                await self._timeout_turn(state, collector, timeout or 0.0)
            await self.kill()
            self._crash_turn(
                state,
                collector,
                f"Pi closed its event stream but did not exit within {_TERM_GRACE_SECONDS:.0f}s",
            )
        stderr_bytes = b""
        if stderr_drain is not None:
            with contextlib.suppress(TimeoutError):
                stderr_bytes = await asyncio.wait_for(asyncio.shield(stderr_drain), timeout=_DRAIN_SECONDS)

        # A terminal provider error is infrastructure failure, not an agent
        # failure, and `pi -p` exits 0 after exhausting retries. GATED on
        # intentional cuts: a cut can fire before the clearing `turn_end` arrives,
        # leaving a stale error from a turn pi was still retrying.
        # Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
        if state.error_message is not None and not stopped_early and not state.max_turns_exhausted:
            self._crash_turn(state, collector, f"Pi error: {state.error_message}")

        # A non-zero exit with no intentional cut means the turn died.
        if proc.returncode not in (0, None) and not stopped_early and not state.max_turns_exhausted:
            detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
            self._crash_turn(state, collector, f"Pi exited non-zero: {detail}")

        # A clean exit that recognized NO events is vocabulary drift. Intentional
        # cuts are exempt: a stop can land before the first event.
        if not stopped_early and not state.max_turns_exhausted and state.recognized_events == 0:
            seen = ", ".join(sorted(state.unrecognized_types)) or "none (stdout carried no JSON events)"
            self._crash_turn(
                state,
                collector,
                "Pi exited cleanly but the turn captured no recognized events. Unrecognized event types seen: "
                + f"{seen}. The CLI's event schema may have changed — see docs/agents/PI.md before trusting any "
                + "run from this CLI version.",
            )

        if stopped_early:
            return AgentEndStatus.STOPPED_EARLY
        if state.max_turns_exhausted:
            return AgentEndStatus.MAX_TURNS_EXHAUSTED
        return AgentEndStatus.COMPLETED

    def _crash_turn(
        self,
        state: _PiTurnState,
        collector: EventCollector,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        """Park the crashed partial record and raise ``AgentCrashError``."""
        state.close_open_tools()
        try:
            self._finalize_and_raise_crash(state.finalize, message, cause=cause)
        finally:
            self._capture_partial_turn(collector)

    async def _timeout_turn(
        self,
        state: _PiTurnState,
        collector: EventCollector,
        timeout: float,
    ) -> NoReturn:
        """Kill the CLI, park the crashed partial record, raise ``TurnTimeoutError``."""
        await self.kill()
        state.close_open_tools()
        try:
            self._finalize_and_raise_timeout(state.finalize, timeout)
        finally:
            self._capture_partial_turn(collector)

    def _handle_line(self, line: bytes, state: _PiTurnState) -> None:
        """Parse one nd-JSON line and dispatch it. Never raises on bad input.

        ``agent_end`` is NOT terminal — only ``agent_settled`` / stdout EOF is — so
        it is recognized, ignored, and the read loop keeps going.
        """
        raw = line.decode("utf-8", "replace").strip()
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("pi: skipping non-JSON stdout line: %s", raw[:200])
            return
        if not isinstance(obj, dict):
            return

        event_type = str(obj.get("type") or "")
        # `session`, `message_start`, `message_end`, `agent_end` and
        # `agent_settled` carry no state we accumulate, but are all recognized.
        if event_type in _RECOGNIZED_EVENTS:
            state.recognized_events += 1
        elif len(state.unrecognized_types) < _MAX_UNRECOGNIZED_TYPES:
            state.unrecognized_types.add(event_type or "<missing type>")

        if event_type == "turn_start":
            state.on_turn_start()
        elif event_type == "message_update":
            state.on_message_update(obj)
        elif event_type == "tool_execution_start":
            state.on_tool_execution_start(obj)
        elif event_type == "tool_execution_end":
            state.on_tool_execution_end(obj)
        elif event_type == "turn_end":
            state.on_turn_end(obj)
        else:
            logger.debug("pi: unhandled event type %r", event_type)
