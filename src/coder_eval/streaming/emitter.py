"""TurnEmitter: the one writer of the event protocol for one ``communicate()`` turn.

An adapter opens one per turn (``Agent._open_emitter``), calls ``begin``, reports what
its harness did through the write methods, and returns ``finalize(...)`` or
``fail(...)``. The emitter owns every per-turn value: open tools, sequence numbers,
the transcript, reported usage, text output, the open inner turn and the end.

Rationale: .claude/notes/agents.md § Shared turn lifecycle
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.errors.agent import truncate_crash_message
from coder_eval.models import (
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    ResultSummary,
    TimingBasis,
    TokenUsage,
    TurnRecord,
)
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
from coder_eval.timing import Window


logger = logging.getLogger(__name__)

_UNSET: Any = object()
_FAILED = (AgentEndStatus.CRASHED, AgentEndStatus.TIMEOUT)
_BUCKETS = ("uncached_input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_RESULT_STATUS: dict[ToolEndStatus, Literal["success", "error", "unknown"]] = {
    ToolEndStatus.OK: "success",
    ToolEndStatus.ERROR: "error",
    ToolEndStatus.PERMISSION_DENIED: "error",
    ToolEndStatus.UNRESOLVED: "unknown",
}


class Clock(Protocol):
    """The source of every stamp the emitter writes."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class Generation:
    """One sub-message of one model generation; ``tokens`` is this part's own delta."""

    blocks: list[ContentBlock]
    tokens: TokenUsage
    reasoning_tokens: int = 0
    stop_reason: str | None = None


@dataclass(frozen=True)
class TurnOutcome:
    """What a ``communicate()`` turn produced.

    ``error`` is the untruncated failure message, set only for ``CRASHED`` and ``TIMEOUT``.
    """

    record: TurnRecord
    status: AgentEndStatus
    error: str | None

    def record_or_raise(
        self, *, timeout_seconds: float | None = None, task_id: str | None = None, iteration: int | None = None
    ) -> TurnRecord:
        """The record, or the exception the orchestrator's retry policy classifies.

        Raises:
            AgentCrashError: the status is ``CRASHED``.
            TurnTimeoutError: the status is ``TIMEOUT``.
        """
        if self.status is AgentEndStatus.CRASHED:
            raise AgentCrashError(self.error or "agent turn crashed")
        if self.status is AgentEndStatus.TIMEOUT:
            raise TurnTimeoutError(timeout_seconds or 0.0, task_id=task_id, iteration=iteration)
        return self.record


@dataclass
class _OpenTool:
    telemetry: CommandTelemetry
    turn_id: str
    parent_tool_id: str | None


