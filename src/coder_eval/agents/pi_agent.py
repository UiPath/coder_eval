"""Pi agent implementation (the ``pi`` Node coding agent — https://pi.dev/).

Drives the ``pi`` CLI in JSON print mode::

    pi -p --mode json --no-context-files --no-approve \
        --session-dir <D> --session-id <ID> [--model provider/id] \
        [--thinking L] [--tools ...] [--exclude-tools ...] \
        [--append-system-prompt S] -- <prompt>

which streams **newline-delimited JSON events** on stdout. Each line is one
event; this module reduces that stream into the standardized coder_eval event
protocol (``AgentStart`` / ``TurnStart`` / ``ToolStart`` / ``ToolEnd`` /
``TurnEnd`` / ``AgentEnd``) and lets :class:`EventCollector` build the
``TurnRecord`` — so no telemetry is assembled by hand here. The design mirrors
:mod:`coder_eval.agents.opencode_agent` almost verbatim; the differences are
noted inline.

Event grammar (captured from ``pi`` 0.84.4)
-------------------------------------------
- ``{"type": "session", ...}`` — always line 1 (cwd/id/version).
- ``{"type": "agent_start"}`` — bare; **can appear more than once** per
  invocation (Pi auto-retries a transient/provider error internally).
- ``{"type": "turn_start"}`` — bare; one per agent-loop step. This is the unit
  ``max_turns`` counts.
- ``{"type": "message_update", "assistantMessageEvent": {...}}`` — streaming
  deltas (text/thinking/toolcall). Text deltas drive ``TextChunkEvent``.
- ``{"type": "message_end", "message": {...}}`` — a complete message. Ignored
  for token accounting: ``turn_end`` echoes the same assistant usage once per
  step, and reading both would double-count.
- ``{"type": "tool_execution_start", "toolCallId": ..., "toolName": ...,
  "args": {...}}`` / ``{"type": "tool_execution_end", "toolCallId": ...,
  "result": ..., "isError": ...}``.
- ``{"type": "turn_end", "message": {...assistant...}, "toolResults": [...]}``
  — ``message.usage`` is that step's OWN usage (per-generation), summed across
  steps for the turn total.
- ``{"type": "agent_end", "messages": [...], "willRetry": <bool>}`` —
  ``willRetry: true`` means another retry cycle follows in the SAME invocation.
- ``{"type": "agent_settled"}`` — the true terminal event (after all retries);
  emit the single ``AgentEndEvent`` here / at EOF, NOT on the first
  ``agent_end``.

Token semantics
---------------
Per-generation, **SUM** (identical to OpenCode; NOT cumulative). Each
``turn_end.message.usage`` carries ``{input, output, cacheRead, cacheWrite,
[reasoning], totalTokens, cost:{...,total}}`` for that step, where
``totalTokens == input + output + cacheRead + cacheWrite``. Unlike OpenCode,
Pi's ``input`` IS the fresh slice already (no flat/nested arbitration needed),
so it maps straight to ``uncached_input_tokens``. ``reasoning`` bills at the
output rate. ``cost.total`` per step is summed into
``token_usage.total_cost_usd`` (the rate card is only a fallback when the
stream omits cost).

Session continuity
------------------
A per-agent ``--session-dir`` + stable ``--session-id`` are assigned in
``start()`` and replayed on every ``communicate()`` — create-if-missing on the
first call, resume after — which is what makes multi-turn (dialog-mode)
evaluation work across separate CLI invocations.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar, Literal, NoReturn
from uuid import uuid4

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
    PiAgentConfig,
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

from .registry import AgentRegistry


logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when tearing down the CLI subprocess.
# Doubles as the post-EOF exit grace in _settle_turn when no turn deadline is
# configured. Genuinely OpenCode-module-private (not exported), so re-declared
# here at the same value rather than imported (a shared-module hoist is out of
# scope); STDOUT_LINE_LIMIT_BYTES, which IS canonical, is imported above.
_TERM_GRACE_SECONDS = 5.0

# SIGKILL does not exist on Windows (where the process-group sweep is a no-op
# anyway); resolve it dynamically so the module imports and typechecks on every
# platform, falling back to SIGTERM for the direct-pid kill_sync path.
_SIGKILL: signal.Signals = getattr(signal, "SIGKILL", signal.SIGTERM)

# How long to keep draining stdout/stderr after the CLI process has been reaped.
# A print-mode CLI may leave an inherited pipe open, so every post-exit read
# must be bounded.
_DRAIN_SECONDS = 2.0

# How many distinct unrecognized event-type strings to retain for the crash
# message when the vocabulary check fails (diagnosis, not an exhaustive list).
_MAX_UNRECOGNIZED_TYPES = 8

# pi's native tool names -> the canonical (Claude) vocabulary every criterion is
# written against. Mirrors opencode_agent._TOOL_NAME_MAP: without it a
# `command_executed` with `tool_name: Bash` matches nothing on a Pi run. Unknown
# tools pass through unchanged.
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
    "ls": "LS",
    "webfetch": "WebFetch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "task": "Agent",
}

# pi per-tool INPUT-arg key -> canonical (Claude) key. Mirrors
# opencode_agent._OPENCODE_ARG_RENAME. The spike's write/read tools used `path`,
# so map it to `file_path` for Read/Write/Edit (the search tools keep `path`,
# which is already Claude's key). Keyed by the canonical tool name (post
# _TOOL_NAME_MAP); unlisted keys pass through.
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

# Config fields the Pi CLI has no equivalent knob for (v1). `experiments/default.yaml`
# sets `permission_mode` on every task, so warn once at start() rather than let a
# task believe it constrained the agent. NOTE `allowed_tools`/`disallowed_tools`/
# `system_prompt` are SUPPORTED (mapped to --tools/--exclude-tools/
# --append-system-prompt) and thus deliberately NOT here.
_UNSUPPORTED_CONFIG_FIELDS: tuple[str, ...] = ("plugins", "permission_mode", "system_prompt_file")

# The full recognized Pi vocabulary (from `pi` 0.84.4). A clean exit that
# recognized NOTHING from this set is vocabulary drift and is crashed rather than
# scored as a silent empty success (see _settle_turn).
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

    def __init__(self, *, task_id: str, iteration: int, user_input: str, model: str | None) -> None:
        self.task_id = task_id
        self.iteration = iteration
        self.user_input = user_input
        self.model = model

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
        self.turn_count += 1
        self.turn_open = True
        self.turn_id = f"turn_{self.turn_count}"
        self.turn_started_at = datetime.now()
        self.turn_text_parts = []
        self.turn_tool_ids = []
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
        started = datetime.now()
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
            # Best-effort: Pi does not tag permission denials distinctly, so infer
            # from the result text. The persisted tri-state folds both to "error"
            # (see _RESULT_STATUS), so a misclassified legit "permission denied" in
            # output is cosmetic. Mirrors opencode_agent.
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
                timestamp=datetime.now(),
                sequence_number=self.sequence,
            )
        completed = datetime.now()
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

    def _as_int(self, value: Any) -> int:
        """Coerce one stream-supplied token count; count a non-number as 0.

        A bare ``int()`` would raise on a non-numeric value, which
        ``communicate``'s ``except Exception`` turns into an ``AgentCrashError``
        (categorized ``AGENT_CRASH``, ``max_retries=2``), burning three attempts
        on one mistyped bucket. A bool is never a token count (``int(True) == 1``).
        """
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
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
        usage = message.get("usage")
        usage = usage if isinstance(usage, dict) else {}

        step_in = self._as_int(usage.get("input"))
        raw_out = self._as_int(usage.get("output"))
        step_reasoning = self._as_int(usage.get("reasoning"))
        step_cw = self._as_int(usage.get("cacheWrite"))
        step_cr = self._as_int(usage.get("cacheRead"))
        # Reasoning bills at the output rate but is reported apart from `output`;
        # fold it into the turn total (the per-message record keeps it separately).
        step_out = raw_out + step_reasoning

        self.usage = TokenUsage(
            uncached_input_tokens=self.usage.uncached_input_tokens + step_in,
            output_tokens=self.usage.output_tokens + step_out,
            cache_creation_input_tokens=self.usage.cache_creation_input_tokens + step_cw,
            cache_read_input_tokens=self.usage.cache_read_input_tokens + step_cr,
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

        started = self.turn_started_at or datetime.now()
        completed = datetime.now()
        blocks: list[ContentBlock] = []
        turn_text = "".join(self.turn_text_parts)
        if turn_text:
            blocks.append(ContentBlock(block_type="text", sequence=0, text=turn_text))
        for i, tool_id in enumerate(self.turn_tool_ids, start=len(blocks)):
            blocks.append(ContentBlock(block_type="tool_use", sequence=i, tool_use_id=tool_id))

        self.messages.append(
            AssistantMessage(
                started_at=started,
                completed_at=completed,
                generation_duration_ms=(completed - started).total_seconds() * 1000,
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

        Pi reports a real per-call ``cost.total`` (spike-verified), which always
        wins. The rate card fills only the gap where the stream reported no cost
        at all; a genuinely free model resolves to the stream's 0.
        """
        rate = self._rate_card_cost()
        if not self.saw_cost:
            return rate
        if self.cost_usd == 0.0 and rate:
            logger.warning(
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
        # A turn still open here never received its `turn_end` — close its
        # TurnStartEvent or the one-pair-per-inner-turn contract breaks.
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
            )
        )


