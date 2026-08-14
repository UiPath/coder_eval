"""OpenCode agent implementation (the open-source terminal coding agent).

Drives the ``opencode`` CLI in non-interactive mode::

    opencode run --format json -m <provider/model> --dir <cwd> [--auto] [--pure] <prompt>

which streams **newline-delimited JSON events** on stdout. Each line is one
event; this module reduces that stream into the standardized coder_eval event
protocol (``AgentStart`` / ``TurnStart`` / ``ToolStart`` / ``ToolEnd`` /
``TurnEnd`` / ``AgentEnd``) and lets :class:`EventCollector` build the
``TurnRecord`` — so no telemetry is assembled by hand here.

Envelope normalization
----------------------
The CLI emits two envelope shapes on the same stream: the normal form carries
its payload under ``part`` — ``{"type": "tool_use", "sessionID": …,
"part": {…}}`` — while the CLI's own error path emits a flat object with no
``part`` (``{"type": "error", "sessionID": …, "error": {…}}``). :func:`_unwrap`
normalizes both to ``(event_type, payload)`` so the dispatch table is written
once. (The ``session.next.*``/``properties`` envelopes belong to ``opencode
serve``'s HTTP/SSE surface and never appear here — see the note on the event
constants below.)

Session continuity
------------------
The ``sessionID`` observed on the first event is retained and replayed via
``--session`` on the next ``communicate()`` call, which is what makes multi-turn
(dialog-mode) evaluation work against a stateless CLI invocation.
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
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    OpenCodeAgentConfig,
    PermissionMode,
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

from .registry import AgentRegistry


logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when tearing down the CLI subprocess.
_TERM_GRACE_SECONDS = 5.0

# How long to keep draining stdout/stderr after the CLI process has been reaped.
# `opencode run` leaves a local server child holding the inherited pipes open, so
# EOF never arrives on its own and every post-exit read must be bounded.
_DRAIN_SECONDS = 2.0

# Event type strings emitted by `opencode run --format json`. These are the CLI's
# OWN compact vocabulary, captured from a live run — NOT the `session.next.*`
# names in the server's OpenAPI schema, which describe the HTTP/SSE surface of
# `opencode serve` instead. The two are not interchangeable.
_STEP_START = "step_start"
_STEP_FINISH = "step_finish"
_TEXT = "text"
_TOOL_USE = "tool_use"
_ERROR = "error"

# The full recognized vocabulary. A zero-exit turn that recognized NOTHING from
# this set captured zero telemetry, and is crashed rather than reported as a
# clean empty success — an earlier version of this harness parsed the wrong
# vocabulary and scored SUCCESS 1.0 with zero turns and zero tokens, which is
# indistinguishable from a real pass in every aggregate. See _settle_turn.
_RECOGNIZED_EVENTS = frozenset({_STEP_START, _STEP_FINISH, _TEXT, _TOOL_USE, _ERROR})

# How many distinct unrecognized event-type strings to retain for the crash
# message when the vocabulary check fails (diagnosis, not an exhaustive list).
_MAX_UNRECOGNIZED_TYPES = 8

# OpenCode's native tool names -> the canonical (Claude) vocabulary that every
# criterion is written against. Mirrors codex_agent's _TOOL_ITEM_NAMES: without
# it a `command_executed` criterion with `tool_name: Bash` matches NOTHING on an
# OpenCode run, and the shell-aware `parameters["command"]` extraction in
# criteria/command_executed.py degrades to raw-JSON matching — so the same task
# scores differently per harness. Unknown tools pass through unchanged.
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
}

# Config fields the OpenCode CLI has no equivalent knob for. `experiments/default.yaml`
# sets `allowed_tools` on every task, so these are silently dropped by default —
# warn once at start() rather than letting a task believe it constrained the agent.
_UNSUPPORTED_CONFIG_FIELDS: tuple[str, ...] = (
    "system_prompt",
    "system_prompt_file",
    "allowed_tools",
    "disallowed_tools",
    "plugins",
)

# ToolEndStatus -> CommandTelemetry.result_status (the persisted tri-state).
_RESULT_STATUS: dict[ToolEndStatus, Literal["success", "error", "unknown"]] = {
    ToolEndStatus.OK: "success",
    ToolEndStatus.ERROR: "error",
    ToolEndStatus.PERMISSION_DENIED: "error",
    ToolEndStatus.UNRESOLVED: "unknown",
}


def _unwrap(obj: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalize an OpenCode CLI event to ``(event_type, payload)``.

    Every line is ``{type, timestamp, sessionID, part: {...}}`` with the payload
    under ``part`` — except the CLI's own error line, which is flat
    (``{type: "error", sessionID, error: {...}}``). Returning the top-level dict
    for the flat case is safe: the accessors read named keys, never iterate.
    """
    event_type = str(obj.get("type") or "")
    part = obj.get("part")
    if isinstance(part, dict):
        return event_type, part
    return event_type, obj


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    """Convert OpenCode's epoch-millisecond timestamps to naive local datetimes.

    Naive-local matches what the rest of the telemetry uses (``datetime.now()``),
    so durations computed against these stay consistent.
    """
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000)
    except (OverflowError, OSError, ValueError):
        return None


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
        self.turn_id: str = ""
        self.step_started_at: datetime | None = None
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
        self.turn_id = str(part.get("messageID") or f"step_{self.step_count}")
        self.step_started_at = datetime.now()
        self.step_text_parts = []
        self.step_tool_ids = []
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
        than a call/result pair, so the matching ``ToolStart``/``ToolEnd`` are
        both synthesized here. A non-terminal state (``pending``/``running``) is
        still handled: the tool is left open and closed by a later event for the
        same ``callID``, or force-closed as ``unresolved`` if the turn dies first.
        Execution timestamps come from ``state.time``, so ``duration_ms`` reflects
        the tool's real runtime rather than our parse instant.
        """
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        call_id = str(part.get("callID") or f"call_{self.sequence + 1}")

        telemetry = self.open_tools.get(call_id)
        if telemetry is None:
            self.sequence += 1
            times = state.get("time") if isinstance(state.get("time"), dict) else {}
            started = _epoch_ms_to_dt(times.get("start"))
            params = state.get("input")
            raw_tool = str(part.get("tool") or "unknown")
            telemetry = CommandTelemetry(
                tool_name=_TOOL_NAME_MAP.get(raw_tool.lower(), raw_tool),
                tool_id=call_id,
                assistant_turn_index=self.step_count,
                timestamp=started or datetime.now(),
                execution_started_at=started,
                parameters=params if isinstance(params, dict) else {},
                sequence_number=self.sequence,
            )
            self.open_tools[call_id] = telemetry
            self.step_tool_ids.append(call_id)
            self.emit(
                ToolStartEvent(task_id=self.task_id, thread_id=self.thread_id, turn_id=self.turn_id, tool=telemetry)
            )

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

        times = state.get("time") if isinstance(state.get("time"), dict) else {}
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

        ``None`` when the model is unpinned or unpriced, matching "nothing could
        be priced". See :meth:`_resolve_cost` for how this composes with the
        stream's own ``cost`` reporting.
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

        A non-zero cost the CLI reported always wins — it is the provider's own
        accounting, and (on OpenRouter) per-request routing makes it strictly
        better than a static headline rate. The rate card fills two gaps that
        would otherwise book tokens with no money and silently understate the
        run-level bill:

        - the stream reported no ``cost`` at all (a provider or auth mode that
          omits it, or a turn that died before its first ``step_finish``);
        - the stream reported ``cost: 0`` for tokens the rate card prices above
          zero. OpenCode reports 0 when its own model registry has no price for
          the model, or under subscription-style auth — neither means the tokens
          were free. A genuinely free model has an all-zero rate entry (or no
          entry), so it still resolves to the stream's 0 here.
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

    def _fresh_input_slice(
        self, tokens: dict[str, Any], raw_in: int, raw_out: int, reasoning: int, cw: int, cr: int
    ) -> int:
        """Decide what ``tokens.input`` means on this stream — per step, from evidence.

        coder_eval's ``uncached_input_tokens`` is the fresh slice only (cost bills it
        at the input rate and the cache buckets separately), and two conventions for
        ``input`` exist in the wild:

        - **flat** — ``input`` already IS the fresh slice and
          ``total = input + output + reasoning + cache.read + cache.write``. This is
          what a live capture on the current CLI shows (observed 2026-08-13:
          ``7966 = 6796 + 128 + 18 + 1024`` exactly).
        - **nested** — cached tokens are counted inside ``input`` (the OpenAI
          ``prompt_tokens`` convention), so ``total = input + output + reasoning``
          and the fresh slice subtracts the cache buckets.

        The stream's own ``total`` arbitrates per step, so a CLI upgrade that flips
        the convention re-classifies itself instead of silently mis-booking a bucket.
        With no cache traffic the conventions agree. With no usable ``total`` the
        flat (live-verified) reading is taken — but if cache traffic is present that
        is an UNVERIFIABLE assumption (the original mapping bug was exactly an
        unverified assumption of this kind), so it warns once per turn rather than
        defaulting in silence. A ``total`` matching NEITHER warns loudly — the
        schema moved, and cost should not be trusted blind.
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
                # The stream contradicts itself: `total` says the cache buckets nest
                # inside `input`, but `input` is too small to contain them.
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
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        raw_in = int(tokens.get("input") or 0)
        raw_out = int(tokens.get("output") or 0)
        step_reasoning = int(tokens.get("reasoning") or 0)
        step_cw = int(cache.get("write") or 0)
        step_cr = int(cache.get("read") or 0)

        step_in = self._fresh_input_slice(tokens, raw_in, raw_out, step_reasoning, step_cw, step_cr)
        # Reasoning tokens are billed at the output rate but reported apart from
        # `output`, so fold them in for the turn total; the per-message record
        # keeps `reasoning_tokens` separately for visibility.
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

        started = self.step_started_at or datetime.now()
        completed = datetime.now()
        blocks: list[ContentBlock] = []
        step_text = "".join(self.step_text_parts)
        if step_text:
            blocks.append(ContentBlock(block_type="text", sequence=0, text=step_text))
        for i, tool_id in enumerate(self.step_tool_ids, start=len(blocks)):
            blocks.append(ContentBlock(block_type="tool_use", sequence=i, tool_use_id=tool_id))

        self.messages.append(
            AssistantMessage(
                started_at=started,
                completed_at=completed,
                generation_duration_ms=(completed - started).total_seconds() * 1000,
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
        ``communicate()``, and the outer ``except Exception`` guard can fire after
        a normal finalize (e.g. a failure while building the record). The first
        call wins so a late crash cannot emit a second terminal event into the
        caller's ``stream_callback``; it still raises, so the failure is not
        swallowed.
        """
        if self.finalized:
            return
        self.finalized = True
        self.close_open_tools()
        usage = self.usage
        cost = self._resolve_cost()
        if cost is not None:
            usage = usage.model_copy(update={"total_cost_usd": cost})
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

    # `should_stop` is polled at every event boundary — i.e. tool-call
    # granularity — and honored by terminating the CLI subprocess cleanly.
    supports_cooperative_stop: ClassVar[bool] = True

    def __init__(
        self,
        config: OpenCodeAgentConfig,
        task_id: str = "unknown",
        **_: Any,
    ) -> None:
        self.config = config
        self.task_id = task_id
        self.working_directory: str | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        self._session_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        # Process-group ids (== the CLI's pid under start_new_session) of every
        # invocation this agent spawned, swept on kill()/kill_sync()/stop() —
        # `opencode run` leaves a server child alive after the CLI exits, and
        # signaling only the CLI pid would orphan it (a slow leak across a batch).
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
                os.kill(proc.pid, signal.SIGKILL)
        self._sweep_process_groups()

    def _sweep_process_groups(self) -> None:
        """SIGKILL every process group this agent spawned (POSIX only).

        Each invocation runs in its own session (``start_new_session``), so its
        pgid is the CLI's pid and the group contains ONLY what that invocation
        spawned — the lingering server child included, a shared daemon we did not
        start excluded. The CLI itself gets SIGTERM-then-SIGKILL first (see
        ``kill``); this reaps whatever survives it. Sessions are persisted on
        disk by OpenCode, so killing a turn's server does not lose ``--session``
        continuity.
        """
        if os.name != "posix":
            return
        for pgid in self._spawned_pgids:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        self._spawned_pgids.clear()

    def get_environment_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"opencode_model": self.config.model, "opencode_pure": self.config.pure}
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
        # PLAN mode is the one mode that must not auto-approve side effects; every
        # other mode runs unattended, where an approval prompt would simply hang.
        if self.config.permission_mode is not PermissionMode.PLAN:
            argv.append("--auto")
        if self._session_id:
            argv += ["--session", self._session_id]
        argv.append("--")
        argv.append(user_input)
        return argv

    def _build_env(self) -> dict[str, str]:
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
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_argv(user_input),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=self._build_env(),
                # A single nd-JSON event can carry a whole tool result (a large file
                # read), which blows past StreamReader's default 64 KiB line cap and
                # would raise ValueError mid-stream, killing the read loop.
                limit=STDOUT_LINE_LIMIT_BYTES,
                # Own session/process group, so teardown can killpg the lingering
                # server child without touching anything this invocation didn't
                # spawn. POSIX-only knob; harmless False elsewhere.
                start_new_session=os.name == "posix",
            )
            self._process = proc
            if os.name == "posix":
                self._spawned_pgids.append(proc.pid)
            assert proc.stdout is not None

            # Drain stderr CONCURRENTLY, from the moment the CLI starts. Reading it
            # only after exit (while stdout drives the loop) deadlocks the pair: a
            # child that fills the ~64 KiB stderr pipe blocks on write, stops
            # emitting stdout, and never exits — so the turn hangs to its deadline.
            # docker_runner dodges this by merging stderr into stdout; here that
            # would corrupt the nd-JSON, so it gets its own reader instead.
            if proc.stderr is not None:
                stderr_drain = asyncio.ensure_future(proc.stderr.read())

            # `opencode run` spawns a local server child that INHERITS this stdout
            # pipe, so the pipe is NOT closed when the CLI itself exits — readline()
            # would block until the turn deadline waiting for an EOF that never
            # comes. So race each read against process exit: whichever lands first
            # wins, and once the process is gone a bounded drain collects whatever
            # is still buffered before the loop ends.
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
                        # The process exited with the read still pending. Give the
                        # buffered tail a bounded window, then stop rather than
                        # waiting on the grandchild's open write end.
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
            # failed turn, and `_end_turn_ok` would clear the rollback flag that
            # `discard_pending_turn` needs to un-bump `_iteration`.
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
            # Everything the turn loop does NOT anticipate: a spawn failure
            # (OSError/PermissionError from create_subprocess_exec), a StreamReader
            # ValueError on a line past `limit`, a malformed-payload TypeError in a
            # handler, a pydantic error assembling telemetry. Without this the
            # exception escapes raw and breaks the pending-turn contract three ways:
            # no AgentEndEvent (an unbalanced event tree for every renderer), the
            # captured telemetry dropped instead of parked on `pending_turn`, and
            # `_iteration` left incremented because the orchestrator never reaches
            # `discard_pending_turn`. Same guard, same reasons, as CodexAgent.
            self._crash_turn(state, collector, f"OpenCode turn failed: {e!s}", cause=e)
        finally:
            if stderr_drain is not None:
                stderr_drain.cancel()
            self._process = None

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

        Raises ``AgentCrashError`` (via :meth:`_crash_turn`) when the stream carried
        a structured error, when the process died with neither a structured error
        nor an intentional stop, or when a clean exit recognized no events at all
        (a zero-telemetry turn must not score — see the guard below). Raises
        ``TurnTimeoutError`` when the turn deadline elapses while waiting for the
        exit.
        """
        # Bound the reap: the read loop can end at EOF with the CLI still alive
        # (it closed its stream but never exited), and an unbounded wait here
        # would outlive the turn deadline — the one window where `timeout` was
        # previously unenforced. Give the exit the deadline's remainder, or a
        # short fixed grace when no deadline is configured (post-EOF, a healthy
        # CLI exits almost immediately).
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
        # Collect what the concurrent reader drained. Bounded for the same reason as
        # the read loop: the inherited stderr pipe outlives the CLI, so waiting for
        # the reader's own EOF would block. Shielded so the timeout doesn't kill it
        # before communicate()'s finally can.
        stderr_bytes = b""
        if stderr_drain is not None:
            with contextlib.suppress(TimeoutError):
                stderr_bytes = await asyncio.wait_for(asyncio.shield(stderr_drain), timeout=_DRAIN_SECONDS)

        if state.error_message is not None:
            self._crash_turn(state, collector, f"OpenCode error: {state.error_message}")

        # A non-zero exit with no structured error event still means the turn
        # died — surface stderr rather than reporting a silent empty success.
        if proc.returncode not in (0, None) and not stopped_early and not state.max_turns_exhausted:
            detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
            self._crash_turn(state, collector, f"OpenCode exited non-zero: {detail}")

        # A clean exit that recognized NO events captured zero telemetry — zero
        # turns, zero tokens, zero cost — while file-based criteria can still
        # pass on whatever the agent did, producing a SUCCESS that is silently
        # missing from every aggregate. This already happened once (the harness
        # parsed the `session.next.*` server vocabulary instead of the CLI's),
        # so vocabulary drift is crashed loudly instead of scored. Intentional
        # cuts (should_stop / max_turns) are exempt: they can land before the
        # first event.
        if not stopped_early and not state.max_turns_exhausted and state.recognized_events == 0:
            seen = ", ".join(sorted(state.unrecognized_types)) or "none (stdout carried no JSON events)"
            self._crash_turn(
                state,
                collector,
                "OpenCode exited cleanly but emitted no recognized events, so the turn captured zero "
                + f"telemetry. Unrecognized event types seen: {seen}. The CLI's event vocabulary may have "
                + "changed — see docs/agents/OPENCODE.md (Telemetry) before trusting any run from this CLI version.",
            )

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

        ``cause`` preserves the explicit ``__cause__`` link when called from
        inside an ``except ... as e`` block.
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

        ``_finalize_and_raise_timeout`` emits the terminal event via
        ``state.finalize``; the partial record is captured immediately after so
        ``pending_turn`` carries everything observed before the deadline.
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
            # OpenCode occasionally interleaves non-JSON notices (e.g. the Bun
            # AVX warning) on stdout; a malformed line must not kill the turn.
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
            error = part.get("error")
            if isinstance(error, dict):
                data = error.get("data")
                message = (data or {}).get("message") if isinstance(data, dict) else None
                state.error_message = str(message or error.get("name") or "unknown error")
            else:
                state.error_message = str(error or "unknown error")
        else:
            logger.debug("opencode: unhandled event type %r", event_type)
