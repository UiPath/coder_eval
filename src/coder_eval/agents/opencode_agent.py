"""OpenCode agent implementation (the open-source terminal coding agent).

Drives the ``opencode`` CLI in non-interactive mode, which streams
newline-delimited JSON events on stdout, and reduces that stream into the
standardized coder_eval event protocol so :class:`EventCollector` builds the
``TurnRecord``.

The CLI emits TWO envelope shapes on the same stream: the normal form carries
its payload under ``part``, while the CLI's own error path emits a flat object
with none. :func:`_unwrap` normalizes both to ``(event_type, payload)`` so the
dispatch table is written once.

The ``sessionID`` observed on the first event is replayed via ``--session`` on
the next ``communicate()``, which is what makes dialog mode work against a
stateless CLI invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar, Literal, NoReturn

from coder_eval.agent import Agent
from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.isolation.docker_runner import STDOUT_LINE_LIMIT_BYTES
from coder_eval.models import (
    AgentKind,
    AgentState,
    ApiRoute,
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    OpenCodeAgentConfig,
    PermissionMode,
    ResultSummary,
    SystemPromptSemantics,
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
from coder_eval.timing import close_window

from ._skills import _plugin_skill_dirs
from .registry import AgentRegistry


logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when tearing down the CLI subprocess.
# Doubles as the post-EOF exit grace in _settle_turn when no deadline is set.
_TERM_GRACE_SECONDS = 5.0

# SIGKILL does not exist on Windows (where the process-group sweep is a no-op
# anyway); resolve it dynamically so the module imports and typechecks on every
# platform, falling back to SIGTERM for the direct-pid kill_sync path.
_SIGKILL: signal.Signals = getattr(signal, "SIGKILL", signal.SIGTERM)

# How long to keep draining stdout/stderr after the CLI has been reaped:
# `opencode run` leaves a server child holding the pipes open, so EOF never
# arrives on its own.
# Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
_DRAIN_SECONDS = 2.0

# The CLI's OWN compact vocabulary, captured from a live run — NOT the
# `session.next.*` names in the server's OpenAPI schema, which describe
# `opencode serve`'s HTTP/SSE surface. The two are not interchangeable.
_STEP_START = "step_start"
_STEP_FINISH = "step_finish"
_TEXT = "text"
_TOOL_USE = "tool_use"
_ERROR = "error"

# The full recognized vocabulary. A zero-exit turn that recognized NOTHING from
# it captured zero telemetry and is crashed, not scored.
# Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
_RECOGNIZED_EVENTS = frozenset({_STEP_START, _STEP_FINISH, _TEXT, _TOOL_USE, _ERROR})

# How many distinct unrecognized event-type strings to retain for the crash
# message when the vocabulary check fails (diagnosis, not an exhaustive list).
_MAX_UNRECOGNIZED_TYPES = 8

# OpenCode's native tool names -> the canonical (Claude) vocabulary that every
# criterion is written against. Unknown tools pass through unchanged.
# Rationale: .claude/notes/agents.md § Tool-name and argument normalization
_TOOL_NAME_MAP: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "multiedit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "list": "LS",
    "webfetch": "WebFetch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "task": "Agent",
    # The GPT-family edit tool: OpenCode's tool set varies by MODEL within this
    # one harness. `Write` matches codex_agent's own mapping.
    "apply_patch": "Write",
    # OpenCode's native skill loader; `skill_triggered` keys on the canonical name.
    "skill": "Skill",
}

# OpenCode per-tool INPUT-arg key -> canonical (Claude) key, keyed by the
# canonical tool name (post _TOOL_NAME_MAP). Unlisted keys pass through.
# `bash`/`glob`/`grep`/`list` need no entry — their keys already match Claude's.
# BOTH file-path spellings are mapped because the CLI has MOVED between them.
# Rationale: .claude/notes/agents.md § Tool-name and argument normalization
_OPENCODE_ARG_RENAME: dict[str, dict[str, str]] = {
    "Read": {"path": "file_path", "filePath": "file_path"},
    "Write": {"path": "file_path", "filePath": "file_path"},
    "Edit": {
        "path": "file_path",
        "filePath": "file_path",
        "oldString": "old_string",
        "newString": "new_string",
        "replaceAll": "replace_all",
    },
    # So `skill_triggered` reads the agent-agnostic `parameters["skill"]` rather
    # than carrying a per-harness alternative list.
    "Skill": {"name": "skill"},
}

# Config fields the OpenCode CLI has no equivalent knob for. `experiments/default.yaml`
# sets `allowed_tools` on every task, so start() warns once rather than letting a
# task believe it constrained the agent. `plugins` is NOT here: its skills half is
# honored. Per-harness table: docs/agents/HARNESS_PARITY.md.
_UNSUPPORTED_CONFIG_FIELDS: tuple[str, ...] = (
    "system_prompt",
    "system_prompt_file",
    "allowed_tools",
    "disallowed_tools",
)

# Skill injection: each plugin root's skills dir is merged into `skills.paths`
# through this variable, which OpenCode applies as a final local-scope layer.
# Only the SKILLS half of a plugin is honored.
# Rationale: .claude/notes/agents.md § Skills, per harness
_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"

# ToolEndStatus -> CommandTelemetry.result_status (the persisted tri-state).
_RESULT_STATUS: dict[ToolEndStatus, Literal["success", "error", "unknown"]] = {
    ToolEndStatus.OK: "success",
    ToolEndStatus.ERROR: "error",
    ToolEndStatus.PERMISSION_DENIED: "error",
    ToolEndStatus.UNRESOLVED: "unknown",
}


def _unwrap(obj: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalize an OpenCode CLI event to ``(event_type, payload)``.

    Every line carries its payload under ``part`` except the CLI's own error
    line, which is flat. Returning the top-level dict for that case is safe: the
    accessors read named keys, never iterate.
    """
    event_type = str(obj.get("type") or "")
    part = obj.get("part")
    if isinstance(part, dict):
        return event_type, part
    return event_type, obj


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    """Convert OpenCode's epoch-millisecond timestamps to naive local datetimes.

    Naive-local matches the rest of the telemetry, so durations stay consistent.
    """
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _canonical_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Rename a tool call's argument keys to the canonical cross-agent vocabulary.

    Order is preserved and unlisted keys pass through untouched.
    """
    rename = _OPENCODE_ARG_RENAME.get(tool_name)
    if not rename:
        return params
    return {rename.get(key, key): value for key, value in params.items()}


class _OpenCodeTurnState:
    """Per-``communicate()`` accumulator: events in, finalization payload out.

    Owns everything the terminal ``AgentEndEvent`` must carry (transcript
    messages, cumulative usage, text output) plus the open-tool bookkeeping
    needed to force-close orphans when a turn dies mid-flight.
    """

    def __init__(self, *, task_id: str, iteration: int, user_input: str, model: str | None) -> None:
        self.task_id = task_id
        self.iteration = iteration
        self.user_input = user_input
        self.model = model

        self.started_at = time.monotonic()
        self.session_id: str | None = None
        self.thread_id: str | None = None

        # Cumulative turn totals (summed across every inner step).
        self.usage = TokenUsage()
        self.cost_usd: float = 0.0
        self.saw_cost = False

        self.messages: list[TranscriptMessage] = []
        self.text_parts: list[str] = []
        self.step_count = 0
        # Steps the CLI reported as FINISHED, as opposed to `step_count`, which
        # counts the ones it started. `_settle_turn` needs the distinction.
        self.steps_finished = 0
        self.turn_id: str = ""
        # True between a step's `step_start` and its `step_finish`. `finalize`
        # needs it to close a TurnStartEvent the stream never got to close.
        self.step_open = False
        self.step_started_at: datetime | None = None
        # Where the NEXT generation window starts: the previous step's finish.
        # None until the first step finishes, and deliberately so — everything
        # before the first `step_start` is CLI process spawn, not model time.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.gen_mark: datetime | None = None
        self.step_text_parts: list[str] = []
        self.step_tool_ids: list[str] = []

        # callID -> (telemetry, started_at) for tools awaiting a result.
        self.open_tools: dict[str, CommandTelemetry] = {}
        self.sequence = 0
        self.stop_reason: str | None = None
        self.error_message: str | None = None
        self.max_turns_exhausted = False
        # Guards the one-terminal-event rule; see finalize().
        self.finalized = False
        # Guards _warn_token_shape: one report per turn, not one per step.
        self.warned_token_shape = False
        # Vocabulary drift detection (see _settle_turn): how many events matched
        # _RECOGNIZED_EVENTS, and a bounded sample of the types that did not.
        self.recognized_events = 0
        self.unrecognized_types: set[str] = set()

        self._emit: Callable[[StreamEvent], None] = lambda _e: None

    def bind(self, emit: Callable[[StreamEvent], None]) -> None:
        self._emit = emit

    def emit(self, event: StreamEvent) -> None:
        self._emit(event)

    @property
    def agent_output(self) -> str:
        return "".join(self.text_parts)

    # --- event handlers ----------------------------------------------------

    def on_step_start(self, part: dict[str, Any]) -> None:
        self.step_count += 1
        self.step_open = True
        self.turn_id = str(part.get("messageID") or f"step_{self.step_count}")
        self.step_started_at = datetime.now()
        self.step_text_parts = []
        self.step_tool_ids = []
        # No per-step span list to reset here any more: the collector sees every
        # span at once and clips each to the window it overlaps.
        self.emit(
            TurnStartEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                model=self.model,
            )
        )

    def on_text(self, part: dict[str, Any]) -> None:
        """``text`` carries a COMPLETE assistant message, not a streaming delta."""
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return
        self.text_parts.append(text)
        self.step_text_parts.append(text)
        self.emit(TextChunkEvent(task_id=self.task_id, thread_id=self.thread_id, turn_id=self.turn_id, text=text))

    def on_tool_use(self, part: dict[str, Any]) -> None:
        """A ``tool_use`` event carries the tool's whole state under ``state``.

        In practice the CLI emits one already-``completed`` event per call rather
        than a call/result pair, so both ``ToolStart`` and ``ToolEnd`` are
        synthesized here. A non-terminal state is still handled: the tool is left
        open and closed by a later event for the same ``callID``, or force-closed
        as ``unresolved``. Execution timestamps come from ``state.time``, so
        ``duration_ms`` is the tool's real runtime, not our parse instant.
        """
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        call_id = str(part.get("callID") or f"call_{self.sequence + 1}")
        time_val = state.get("time")
        times = time_val if isinstance(time_val, dict) else {}
        started = _epoch_ms_to_dt(times.get("start"))
        params = state.get("input")
        params = params if isinstance(params, dict) else {}

        telemetry = self.open_tools.get(call_id)
        if telemetry is None:
            self.sequence += 1
            raw_tool = str(part.get("tool") or "unknown")
            tool_name = _TOOL_NAME_MAP.get(raw_tool.lower(), raw_tool)
            telemetry = CommandTelemetry(
                tool_name=tool_name,
                tool_id=call_id,
                assistant_turn_index=self.step_count,
                timestamp=started or datetime.now(),
                execution_started_at=started,
                parameters=_canonical_params(tool_name, params),
                sequence_number=self.sequence,
            )
            self.open_tools[call_id] = telemetry
            self.step_tool_ids.append(call_id)
            self.emit(
                ToolStartEvent(task_id=self.task_id, thread_id=self.thread_id, turn_id=self.turn_id, tool=telemetry)
            )
        else:
            # A SECOND event for a call already open. The first routinely carries
            # no `input` yet, so freezing its view would leave `parameters`
            # permanently `{}` and zero every `command_executed` row while the run
            # looked normal. Later evidence wins; absent evidence clears nothing.
            if params:
                telemetry.parameters = _canonical_params(telemetry.tool_name, params)
            if started is not None:
                telemetry.execution_started_at = started

        status_text = str(state.get("status") or "").lower()
        output = state.get("output")
        error_text = state.get("error")
        if status_text in ("pending", "running"):
            return  # still in flight; a later event (or the orphan sweep) closes it

        if status_text == "error" or error_text:
            message = str(error_text or output or "tool failed")
            denied = "permission" in message.lower() or "denied" in message.lower()
            status = ToolEndStatus.PERMISSION_DENIED if denied else ToolEndStatus.ERROR
        else:
            message = None
            status = ToolEndStatus.OK

        # `times` is the SAME dict read at the top: nothing between rebinds or
        # mutates `state`.
        self._close_tool(
            call_id,
            status=status,
            summary=output if isinstance(output, str) else None,
            error=message,
            completed_at=_epoch_ms_to_dt(times.get("end")),
        )

    def _close_tool(
        self,
        call_id: str,
        *,
        status: ToolEndStatus,
        summary: str | None,
        error: str | None,
        completed_at: datetime | None = None,
    ) -> None:
        telemetry = self.open_tools.pop(call_id, None)
        if telemetry is None:
            # A result with no matching call (shouldn't happen, but never drop it).
            self.sequence += 1
            telemetry = CommandTelemetry(
                tool_name="unknown",
                tool_id=call_id,
                assistant_turn_index=self.step_count,
                timestamp=datetime.now(),
                sequence_number=self.sequence,
            )
        completed = completed_at or datetime.now()
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

    def _rate_card_cost(self) -> float | None:
        """Price the captured buckets from the static rate card.

        ``None`` when the model is unpinned or unpriced.
        """
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

        A non-zero cost the CLI reported always wins. The rate card fills two gaps
        that would otherwise book tokens with no money: no ``cost`` field at all,
        and ``cost: 0`` for tokens the rate card prices above zero.

        Rationale: .claude/notes/agents.md § Cost: the stream versus the rate card
        """
        rate = self._rate_card_cost()
        if not self.saw_cost:
            return rate
        if self.cost_usd == 0.0 and rate:
            logger.warning(
                "opencode: the stream reported $0 for a turn the rate card prices at $%.6f "
                + "(model unpriced in OpenCode's registry, or subscription auth); using the rate card "
                + "so the run total is not understated.",
                rate,
            )
            return rate
        return self.cost_usd

    def _warn_token_shape(self, message: str, *args: Any) -> None:
        """Report a token-bucket surprise ONCE per turn (a broken stream repeats it)."""
        if self.warned_token_shape:
            return
        self.warned_token_shape = True
        logger.warning("opencode: unexpected token accounting — " + message, *args)

    def _as_int(self, bucket: str, value: Any) -> int:
        """Coerce one stream-supplied token count, warning instead of raising.

        This is what makes ``_handle_line``'s advertised "Never raises on bad
        input" true.

        Rationale: .claude/notes/agents.md § Why token-shape drift warns instead of raising
        """
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            if value is not None:
                self._warn_token_shape(
                    "tokens.%s is %r (%s), not a number; counting it as 0 — the CLI's token schema "
                    + "may have changed, so re-check docs/agents/OPENCODE.md before trusting cost",
                    bucket,
                    value,
                    type(value).__name__,
                )
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            self._warn_token_shape(
                "tokens.%s is %r, which is not convertible to a number; counting it as 0 — the CLI's "
                + "token schema may have changed, so re-check docs/agents/OPENCODE.md before trusting cost",
                bucket,
                value,
            )
            return 0

    def _fresh_input_slice(
        self, tokens: dict[str, Any], raw_in: int, raw_out: int, reasoning: int, cw: int, cr: int
    ) -> int:
        """Decide what ``tokens.input`` means on this stream — per step, from evidence.

        Two conventions exist in the wild: **flat**, where ``input`` already IS the
        fresh slice and ``total = input + output + reasoning + cache``, and
        **nested**, where the cache buckets are counted inside ``input`` (the
        OpenAI ``prompt_tokens`` convention) and ``total = input + output +
        reasoning``.

        The stream's own ``total`` arbitrates PER STEP. With no cache traffic the
        two agree. With no usable ``total`` the flat reading is taken, but warns
        once if cache traffic is present — that is an unverifiable assumption, and
        the original mapping bug was exactly one of those. A ``total`` matching
        NEITHER warns loudly.

        Rationale: .claude/notes/agents.md § Token accounting, per harness
        """
        total = tokens.get("total")
        if not isinstance(total, int):
            if cr or cw:
                self._warn_token_shape(
                    "tokens.total is missing with cache traffic present (cache.read=%d, cache.write=%d); "
                    + "assuming the flat convention (`input` is the fresh slice) but the mapping cannot be "
                    + "verified for this stream — re-check docs/agents/OPENCODE.md before trusting cost",
                    cr,
                    cw,
                )
            return raw_in
        nested = raw_in + raw_out + reasoning
        flat = nested + cr + cw
        # Check flat first: with zero cache traffic the two sums coincide and the
        # conventions agree, so `input` is the fresh slice either way.
        if total == flat:
            return raw_in
        if total == nested:  # implies cache traffic, since flat was checked first
            fresh = raw_in - cr - cw
            if fresh < 0:
                # The stream contradicts itself: `total` says the cache buckets
                # nest inside `input`, but `input` is too small to hold them.
                self._warn_token_shape(
                    "tokens.total says the cache buckets nest inside input, but input(%d) < "
                    + "cache.read(%d) + cache.write(%d); keeping `input` as the fresh slice",
                    raw_in,
                    cr,
                    cw,
                )
                return raw_in
            return fresh
        self._warn_token_shape(
            "tokens.total(%d) matches neither input+output+reasoning(%d) nor that sum plus the cache "
            + "buckets(%d); the bucket mapping may no longer match the CLI — re-check "
            + "docs/agents/OPENCODE.md before trusting cost",
            total,
            nested,
            flat,
        )
        return raw_in

    def on_step_finish(self, part: dict[str, Any]) -> None:
        self.steps_finished += 1
        self.step_open = False
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache_val = tokens.get("cache")
        cache = cache_val if isinstance(cache_val, dict) else {}
        raw_in = self._as_int("input", tokens.get("input") or 0)
        raw_out = self._as_int("output", tokens.get("output") or 0)
        step_reasoning = self._as_int("reasoning", tokens.get("reasoning") or 0)
        step_cw = self._as_int("cache.write", cache.get("write") or 0)
        step_cr = self._as_int("cache.read", cache.get("read") or 0)

        step_in = self._fresh_input_slice(tokens, raw_in, raw_out, step_reasoning, step_cw, step_cr)
        # Reasoning bills at the output rate but is reported apart from `output`,
        # so fold it into the turn total; the per-message record keeps it apart.
        step_out = raw_out + step_reasoning

        self.usage = TokenUsage(
            uncached_input_tokens=self.usage.uncached_input_tokens + step_in,
            output_tokens=self.usage.output_tokens + step_out,
            cache_creation_input_tokens=self.usage.cache_creation_input_tokens + step_cw,
            cache_read_input_tokens=self.usage.cache_read_input_tokens + step_cr,
        )
        cost = part.get("cost")
        if isinstance(cost, int | float):
            self.cost_usd += float(cost)
            self.saw_cost = True

        finish = part.get("reason")
        if isinstance(finish, str) and finish:
            self.stop_reason = finish

        completed = datetime.now()
        step_start = self.step_started_at or completed
        blocks: list[ContentBlock] = []
        step_text = "".join(self.step_text_parts)
        if step_text:
            blocks.append(ContentBlock(block_type="text", sequence=0, text=step_text))
        for i, tool_id in enumerate(self.step_tool_ids, start=len(blocks)):
            blocks.append(ContentBlock(block_type="tool_use", sequence=i, tool_use_id=tool_id))

        # Tile from the previous step's finish. The RAW window only.
        started, generation_ms = close_window(
            mark=self.gen_mark if self.gen_mark is not None else step_start,
            now=completed,
            item_start=step_start,
        )
        self.messages.append(
            AssistantMessage(
                started_at=started,
                completed_at=completed,
                generation_duration_ms=generation_ms,
                content_blocks=blocks,
                tool_use_ids=list(self.step_tool_ids),
                input_tokens=step_in,
                output_tokens=step_out,
                cache_creation_tokens=step_cw,
                cache_read_tokens=step_cr,
                reasoning_tokens=step_reasoning,
                stop_reason=finish if isinstance(finish, str) else None,
                model=self.model,
                message_id=str(part.get("messageID") or "") or None,
            )
        )
        # A message was appended, so the next window starts where this one ended.
        # Only `step_finish` advances the mark.
        self.gen_mark = completed
        # SPENT state, cleared HERE and not only in `on_step_start`: a second
        # `step_finish` with no intervening start would otherwise republish this
        # step's whole span as the next one's. The `min()` in `close_window` still
        # defends a genuinely OPEN step against a backwards clock, which is what
        # it is for — this reducer's stamps are raw `datetime.now()`.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.step_started_at = None
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

    def on_error(self, part: dict[str, Any]) -> None:
        """Record the CLI's own structured error, which ``_settle_turn`` crashes on.

        The payload is the flat envelope, and its shape varies: a nested
        ``error.data.message`` when the CLI has one, otherwise the error's
        ``name``. Anything else degrades to its string form rather than raising.
        """
        error = part.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            message = (data or {}).get("message") if isinstance(data, dict) else None
            self.error_message = str(message or error.get("name") or "unknown error")
        else:
            self.error_message = str(error or "unknown error")

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
        """Close orphaned tools and emit the terminal ``AgentEndEvent``.

        Idempotent: the protocol allows EXACTLY ONE ``AgentEndEvent`` per
        ``communicate()``.

        Rationale: .claude/notes/agents.md § Shared turn lifecycle
        """
        if self.finalized:
            return
        self.finalized = True
        self.close_open_tools()
        usage = self.usage
        cost = self._resolve_cost()
        if cost is not None:
            usage = usage.model_copy(update={"total_cost_usd": cost})
        # A step still open never received its `step_finish`; close it or the
        # one-pair-per-inner-turn contract breaks. Completed steps already closed
        # themselves, so this fires ONLY for the straggler.
        if self.step_open:
            self.step_open = False
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
                assistant_turn_count=self.step_count,
                messages=list(self.messages),
                num_turns=self.step_count,
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
            )
        )