@AgentRegistry.register(AgentKind.PI, PiAgentConfig)
class PiAgent(Agent[PiAgentConfig]):
    """Runs the ``pi`` CLI as a subprocess, one invocation per turn."""

    # `should_stop` is polled at every event boundary — i.e. tool-call
    # granularity — and honored by terminating the CLI subprocess cleanly.
    supports_cooperative_stop: ClassVar[bool] = True

    # Pi maps `system_prompt` to `--append-system-prompt`, so it appends to (does
    # not replace) the CLI's default prompt. Declared explicitly (mirrors Codex /
    # Antigravity, NOT OpenCode's `"unknown"`).
    system_prompt_semantics: ClassVar[SystemPromptSemantics] = "append"

    def __init__(
        self,
        config: PiAgentConfig,
        route: ApiRoute | None = None,
        *,
        task_id: str = "unknown",
    ) -> None:
        """Every parameter the agent factory can pass is DECLARED, not absorbed.

        ``create_agent`` calls ``agent_class(config, route=route, **kwargs)``
        through a ``cast(Any, ...)``, so a ``**_`` sink would mean nothing checks
        the kwargs at runtime either — a mis-gated kwarg must be loud here rather
        than silently dropped.

        ``route`` is accepted for factory parity and deliberately unused: the CLI
        owns its own provider configuration. ``task_id`` only labels the emitted
        event stream.
        """
        self.config = config
        self.route = route
        self.task_id = task_id
        self.working_directory: str | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        # Per-agent session, reused across communicate() calls for multi-turn /
        # simulation continuity (assigned in start(), removed in stop()/kill()).
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
        self.working_directory = working_directory
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        # A stable pre-assigned id (create-if-missing on turn 1, resume after).
        # The tempdir lives OUTSIDE the sandbox working dir and staged reference
        # dir, so it never pollutes graded files or trips reference-integrity.
        self._session_id = f"coder-eval-{self.task_id}-{uuid4().hex[:8]}"
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

        Each invocation runs in its own session (``start_new_session``), so its
        pgid is the CLI's pid and the group contains ONLY what that invocation
        spawned — a lingering child included, a shared daemon we did not start
        excluded.
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
        return info

    # --- command construction ---------------------------------------------

    def _build_argv(self, user_input: str) -> list[str]:
        # -p exits after the run; --no-context-files + --no-approve isolate the
        # sandbox from host AGENTS.md/CLAUDE.md and project-local trust (analogue
        # of OpenCode --pure / Claude setting_sources: []). --session-dir +
        # --session-id give cross-communicate() continuity (simulation mode) —
        # reused every call, created on turn 1, resumed after. NOT --no-session
        # (that would defeat continuity). All spike-verified. No --dir flag: the
        # working dir is set via the subprocess `cwd`.
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
        if self.config.allowed_tools:
            argv += ["--tools", ",".join(self.config.allowed_tools)]  # ENFORCED (a win over OpenCode)
        if self.config.disallowed_tools:
            argv += ["--exclude-tools", ",".join(self.config.disallowed_tools)]
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
        Returns the WHOLE environment (seeded from ``os.environ``) so the CLI
        keeps the host's provider credentials (``OPENROUTER_API_KEY``, ...).
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
            )
        )

        deadline = None if timeout is None else time.monotonic() + timeout
        stopped_early = False
        stderr_drain: asyncio.Future[bytes] | None = None
        # Bound OUTSIDE the try so the teardown in `finally` can tell "never
        # spawned" from "spawned and possibly still running".
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_argv(user_input),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=self._build_env(),
                # A single nd-JSON event can carry a whole tool result, which blows
                # past StreamReader's default 64 KiB line cap and would raise
                # ValueError mid-stream, killing the read loop.
                limit=STDOUT_LINE_LIMIT_BYTES,
                # Own session/process group, so teardown can killpg any lingering
                # child without touching anything this invocation didn't spawn.
                start_new_session=os.name == "posix",
            )
            self._process = proc
            if os.name == "posix":
                self._spawned_pgids.append(proc.pid)
            assert proc.stdout is not None

            # Drain stderr CONCURRENTLY: a child that fills the ~64 KiB stderr pipe
            # blocks on write, stops emitting stdout, and never exits — hanging the
            # turn to its deadline. It gets its own reader so the nd-JSON on stdout
            # stays clean.
            if proc.stderr is not None:
                stderr_drain = asyncio.ensure_future(proc.stderr.read())

            # A print-mode CLI may leave an inherited pipe open, so readline() can
            # block on an EOF that never comes. Race each read against process
            # exit; once the process is gone a bounded drain collects the tail.
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
            # failed turn, and `_end_turn_ok` would clear the rollback flag that
            # `discard_pending_turn` needs.
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
            # A spawn failure (OSError), a StreamReader ValueError past `limit`, a
            # malformed-payload error in a handler, a pydantic error assembling
            # telemetry. Funnel to the pending-turn contract like the siblings.
            self._crash_turn(state, collector, f"Pi turn failed: {e!s}", cause=e)
            raise  # unreachable (_crash_turn is NoReturn) — makes the no-fall-through explicit
        finally:
            if stderr_drain is not None:
                stderr_drain.cancel()
            self._reap_orphaned_cli(proc)
            self._process = None

    def _reap_orphaned_cli(self, proc: asyncio.subprocess.Process | None) -> None:
        """Kill a CLI still running as the turn unwinds. No-op otherwise.

        The ``except Exception`` crash and an external cancellation both reach the
        ``finally`` with the child possibly alive — neither passes through the
        graceful ``kill()``. Abandoning it is not merely a leak: ``AgentCrashError``
        is retried, so attempt 2 would spawn a SECOND ``pi`` editing the very files
        the criteria are about to score. Synchronous (no await) so it survives a
        ``CancelledError`` in flight. ``proc`` is ``None`` when the spawn failed.
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

        # A non-zero exit with no intentional cut means the turn died.
        if proc.returncode not in (0, None) and not stopped_early and not state.max_turns_exhausted:
            detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
            self._crash_turn(state, collector, f"Pi exited non-zero: {detail}")

        # A clean exit that recognized NO events is vocabulary drift — the CLI's
        # schema moved, and scoring a silent empty success would be indistinguishable
        # from a real pass in every aggregate. Intentional cuts are exempt (a stop
        # can land before the first event).
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

        Pi may emit multiple ``agent_start``/``turn_*``/``agent_end`` cycles in one
        invocation (auto-retry). ``agent_end`` is NOT terminal — only
        ``agent_settled`` / stdout EOF is — so it is recognized and otherwise
        ignored, and the read loop keeps going.
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
        # `session` is line 1 (cwd/id); `message_start`/`message_end`/`agent_end`/
        # `agent_settled` carry no state we accumulate (usage is read from
        # `turn_end`, not the echoing `message_end`). All are recognized vocabulary.
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
