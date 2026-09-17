"""``SubprocessJsonlAgent``: one CLI invocation per turn, its stdout read as nd-JSON events.

The base owns the transport: the spawn, the concurrent stderr drain, the read loop
racing each line against exit and the turn deadline, the cooperative stop, the
settle that decides how the turn ended, and every reap. A subclass supplies the
argv, the environment and a ``JsonlDecoder`` that turns one event into
``TurnEmitter`` calls.

Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

from coder_eval.agent import Agent
from coder_eval.errors.agent import format_timeout_reason
from coder_eval.isolation.docker_runner import STDOUT_LINE_LIMIT_BYTES
from coder_eval.models import AgentState, ApiRoute, BaseAgentConfig
from coder_eval.streaming.callbacks import StreamCallback
from coder_eval.streaming.emitter import TurnEmitter, TurnOutcome
from coder_eval.streaming.events import AgentEndStatus, StopReason, end_status_for


logger = logging.getLogger(__name__)

# Grace between SIGTERM and SIGKILL, and the post-EOF exit grace when no deadline is set.
_TERM_GRACE_SECONDS = 5.0

# SIGKILL does not exist on Windows (where the process-group sweep is a no-op).
_SIGKILL: signal.Signals = getattr(signal, "SIGKILL", signal.SIGTERM)

# How long to keep reading after the CLI exited: a child can hold the pipes open.
_DRAIN_SECONDS = 2.0

# How many distinct unrecognized event types the drift crash names.
_MAX_UNRECOGNIZED_TYPES = 8


class JsonlDecoder(ABC):
    """One turn's reducer: one decoded nd-JSON event in, ``TurnEmitter`` calls out.

    ``error`` is a terminal CLI or provider error the stream reported; the base
    crashes the turn on it. When ``error_survives_stop`` is False, a requested stop
    wins: the stream can clear its error later, so a cut may land on a stale one.
    """

    error_survives_stop: ClassVar[bool] = False

    def __init__(self, emitter: TurnEmitter) -> None:
        self.emitter = emitter
        self.error: str | None = None

    @abstractmethod
    def __call__(self, event: dict[str, Any]) -> None:
        """Reduce one event; never raises on unexpected payload shapes."""

    @abstractmethod
    def end(self, status: AgentEndStatus, *, reason: str | None = None) -> TurnOutcome:
        """End the turn: ``emitter.fail(status, reason)`` for CRASHED / TIMEOUT, else ``emitter.finalize``."""


class SubprocessJsonlAgent[ConfigT: BaseAgentConfig](Agent[ConfigT]):
    """An adapter whose turn is one CLI process streaming nd-JSON on stdout."""

    cli_name: ClassVar[str]
    docs_page: ClassVar[str]
    recognized_events: ClassVar[frozenset[str]]
    decoder: ClassVar[type[JsonlDecoder]]

    def __init__(
        self,
        config: ConfigT,
        route: ApiRoute | None = None,
        *,
        task_id: str = "unknown",
        cost_log_tags: dict[str, str] | None = None,
    ) -> None:
        super().__init__(config, route, cost_log_tags=cost_log_tags)
        self.task_id = task_id
        self.working_directory: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        # Process-group ids of every invocation this agent spawned, swept on
        # kill()/kill_sync()/stop().
        self._spawned_pgids: list[int] = []
        self._state = AgentState.WORKING

    @abstractmethod
    def argv(self, prompt: str) -> list[str]:
        """The CLI command line for one turn; ``prompt`` is a distinct argv element."""

    @abstractmethod
    def env(self) -> dict[str, str]:
        """The CLI's whole environment."""

    def observe(self, event: dict[str, Any]) -> None:
        """See every decoded event before the decoder does (session ids, for example)."""
        return None

    def clean_exit_problem(self, decoder: JsonlDecoder) -> str | None:
        """Why a clean, uncut exit that recognized events must still crash, or None."""
        return None

    async def communicate(
        self,
        user_input: str,
        *,
        iteration: int,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        should_stop: Callable[[], StopReason | None] | None = None,
    ) -> TurnOutcome:
        """Run one CLI invocation as one turn; see ``Agent.communicate``."""
        if self.working_directory is None:
            raise RuntimeError(f"{type(self).__name__}.start() must be called before communicate()")

        emitter = self._open_emitter(
            prompt=user_input,
            iteration=iteration,
            model=self.config.model,
            task_id=self.task_id,
            stream_callback=stream_callback,
        )
        emitter.begin()
        decoder = self.decoder(emitter)
        vocabulary = _Vocabulary()
        # Deadlines stay on `time.monotonic()`: a deadline must not move when the wall clock steps.
        deadline = None if timeout is None else time.monotonic() + timeout
        requested_stop: StopReason | None = None
        stderr_drain: asyncio.Future[bytes] | None = None
        # Bound OUTSIDE the try so `finally` can tell "never spawned" from "spawned".
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(user_input),
                # A CLI that reads a non-TTY stdin to EOF stalls on an inherited open one.
                # Rationale: .claude/notes/agents.md § Why a CLI never inherits stdin
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=self.env(),
                # One nd-JSON event can carry a whole tool result, past the 64 KiB default.
                limit=STDOUT_LINE_LIMIT_BYTES,
                # Own process group, so teardown can killpg a lingering child.
                start_new_session=os.name == "posix",
            )
            self._process = proc
            if os.name == "posix":
                self._spawned_pgids.append(proc.pid)
            assert proc.stdout is not None
            # Drained CONCURRENTLY, or a child that fills the pipe hangs the turn.
            if proc.stderr is not None:
                stderr_drain = asyncio.ensure_future(proc.stderr.read())

            exit_waiter = asyncio.ensure_future(proc.wait())
            read_task: asyncio.Future[bytes] | None = None
            try:
                while True:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return await self._time_out(decoder, timeout or 0.0)
                    if read_task is None:
                        read_task = asyncio.ensure_future(proc.stdout.readline())
                    done, _pending = await asyncio.wait(
                        {read_task, exit_waiter}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                    )
                    if not done:
                        return await self._time_out(decoder, timeout or 0.0)
                    if not read_task.done():
                        # Exited with the read pending: bound the tail, a child may hold the pipe.
                        try:
                            await asyncio.wait_for(asyncio.shield(read_task), _DRAIN_SECONDS)
                        except TimeoutError:
                            break
                    line = read_task.result()
                    read_task = None
                    if not line:
                        break
                    self._handle_line(line, decoder, vocabulary)
                    requested_stop = should_stop() if should_stop is not None else None
                    if requested_stop is not None:
                        await self.kill()
                        break
            finally:
                if read_task is not None:
                    read_task.cancel()
                exit_waiter.cancel()

            return await self._settle(
                proc,
                decoder,
                vocabulary,
                stderr_drain,
                requested_stop=requested_stop,
                deadline=deadline,
                timeout=timeout,
            )
        except asyncio.CancelledError:
            self._state = AgentState.ERROR
            decoder.end(AgentEndStatus.CRASHED, reason="turn cancelled")
            raise
        except Exception as e:
            # A spawn failure, a StreamReader ValueError past `limit`, a malformed payload.
            logger.warning("%s: turn failed", self.cli_name.lower(), exc_info=True)
            return self._crash(decoder, f"{self.cli_name} turn failed: {e!s}")
        finally:
            if stderr_drain is not None:
                stderr_drain.cancel()
            self._reap(proc)
            self._process = None

    async def _settle(
        self,
        proc: asyncio.subprocess.Process,
        decoder: JsonlDecoder,
        vocabulary: _Vocabulary,
        stderr_drain: asyncio.Future[bytes] | None,
        *,
        requested_stop: StopReason | None,
        deadline: float | None,
        timeout: float | None,
    ) -> TurnOutcome:
        """Reap the CLI once the read loop is done and end the turn.

        In order: no exit by the deadline is TIMEOUT (without one, CRASHED after the
        grace); a stream error is CRASHED (unless a stop was requested and the decoder's
        error does not survive one); a requested stop ends with its status; a non-zero
        exit, no recognized event, or a subclass's ``clean_exit_problem`` is CRASHED;
        else COMPLETED.

        Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
        """
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS if remaining is None else remaining)
        except TimeoutError:
            if remaining is not None:
                return await self._time_out(decoder, timeout or 0.0)
            await self.kill()
            return self._crash(
                decoder, f"{self.cli_name} closed its event stream but did not exit within {_TERM_GRACE_SECONDS:.0f}s"
            )
        stderr_bytes = b""
        if stderr_drain is not None:
            with contextlib.suppress(TimeoutError):
                stderr_bytes = await asyncio.wait_for(asyncio.shield(stderr_drain), timeout=_DRAIN_SECONDS)

        if decoder.error is not None and (requested_stop is None or decoder.error_survives_stop):
            return self._crash(decoder, f"{self.cli_name} error: {decoder.error}")
        if requested_stop is not None:
            return decoder.end(end_status_for(requested_stop))
        if proc.returncode not in (0, None):
            detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
            return self._crash(decoder, f"{self.cli_name} exited non-zero: {detail}")
        if vocabulary.recognized == 0:
            seen = ", ".join(sorted(vocabulary.unrecognized)) or "none (stdout carried no JSON events)"
            return self._crash(
                decoder,
                f"{self.cli_name} exited cleanly but the turn captured no recognized events. Unrecognized event "
                + f"types seen: {seen}. The CLI's event schema may have changed — see {self.docs_page} before "
                + "trusting any run from this CLI version.",
            )
        problem = self.clean_exit_problem(decoder)
        if problem is not None:
            return self._crash(decoder, problem)
        return decoder.end(AgentEndStatus.COMPLETED)

    def _crash(self, decoder: JsonlDecoder, message: str) -> TurnOutcome:
        self._state = AgentState.ERROR
        return decoder.end(AgentEndStatus.CRASHED, reason=message)

    async def _time_out(self, decoder: JsonlDecoder, timeout: float) -> TurnOutcome:
        await self.kill()
        self._state = AgentState.ERROR
        return decoder.end(AgentEndStatus.TIMEOUT, reason=format_timeout_reason(timeout))

    def _handle_line(self, line: bytes, decoder: JsonlDecoder, vocabulary: _Vocabulary) -> None:
        """Parse one line and hand a JSON object to ``observe`` and the decoder; skip anything else."""
        raw = line.decode("utf-8", "replace").strip()
        if not raw:
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("%s: skipping non-JSON stdout line: %s", self.cli_name.lower(), raw[:200])
            return
        if not isinstance(event, dict):
            return
        event_type = str(event.get("type") or "")
        if event_type in self.recognized_events:
            vocabulary.recognized += 1
        elif len(vocabulary.unrecognized) < _MAX_UNRECOGNIZED_TYPES:
            vocabulary.unrecognized.add(event_type or "<missing type>")
        self.observe(event)
        decoder(event)

    async def kill(self) -> None:
        """SIGTERM the in-flight CLI, SIGKILL it after the grace, then sweep its process groups."""
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
        """SIGKILL the in-flight CLI and its process groups (watchdog thread; must not await)."""
        proc = self._process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(proc.pid, _SIGKILL)
        self._sweep_process_groups()

    def _sweep_process_groups(self) -> None:
        """SIGKILL every process group this agent spawned (POSIX only); each holds one invocation's children."""
        if os.name != "posix":
            return
        for pgid in self._spawned_pgids:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, _SIGKILL)
        self._spawned_pgids.clear()

    def _reap(self, proc: asyncio.subprocess.Process | None) -> None:
        """Kill a CLI still running as the turn unwinds; synchronous, so it survives a cancel."""
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.kill()
        self._sweep_process_groups()


class _Vocabulary:
    """The drift check's evidence: how many events matched, and a sample of the types that did not."""

    def __init__(self) -> None:
        self.recognized = 0
        self.unrecognized: set[str] = set()