class TurnEmitter:
    """The sole writer of the event protocol for one turn.

    Every event goes to an internal ``EventCollector`` first, then to each sink through
    ``safe_emit``, stamped ``clock.now()``. After ``finalize`` or ``fail`` every write
    is dropped. ``RuntimeError`` and ``TypeError`` from a write mean a harness bug.

    Rationale: .claude/notes/agents.md § Shared turn lifecycle
    """

    def __init__(
        self,
        *,
        task_id: str,
        iteration: int,
        prompt: str,
        model: str | None,
        basis: TimingBasis,
        clock: Clock,
        sinks: Sequence[StreamCallback],
    ) -> None:
        self._task_id = task_id
        self._iteration = iteration
        self._prompt = prompt
        self._model = model
        self._basis = basis
        self._clock = clock
        self._sinks = list(sinks)
        self._collector = EventCollector()
        self._began = False
        self._began_at: datetime | None = None
        self._began_monotonic = 0.0
        self._outcome: TurnOutcome | None = None
        self._ending = False
        self._dropped_logged = False
        self._open_tools: dict[str, _OpenTool] = {}
        self._sequence = 0
        self._messages: list[AssistantMessage] = []
        self._text: list[str] = []
        self._turn_id: str | None = None
        self._turn_parent: str | None = None
        self._main_turns = 0
        self._reported = TokenUsage()

    @property
    def iteration(self) -> int:
        return self._iteration

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def inner_turn_open(self) -> bool:
        return self._turn_id is not None

    def now(self) -> datetime:
        return self._clock.now()

    def begin(self) -> None:
        """Emit the ``AgentStartEvent``; once per emitter."""
        if self._began:
            raise RuntimeError("TurnEmitter.begin() called twice")
        self._began = True
        self._began_at = self.now()
        self._began_monotonic = time.monotonic()
        self._emit(
            AgentStartEvent(task_id=self._task_id, prompt=self._prompt, iteration=self._iteration, model=self._model),
            stamp=self._began_at,
        )

    def begin_inner_turn(self, turn_id: str, model: str | None = None, *, parent_tool_id: str | None = None) -> None:
        """Open one inner turn; raises ``RuntimeError`` while another is open."""
        if self._ended():
            return
        if self._turn_id is not None:
            raise RuntimeError(f"inner turn {turn_id!r} begun while {self._turn_id!r} is still open")
        self._turn_id = turn_id
        self._turn_parent = parent_tool_id
        if parent_tool_id is None:
            self._main_turns += 1
            model = model or self._model
        self._emit(TurnStartEvent(task_id=self._task_id, turn_id=turn_id, model=model), parent_tool_id)

    def end_inner_turn(
        self, status: TurnEndStatus = TurnEndStatus.COMPLETED, *, tokens: TokenUsage | None = None
    ) -> None:
        """Close the open inner turn, adding ``tokens`` (a delta) to the reported usage."""
        if self._ended():
            return
        if self._turn_id is None:
            raise RuntimeError("end_inner_turn() with no inner turn open")
        if tokens is not None:
            self._reported += tokens
        turn_id, parent = self._turn_id, self._turn_parent
        self._turn_id = self._turn_parent = None
        self._emit(TurnEndEvent(task_id=self._task_id, turn_id=turn_id, status=status, tokens=tokens), parent)

    def text(self, chunk: str, *, parent_tool_id: str | None = None) -> None:
        """Stream visible assistant text; main-thread chunks form the default ``agent_output``."""
        if self._ended():
            return
        if parent_tool_id is None:
            self._text.append(chunk)
        self._emit(TextChunkEvent(task_id=self._task_id, turn_id=self._turn_id or "", text=chunk), parent_tool_id)

    def open_tool(
        self,
        tool_id: str,
        name: str,
        params: dict[str, Any],
        *,
        parent_tool_id: str | None = None,
        started_at: datetime | None = _UNSET,
        generation_completed: bool = False,
    ) -> None:
        """Record a tool call's start; ``timestamp`` is the execution start, else the clock.

        Raises:
            TypeError: ``started_at`` passed under ``TURN_CLOCK``, or omitted on a
                main-thread tool under ``CLI_EPOCH_MS``.
        """
        if self._ended():
            return
        now = self.now()
        execution_started_at = self._stamp("started_at", started_at, now, parent_tool_id)
        telemetry = CommandTelemetry(
            tool_name=name,
            tool_id=tool_id,
            timestamp=execution_started_at or now,
            parameters=params,
            sequence_number=self._next_sequence(),
            execution_started_at=execution_started_at,
            generation_completed_at=now if generation_completed else None,
        )
        turn_id = self._turn_id or ""
        self._open_tools[tool_id] = _OpenTool(telemetry, turn_id, parent_tool_id)
        self._emit(ToolStartEvent(task_id=self._task_id, turn_id=turn_id, tool=telemetry), parent_tool_id)

    def close_tool(
        self,
        tool_id: str,
        *,
        status: ToolEndStatus,
        summary: str | None = None,
        error: str | None = None,
        result_data: dict[str, Any] | list[Any] | None = None,
        parameters: dict[str, Any] | None = None,
        completed_at: datetime | None = _UNSET,
        started_at: datetime | None = None,
        reported_duration_ms: float | None = None,
    ) -> None:
        """Record a tool call's end; an unknown id synthesizes a ``tool_name="unknown"`` call.

        Only a resolved call is timed: ``UNRESOLVED`` keeps ``execution_started_at``
        and sets no completion stamp and no duration. ``started_at`` is a CLI start
        stamp that arrived only with the result (``CLI_EPOCH_MS``); it fills a call
        opened without one and never replaces an existing start. ``reported_duration_ms``
        is a duration the CLI reported for a call with no stamps; a positive value is
        used only when the stamps measure none.

        Raises:
            TypeError: the same basis rule as ``open_tool``, for ``completed_at``; or
                ``started_at`` / ``reported_duration_ms`` given under ``TURN_CLOCK``.
        """
        if self._ended():
            return
        if self._basis is TimingBasis.TURN_CLOCK and (started_at is not None or reported_duration_ms is not None):
            raise TypeError(
                "started_at= and reported_duration_ms= are not accepted under TimingBasis.TURN_CLOCK: "
                + "the emitter stamps the clock"
            )
        opened = self._open_tools.get(tool_id)
        stamp = self._stamp("completed_at", completed_at, self.now(), opened.parent_tool_id if opened else None)
        if opened is not None and started_at is not None and opened.telemetry.execution_started_at is None:
            opened.telemetry.execution_started_at = started_at
            opened.telemetry.timestamp = started_at
        self._close(tool_id, status, summary, error, result_data, parameters, stamp, reported_duration_ms)

    def add_generation(
        self,
        *,
        message_id: str | None,
        window: Window,
        parts: Sequence[Generation],
        model: str | None = None,
        parent_tool_id: str | None = None,
    ) -> list[AssistantMessage]:
        """Add one measured generation, one ``AssistantMessage`` per part, sharing ``window``.

        ``window.duration_ms`` is apportioned by each part's output tokens (evenly when
        none has output), each share but the last rounded to 1e-6 ms. The returned
        messages and the passed blocks are the live objects in the transcript; after
        the turn ended they are detached and change nothing.

        Raises:
            ValueError: ``parts`` is empty.
        """
        if not parts:
            raise ValueError("add_generation() needs at least one part")
        total_ms = window.duration_ms
        output = sum(part.tokens.output_tokens for part in parts)
        messages: list[AssistantMessage] = []
        assigned = 0.0
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                share = total_ms - assigned
            else:
                share = round(total_ms * (part.tokens.output_tokens / output if output > 0 else 1 / len(parts)), 6)
                assigned += share
            messages.append(
                self._message(part, window.started_at, window.completed_at, share, message_id, model, parent_tool_id)
            )
        if not self._ended():
            self._messages.extend(messages)
        return messages

    def add_unmeasured_generation(
        self,
        *,
        message_id: str | None,
        part: Generation,
        model: str | None = None,
        parent_tool_id: str | None = None,
    ) -> AssistantMessage:
        """Add a generation with no measurable window: equal bounds at ``now()``, no duration."""
        now = self.now()
        message = self._message(part, now, now, None, message_id, model, parent_tool_id)
        if not self._ended():
            self._messages.append(message)
        return message

    def finalize(
        self,
        status: AgentEndStatus,
        *,
        usage: TokenUsage | None = None,
        stop_reason: str | None = None,
        agent_output: str | None = None,
        model_used: str | None = None,
        assistant_turn_count: int | None = None,
        num_turns: int | None = _UNSET,
        result_summary: ResultSummary | None = _UNSET,
    ) -> TurnOutcome:
        """End a clean turn; a second call returns the first outcome and emits nothing.

        ``usage`` defaults to the sum of ``end_inner_turn`` tokens. ``result_summary``
        defaults to the final reply: the text that follows the last tool call in the
        last main-thread message. ``num_turns`` defaults to the main-thread inner turns;
        an explicit ``None`` records that the harness reported none.

        Raises:
            ValueError: ``status`` is ``CRASHED`` or ``TIMEOUT`` (use ``fail``).
        """
        if status in _FAILED:
            raise ValueError(f"finalize({status.value}): a failed turn ends with fail()")
        if self._outcome is not None:
            return self._outcome
        if self._ending:
            raise RuntimeError("the turn already ended, but its record could not be built")
        if result_summary is _UNSET:
            result_summary = ResultSummary(
                is_error=False, subtype=status.value, stop_reason=stop_reason, result=self._final_reply()
            )
        return self._end(
            status,
            reason=None,
            usage=usage,
            agent_output=agent_output,
            model_used=model_used,
            assistant_turn_count=assistant_turn_count,
            num_turns=num_turns,
            result_summary=result_summary,
        )

    def fail(
        self,
        status: Literal[AgentEndStatus.CRASHED, AgentEndStatus.TIMEOUT],
        reason: str,
        *,
        usage: TokenUsage | None = None,
        agent_output: str | None = None,
        model_used: str | None = None,
        assistant_turn_count: int | None = None,
        num_turns: int | None = _UNSET,
    ) -> TurnOutcome:
        """End a failed turn with the full ``reason``; a second call returns the first outcome.

        The payload keywords default as in ``finalize``.

        Raises:
            ValueError: ``status`` is not ``CRASHED`` or ``TIMEOUT``.
        """
        if status not in _FAILED:
            raise ValueError(f"fail({status.value}): a clean turn ends with finalize()")
        if self._outcome is not None:
            return self._outcome
        if self._ending:
            raise RuntimeError("the turn already ended, but its record could not be built")
        return self._end(
            status,
            reason=reason,
            usage=usage,
            agent_output=agent_output,
            model_used=model_used,
            assistant_turn_count=assistant_turn_count,
            num_turns=num_turns,
            result_summary=None,
        )

    def _ended(self) -> bool:
        if self._outcome is None and not self._ending:
            return False
        if not self._dropped_logged:
            self._dropped_logged = True
            logger.debug("[%s] a write after the turn ended was dropped", self._task_id)
        return True

    def _emit(self, event: StreamEvent, parent_tool_id: str | None = None, *, stamp: datetime | None = None) -> None:
        event.timestamp = stamp if stamp is not None else self.now()
        event.thread_id = event.parent_thread_id = parent_tool_id
        self._collector.on_event(event)
        for sink in self._sinks:
            safe_emit(sink, event)

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence

    def _stamp(
        self, keyword: str, value: datetime | None, now: datetime, parent_tool_id: str | None
    ) -> datetime | None:
        if parent_tool_id is not None:
            if value is not _UNSET:
                return value
            return now if self._basis is TimingBasis.TURN_CLOCK else None
        if self._basis is TimingBasis.TURN_CLOCK:
            if value is not _UNSET:
                raise TypeError(
                    f"{keyword}= is not accepted under TimingBasis.TURN_CLOCK: the emitter stamps the clock"
                )
            return now
        if value is _UNSET:
            raise TypeError(f"{keyword}= is required under TimingBasis.{self._basis.name} (None means no CLI stamp)")
        return value

    def _close(
        self,
        tool_id: str,
        status: ToolEndStatus,
        summary: str | None,
        error: str | None,
        result_data: dict[str, Any] | list[Any] | None,
        parameters: dict[str, Any] | None,
        stamp: datetime | None,
        reported_duration_ms: float | None = None,
    ) -> None:
        now = self.now()
        opened = self._open_tools.pop(tool_id, None)
        if opened is None:
            opened = _OpenTool(
                CommandTelemetry(
                    tool_name="unknown", tool_id=tool_id, timestamp=now, sequence_number=self._next_sequence()
                ),
                self._turn_id or "",
                None,
            )
        telemetry = opened.telemetry
        if status is not ToolEndStatus.UNRESOLVED and stamp is not None:
            telemetry.execution_completed_at = stamp
            if telemetry.execution_started_at is not None:
                telemetry.duration_ms = max(0.0, (stamp - telemetry.execution_started_at).total_seconds() * 1000)
        if (
            status is not ToolEndStatus.UNRESOLVED
            and telemetry.duration_ms is None
            and reported_duration_ms is not None
            and reported_duration_ms > 0
        ):
            telemetry.duration_ms = reported_duration_ms
        telemetry.result_status = _RESULT_STATUS[status]
        telemetry.result_summary = summary
        telemetry.error_message = error
        telemetry.result_data = result_data
        if parameters is not None:
            telemetry.parameters = parameters
        self._emit(
            ToolEndEvent(task_id=self._task_id, turn_id=opened.turn_id, tool=telemetry, status=status),
            opened.parent_tool_id,
        )

    def _message(
        self,
        part: Generation,
        started_at: datetime,
        completed_at: datetime,
        duration_ms: float | None,
        message_id: str | None,
        model: str | None,
        parent_tool_id: str | None,
    ) -> AssistantMessage:
        tokens = part.tokens
        return AssistantMessage(
            started_at=started_at,
            completed_at=completed_at,
            generation_duration_ms=duration_ms,
            content_blocks=part.blocks,
            tool_use_ids=[b.tool_use_id for b in part.blocks if b.block_type == "tool_use" and b.tool_use_id],
            input_tokens=tokens.uncached_input_tokens,
            output_tokens=tokens.output_tokens,
            cache_creation_tokens=tokens.cache_creation_input_tokens,
            cache_read_tokens=tokens.cache_read_input_tokens,
            reasoning_tokens=part.reasoning_tokens,
            stop_reason=part.stop_reason,
            model=model or self._model,
            message_id=message_id,
            parent_tool_use_id=parent_tool_id,
        )

    def _final_reply(self) -> str | None:
        main = [m for m in self._messages if m.parent_tool_use_id is None]
        if not main:
            return None
        blocks = main[-1].content_blocks
        last_tool = max((i for i, b in enumerate(blocks) if b.block_type == "tool_use"), default=-1)
        return "".join(b.text or "" for b in blocks[last_tool + 1 :] if b.block_type == "text") or None

    def _end(
        self,
        status: AgentEndStatus,
        *,
        reason: str | None,
        usage: TokenUsage | None,
        agent_output: str | None,
        model_used: str | None,
        assistant_turn_count: int | None,
        num_turns: int | None,
        result_summary: ResultSummary | None,
    ) -> TurnOutcome:
        for tool_id in list(self._open_tools):
            self._close(tool_id, ToolEndStatus.UNRESOLVED, None, None, None, None, None)
        if self._turn_id is not None:
            self.end_inner_turn(TurnEndStatus(status.value))
        published = usage if usage is not None else self._reported
        self._warn_on_delta_overshoot(published)
        crashed = status in _FAILED
        ended_at = self.now()
        self._emit(
            AgentEndEvent(
                task_id=self._task_id,
                status=status,
                usage=published,
                iteration=self._iteration,
                user_input=self._prompt,
                agent_output=agent_output if agent_output is not None else "".join(self._text),
                model_used=model_used if model_used is not None else self._model,
                assistant_turn_count=assistant_turn_count if assistant_turn_count is not None else self._main_turns,
                messages=list(self._messages),
                num_turns=self._main_turns if num_turns is _UNSET else num_turns,
                result_summary=result_summary,
                crashed=crashed,
                crash_reason=truncate_crash_message(reason) if reason is not None else None,
                duration_seconds=self._duration_seconds(ended_at),
            ),
            stamp=ended_at,
        )
        self._ending = True
        self._outcome = TurnOutcome(record=self._collector.build_turn_record(), status=status, error=reason)
        return self._outcome

    def _duration_seconds(self, ended_at: datetime) -> float:
        """The bracket's own span; monotonic when the clock is the wall clock, which can step."""
        if self._began_at is None:
            return 0.0
        if self._basis is TimingBasis.TURN_CLOCK:
            return (ended_at - self._began_at).total_seconds()
        return time.monotonic() - self._began_monotonic

    def _warn_on_delta_overshoot(self, published: TokenUsage) -> None:
        over = [
            f"{bucket} {getattr(self._reported, bucket)} > {getattr(published, bucket)}"
            for bucket in _BUCKETS
            if getattr(self._reported, bucket) > getattr(published, bucket)
        ]
        if over:
            logger.warning(
                "[%s] the inner-turn token deltas exceed the published turn usage (%s); a harness double-counts",
                self._task_id,
                "; ".join(over),
            )