@AgentRegistry.register(AgentKind.OPENCODE, OpenCodeAgentConfig)
class OpenCodeAgent(Agent[OpenCodeAgentConfig]):
    """Runs the ``opencode`` CLI as a subprocess, one invocation per turn."""

    # `should_stop` is polled at every event boundary (tool-call granularity).
    supports_cooperative_stop: ClassVar[bool] = True

    # No CLI knob for `system_prompt`, so the honest regime is `"unknown"`.
    # Rationale: .claude/notes/agents.md § The system_prompt_semantics marker
    system_prompt_semantics: ClassVar[SystemPromptSemantics] = "unknown"

    def __init__(
        self,
        config: OpenCodeAgentConfig,
        route: ApiRoute | None = None,
        *,
        task_id: str = "unknown",
    ) -> None:
        """Every parameter the agent factory can pass is DECLARED, not absorbed.

        ``route`` is accepted for factory parity and deliberately unused: the CLI
        owns its own provider configuration (``docs/agents/OPENCODE.md``), so the
        run's Bedrock/Anthropic routing does not apply. ``task_id`` only labels
        the event stream.

        Rationale: .claude/notes/agents.md § Why the constructors declare every kwarg
        """
        self.config = config
        self.route = route
        self.task_id = task_id
        self.working_directory: str | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        self._skill_dirs: list[str] = []
        self._session_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        # Process-group ids of every invocation this agent spawned, swept on
        # kill()/kill_sync()/stop(): signalling only the CLI pid orphans the
        # server child `opencode run` leaves behind.
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
        if shutil.which("opencode") is None:
            raise RuntimeError(
                "The 'opencode' CLI was not found on PATH."
                + " Install it with `npm install -g opencode-ai` (or see https://opencode.ai/docs/)."
            )
        ignored = [f for f in _UNSUPPORTED_CONFIG_FIELDS if getattr(self.config, f, None)]
        if ignored:
            logger.warning(
                "opencode: %s set but NOT enforced — the CLI has no equivalent knob, so the run is "
                + "unconstrained by them; do not rely on them as a boundary (see docs/agents/OPENCODE.md).",
                ", ".join(ignored),
            )
        self._skill_dirs = _plugin_skill_dirs(self.config.plugins, log=logger)
        if self._skill_dirs:
            logger.info(
                "opencode: injecting %d skill path(s) via %s: %s",
                len(self._skill_dirs),
                _CONFIG_CONTENT_ENV,
                self._skill_dirs,
            )
        elif self.config.plugins:
            # The run is about to measure the model without the skills under
            # test. Say so loudly.
            logger.warning(
                "opencode: %d plugin(s) declared but 0 skill path(s) resolved — the agent will run "
                + "WITHOUT them (see docs/agents/OPENCODE.md).",
                len(self.config.plugins),
            )
        self.working_directory = working_directory
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        self._session_id = None
        self._state = AgentState.WORKING

    async def stop(self) -> None:
        await self.kill()
        self._mark_stopped()

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
        the group holds ONLY what that invocation spawned. The CLI itself gets
        SIGTERM-then-SIGKILL first (see ``kill``); this reaps what survives.
        Sessions persist on disk, so this does not lose ``--session`` continuity.
        """
        if os.name != "posix":
            return
        for pgid in self._spawned_pgids:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, _SIGKILL)
        self._spawned_pgids.clear()

    def get_environment_info(self) -> dict[str, Any]:
        # Base first so the `system_prompt_semantics` run marker is always
        # present (an absent marker reads as a pre-marker run).
        info: dict[str, Any] = {
            **super().get_environment_info(),
            "opencode_model": self.config.model,
            "opencode_pure": self.config.pure,
        }
        if self._skill_dirs:
            # Recorded per task so a report can confirm the skills reached the
            # agent.
            info["opencode_skill_paths"] = list(self._skill_dirs)
        if self.config.variant:
            info["opencode_variant"] = self.config.variant
        if self._session_id:
            info["opencode_session_id"] = self._session_id
        return info

    # --- command construction ---------------------------------------------

    def _build_argv(self, user_input: str) -> list[str]:
        argv = ["opencode", "run", "--format", "json"]
        if self.config.model:
            argv += ["-m", self.config.model]
        if self.working_directory:
            argv += ["--dir", self.working_directory]
        if self.config.variant:
            argv += ["--variant", self.config.variant]
        if self.config.pure:
            argv.append("--pure")
        # PLAN is the one mode that must not auto-approve side effects; every
        # other runs unattended, where an approval prompt would simply hang.
        if self.config.permission_mode is not PermissionMode.PLAN:
            argv.append("--auto")
        if self._session_id:
            argv += ["--session", self._session_id]
        argv.append("--")
        argv.append(user_input)
        return argv

    def _build_env(self) -> dict[str, str]:
        """The CLI's full environment: the host's, plus the sandbox's contributions.

        The PATH prepend is the mock-shadowing contract (``Agent.start``): the
        sandbox's mock CLI directories must resolve BEFORE the real binaries, in
        the order given, or a task grading a mocked CLI silently exercises the
        real one. ``PLUGIN_TOOLS_DIR`` is advisory and never overrides an
        inherited value.

        Returns the WHOLE environment, seeded from ``os.environ``, whose keys
        CPython upper-cases on Windows — so ``"PATH"`` is the inherited key on
        every platform and cannot duplicate a differently-cased one. (Codex hands
        the SDK a PARTIAL dict instead, which is why it resolves the key
        case-insensitively.)
        """
        env = dict(os.environ)
        if self._env_path_prepend:
            env["PATH"] = os.pathsep.join([*self._env_path_prepend, env.get("PATH", "")])
        if self._plugin_tools_dir and "PLUGIN_TOOLS_DIR" not in env:
            env["PLUGIN_TOOLS_DIR"] = self._plugin_tools_dir
        self._inject_skill_paths(env)
        return env

    def _inject_skill_paths(self, env: dict[str, str]) -> None:
        """Merge the resolved skill directories into ``OPENCODE_CONFIG_CONTENT``.

        No plugins means the variable is left exactly as inherited. An inherited
        value is appended to, never clobbered: the host may legitimately configure
        OpenCode through the same seam.
        """
        if not self._skill_dirs:
            return
        config: dict[str, Any] = {}
        inherited = env.get(_CONFIG_CONTENT_ENV)
        if inherited:
            try:
                parsed = json.loads(inherited)
            except json.JSONDecodeError:
                logger.warning(
                    "opencode: inherited %s is not valid JSON; replacing it with the injected skill paths.",
                    _CONFIG_CONTENT_ENV,
                )
            else:
                if isinstance(parsed, dict):
                    config = parsed
                else:
                    logger.warning(
                        "opencode: inherited %s is not a JSON object; replacing it with the injected skill paths.",
                        _CONFIG_CONTENT_ENV,
                    )
        skills = config.get("skills")
        skills = dict(skills) if isinstance(skills, dict) else {}
        existing = [path for path in skills.get("paths", []) if isinstance(path, str)]
        skills["paths"] = existing + [path for path in self._skill_dirs if path not in existing]
        config["skills"] = skills
        env[_CONFIG_CONTENT_ENV] = json.dumps(config)

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
            raise RuntimeError("OpenCodeAgent.start() must be called before communicate()")

        self._begin_turn()
        collector = EventCollector()

        def emit(event: StreamEvent) -> None:
            collector.on_event(event)
            if stream_callback is not None:
                safe_emit(stream_callback, event)

        state = _OpenCodeTurnState(
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
            )
        )

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
                # Own session/process group, so teardown can killpg the server
                # child. POSIX-only knob; harmless False elsewhere.
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

            # The server child INHERITS this stdout pipe, so it is not closed when
            # the CLI exits: race each read against process exit, then drain.
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
                        # Exited with the read still pending: bound the tail rather
                        # than wait on the grandchild's open write end.
                        try:
                            await asyncio.wait_for(asyncio.shield(read_task), _DRAIN_SECONDS)
                        except TimeoutError:
                            break
                    line = read_task.result()
                    read_task = None
                    if not line:
                        break

                    self._handle_line(line, state)

                    if max_turns is not None and state.step_count > max_turns:
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
            # Everything the loop does NOT anticipate. Without this the exception
            # escapes raw and breaks the pending-turn contract three ways: no
            # AgentEndEvent, the telemetry dropped rather than parked, and
            # `_iteration` left incremented.
            self._crash_turn(state, collector, f"OpenCode turn failed: {e!s}", cause=e)
            raise  # unreachable (_crash_turn is NoReturn) — makes the no-fall-through explicit
        finally:
            if stderr_drain is not None:
                stderr_drain.cancel()
            self._reap_orphaned_cli(proc)
            self._process = None

    def _reap_orphaned_cli(self, proc: asyncio.subprocess.Process | None) -> None:
        """Kill a CLI that is still running as the turn unwinds. No-op otherwise.

        Deliberately synchronous: this runs while a ``CancelledError`` is
        propagating, where any await can itself be cut short. Skipping the SIGTERM
        courtesy is right for a turn that is already lost — :meth:`kill` still
        owns every path with something left to flush. ``proc`` is ``None`` when
        the spawn itself failed.

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
        state: _OpenCodeTurnState,
        collector: EventCollector,
        stderr_drain: asyncio.Future[bytes] | None,
        *,
        stopped_early: bool,
        deadline: float | None,
        timeout: float | None,
    ) -> AgentEndStatus:
        """Reap the CLI once the read loop is done and decide the turn's end status.

        Raises ``AgentCrashError`` (via :meth:`_crash_turn`) on a structured error,
        on a death with neither a structured error nor an intentional stop, or on
        a clean exit that captured no token telemetry. Raises ``TurnTimeoutError``
        when the deadline elapses while waiting for the exit.
        """
        # Bound the reap: the read loop can end at EOF with the CLI still alive,
        # and an unbounded wait here would outlive the turn deadline.
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
                f"OpenCode closed its event stream but did not exit within {_TERM_GRACE_SECONDS:.0f}s",
            )
        # Bounded for the same reason as the read loop: the inherited stderr pipe
        # outlives the CLI. Shielded so the timeout doesn't kill it early.
        stderr_bytes = b""
        if stderr_drain is not None:
            with contextlib.suppress(TimeoutError):
                stderr_bytes = await asyncio.wait_for(asyncio.shield(stderr_drain), timeout=_DRAIN_SECONDS)

        if state.error_message is not None:
            self._crash_turn(state, collector, f"OpenCode error: {state.error_message}")

        # A non-zero exit with no structured error still means the turn died:
        # surface stderr rather than reporting a silent empty success.
        if proc.returncode not in (0, None) and not stopped_early and not state.max_turns_exhausted:
            detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
            self._crash_turn(state, collector, f"OpenCode exited non-zero: {detail}")

        # A clean exit that captured NO token telemetry must not score. Keying on
        # the token counts ALONE is what misses the second arm: an exit that
        # recognized no events at all reaches the same silent-empty-success
        # outcome. Intentional cuts are exempt — either can land before the first
        # event, or mid-step. (The two arms are NOT interchangeable downstream;
        # see the require_token_telemetry escape hatch below.)
        # Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
        nothing_recognized = state.recognized_events == 0
        finished_without_tokens = state.steps_finished > 0 and state.usage.is_empty()
        if not stopped_early and not state.max_turns_exhausted and (nothing_recognized or finished_without_tokens):
            if nothing_recognized:
                seen = ", ".join(sorted(state.unrecognized_types)) or "none (stdout carried no JSON events)"
                detail = f"It emitted no recognized events at all. Unrecognized event types seen: {seen}."
            else:
                detail = (
                    f"It reported {state.steps_finished} finished step(s), none of which carried usable "
                    + f"token counts (cost reported: {'yes' if state.saw_cost else 'no'})."
                )
            message = (
                f"OpenCode exited cleanly but the turn captured zero token telemetry. {detail} The CLI's "
                + "event or token schema may have changed — see docs/agents/OPENCODE.md (Telemetry) before "
                + "trusting any run from this CLI version."
            )
            # Escape hatch for a provider/auth mode that reports no usage at all.
            # Deliberately does NOT cover `nothing_recognized`: that arm is
            # vocabulary drift, which no provider quirk explains.
            if not self.config.require_token_telemetry and not nothing_recognized:
                logger.warning("opencode: %s Scored anyway — require_token_telemetry is off.", message)
            else:
                self._crash_turn(state, collector, message)

        if stopped_early:
            return AgentEndStatus.STOPPED_EARLY
        if state.max_turns_exhausted:
            return AgentEndStatus.MAX_TURNS_EXHAUSTED
        return AgentEndStatus.COMPLETED

    def _crash_turn(
        self,
        state: _OpenCodeTurnState,
        collector: EventCollector,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        """Park the crashed partial record and raise ``AgentCrashError``.

        ``cause`` preserves the ``__cause__`` link from an ``except ... as e``.
        """
        state.close_open_tools()
        try:
            self._finalize_and_raise_crash(state.finalize, message, cause=cause)
        finally:
            self._capture_partial_turn(collector)

    async def _timeout_turn(
        self,
        state: _OpenCodeTurnState,
        collector: EventCollector,
        timeout: float,
    ) -> NoReturn:
        """Kill the CLI, park the crashed partial record, raise ``TurnTimeoutError``.

        The partial record is captured immediately after, so ``pending_turn``
        carries everything observed before the deadline.
        """
        await self.kill()
        state.close_open_tools()
        try:
            self._finalize_and_raise_timeout(state.finalize, timeout)
        finally:
            self._capture_partial_turn(collector)

    def _handle_line(self, line: bytes, state: _OpenCodeTurnState) -> None:
        """Parse one nd-JSON line and dispatch it. Never raises on bad input."""
        raw = line.decode("utf-8", "replace").strip()
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # OpenCode interleaves non-JSON notices (the Bun AVX warning) on
            # stdout; a malformed line must not kill the turn.
            logger.debug("opencode: skipping non-JSON stdout line: %s", raw[:200])
            return
        if not isinstance(obj, dict):
            return

        event_type, part = _unwrap(obj)
        if event_type in _RECOGNIZED_EVENTS:
            state.recognized_events += 1
        elif len(state.unrecognized_types) < _MAX_UNRECOGNIZED_TYPES:
            state.unrecognized_types.add(event_type or "<missing type>")

        # sessionID rides on the envelope, not the part.
        session_id = obj.get("sessionID") or part.get("sessionID")
        if isinstance(session_id, str) and session_id:
            if state.session_id is None:
                state.session_id = session_id
                state.thread_id = session_id
            self._session_id = session_id

        if event_type == _STEP_START:
            state.on_step_start(part)
        elif event_type == _TEXT:
            state.on_text(part)
        elif event_type == _TOOL_USE:
            state.on_tool_use(part)
        elif event_type == _STEP_FINISH:
            state.on_step_finish(part)
        elif event_type == _ERROR:
            state.on_error(part)
        else:
            logger.debug("opencode: unhandled event type %r", event_type)
