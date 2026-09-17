"""Abstract base class for coding agents."""

# by-design model-hub ↔ registry type-level cycle; runtime imports are lazy per CE017
# pyright: reportImportCycles=false

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, NoReturn, Protocol

from .errors import AgentCrashError, TurnTimeoutError
from .errors.agent import format_timeout_reason, truncate_crash_message
from .models import AgentState as AgentState
from .models import ApiRoute, BaseAgentConfig, HarnessContract, TimingBasis, ToolNameMap, TurnRecord
from .streaming.callbacks import CompositeStreamCallback, StreamCallback
from .streaming.collector import EventCollector
from .streaming.emitter import Clock, TurnEmitter, TurnOutcome
from .streaming.events import AgentEndEvent, AgentEndStatus, StopReason, StreamEvent
from .timing import TurnClock


logger = logging.getLogger(__name__)


class _FinalizeFn(Protocol):
    """The per-turn ``finalize`` callback shared by every agent's turn-state.

    Pinning the exact keyword-only signature here (instead of a loose
    ``Callable[..., None]``) lets pyright catch a future ``Agent`` subclass that
    wires an incompatible ``finalize`` into the shared mid-turn failure kernels.
    """

    def __call__(
        self,
        status: AgentEndStatus,
        *,
        crashed: bool = ...,
        crash_reason: str | None = ...,
    ) -> None:
        """Finalize the current turn with the given end status."""


class Agent[ConfigT: BaseAgentConfig](ABC):
    """Abstract base class for all coding agent implementations.

    Generic over ConfigT (the agent's config type) to enforce type-safe
    configuration binding at the agent level. Concrete implementations
    specify their config type:

        class ClaudeCodeAgent(Agent[ClaudeCodeAgentConfig]):
            def __init__(self, config: ClaudeCodeAgentConfig, ...):
                ...

    This ensures mypy enforces the correct config type for each agent.
    """

    pending_turn: TurnRecord | None = None
    """A not-yet-ported adapter parks its crashed partial record here before raising;
    ``_legacy_outcome`` reads and clears it. Always None once ``communicate()`` returns.
    """

    # Class-level defaults for the not-yet-ported adapters' turn bookkeeping.
    _state: AgentState = AgentState.WORKING
    _iteration: int = 0
    _iteration_was_incremented: bool = False

    # Which uniform config fields this harness honors. No default: registration
    # rejects a class that does not declare one.
    # Rationale: .claude/notes/agents.md § The uniform fields, per harness
    contract: ClassVar[HarnessContract]

    # Canonical tool name -> native tools. Registration requires one exactly when the
    # contract enforces allowed_tools or disallowed_tools.
    tool_names: ClassVar[ToolNameMap | None] = None

    def __init__(
        self, config: ConfigT, route: ApiRoute | None = None, *, cost_log_tags: dict[str, str] | None = None
    ) -> None:
        """Bind the resolved config and route.

        Args:
            config: The agent's resolved config.
            route: API routing; harnesses that own their provider config ignore it.
            cost_log_tags: LiteLLM-only correlation headers the factory forwards on
                every LiteLLM route. An agent that cannot stamp them keeps them unused.
        """
        self.config = config
        self.route = route
        self.cost_log_tags = cost_log_tags

    def _begin_turn(self) -> None:
        """Mark the start of a ``communicate()`` turn: reset the pending slot and
        bump the iteration counter so a mid-turn failure can be rolled back.

        Call once at the top of every ``communicate()`` implementation.
        """
        self.pending_turn = None
        self._iteration += 1
        self._iteration_was_incremented = True

    def _end_turn_ok(self) -> None:
        """Mark a turn as cleanly completed so its iteration bump stands.

        Call on the success path of ``communicate()`` (before returning).
        """
        self._iteration_was_incremented = False

    def _mark_stopped(self) -> None:
        """Common ``stop()`` tail: clear the pending slot and enter FINISHED.

        Subclasses call this after their own resource teardown.
        """
        self.pending_turn = None
        self._state = AgentState.FINISHED

    # --- Shared mid-turn failure kernels --------------------------------------
    #
    # Each agent keeps its OWN outer try/except/finally bracket -- the brackets
    # genuinely differ -- and calls these from inside its existing branches. They
    # take the agent's own ``finalize`` callable, so the helper never needs to know
    # how each agent assembles its end-event payload.

    def _finalize_and_raise_timeout(
        self, finalize: _FinalizeFn, timeout: float, *, cause: BaseException | None = None
    ) -> NoReturn:
        """Mark ERROR, finalize the turn as a timed-out crash, raise TurnTimeoutError.

        Reproduces the per-branch ``_state=ERROR -> finalize(TIMEOUT) -> raise`` triple
        that appears three times in Claude plus once in Codex. When called from inside
        an ``except ... as e`` block, pass ``cause=e`` to preserve the explicit
        ``__cause__`` link; otherwise Python's implicit ``__context__`` chaining stands.
        """
        self._state = AgentState.ERROR
        finalize(AgentEndStatus.TIMEOUT, crashed=True, crash_reason=format_timeout_reason(timeout))
        if cause is not None:
            raise TurnTimeoutError(timeout, iteration=self._iteration) from cause
        raise TurnTimeoutError(timeout, iteration=self._iteration)

    def _finalize_and_raise_crash(
        self, finalize: _FinalizeFn, message: str, *, cause: BaseException | None = None
    ) -> NoReturn:
        """Mark ERROR, finalize the turn as a crash, raise AgentCrashError.

        ``message`` is the agent-built error string (the helper does NOT construct
        it). ``crash_reason`` is truncated for storage while the raised
        ``AgentCrashError`` carries ``message`` as passed (truncation is idempotent,
        so an already-truncated message round-trips unchanged). When called from
        inside an ``except ... as e`` block, pass ``cause=e`` to preserve the explicit
        ``__cause__`` link; otherwise Python's implicit ``__context__`` chaining stands.
        """
        self._state = AgentState.ERROR
        finalize(AgentEndStatus.CRASHED, crashed=True, crash_reason=truncate_crash_message(message))
        if cause is not None:
            raise AgentCrashError(message) from cause
        raise AgentCrashError(message)

    def _finalize_external_cancel(self, finalize: _FinalizeFn) -> None:
        """Finalize a turn cancelled from outside (the task watchdog) as a crash. Does NOT raise.

        Only the ``crashed`` branch parks the record on ``pending_turn``; finalizing
        as ``COMPLETED`` drops it, and the unwinding frame takes the return value
        with it, so a killed turn's telemetry survives only via this path. The caller
        re-raises the ``CancelledError`` afterwards.
        """
        self._state = AgentState.ERROR
        finalize(AgentEndStatus.CRASHED, crashed=True, crash_reason="turn cancelled")

    def _capture_partial_turn(self, collector: EventCollector) -> None:
        """Build the crashed partial ``TurnRecord`` into ``pending_turn`` (best-effort).

        Shared crash-tail of each agent's ``finalize``: if assembling the partial
        record itself raises, swallow it and leave ``pending_turn`` None rather than
        masking the original mid-turn failure.
        """
        try:
            self.pending_turn = collector.build_turn_record()
        except Exception:
            logger.exception("Failed to build partial turn record")
            self.pending_turn = None

    @abstractmethod
    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        plugin_root: Path | None = None,
    ) -> None:
        """Initialize and start the agent.

        Args:
            working_directory: Path to the working directory for the agent
            env_path_prepend: Optional absolute directories to prepend to PATH for any
                subprocess the agent spawns (typically resolved sandbox mock dirs).
                Implementations that don't shell out may ignore this argument.
            plugin_tools_dir: Optional canonical ``node_modules/@uipath`` to export as
                ``PLUGIN_TOOLS_DIR`` so the agent's UiPath CLI pins plugin discovery
                instead of walking up from CWD. An external ``PLUGIN_TOOLS_DIR`` in
                the process environment still wins. Implementations that don't shell
                out may ignore this argument.
            plugin_root: The staged canonical plugin root (``<root>/skills/<name>/SKILL.md``),
                or None when the task sets no plugins. Deliver it the harness's native way.
        """
        pass

    @abstractmethod
    async def communicate(
        self,
        user_input: str,
        *,
        iteration: int,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        should_stop: Callable[[], StopReason | None] | None = None,
    ) -> TurnOutcome:
        """Run one turn and return its outcome; a crash or timeout is an outcome, not an exception.

        Args:
            user_input: The message/prompt to send to the agent
            iteration: The caller's turn number, stamped on the record; a retry of
                the same turn passes the same number.
            stream_callback: Optional callback for real-time event streaming
            timeout: Hard wall-clock deadline in seconds. When exceeded the agent
                force-terminates any in-flight subprocess and returns a ``TIMEOUT``
                outcome. Do not rely solely on asyncio cancellation -- some SDKs
                swallow it.
            should_stop: The run's single stop poll. An implementation with
                ``contract.cooperative_stop`` calls it at each safe boundary; a
                non-None reason means stop pulling work and finalize with
                ``end_status_for(reason)``. Agents that do not support it ignore it.

        Returns:
            The ``TurnOutcome`` from the turn's ``TurnEmitter``: ``finalize(...)`` for a
            clean status, ``fail(...)`` for ``CRASHED`` / ``TIMEOUT`` (its record is
            ``crashed=True``).

        Raises:
            asyncio.CancelledError: the turn was cancelled from outside. The agent
                ends the turn first with ``fail(CRASHED, "turn cancelled")``, then
                re-raises. Any other exception is a harness bug.

        Open one ``TurnEmitter`` per turn with ``_open_emitter``; it is the sole writer
        of the event protocol.

        Rationale: .claude/notes/agents.md § Shared turn lifecycle
        """
        pass

    def _open_emitter(
        self,
        *,
        prompt: str,
        iteration: int,
        model: str | None,
        task_id: str,
        stream_callback: StreamCallback | None,
    ) -> TurnEmitter:
        """The turn's emitter, on a fresh ``TurnClock`` or the wall clock per ``contract.timing_basis``."""
        clock: Clock = TurnClock() if self.contract.timing_basis is TimingBasis.TURN_CLOCK else datetime
        return TurnEmitter(
            task_id=task_id,
            iteration=iteration,
            prompt=prompt,
            model=model,
            basis=self.contract.timing_basis,
            clock=clock,
            sinks=[stream_callback] if stream_callback is not None else [],
        )

    async def _legacy_outcome(
        self,
        body: Callable[..., Awaitable[TurnRecord]],
        user_input: str,
        *,
        iteration: int,
        stream_callback: StreamCallback | None,
        timeout: float | None,
        should_stop: Callable[[], StopReason | None] | None,
    ) -> TurnOutcome:
        """Adapt a not-yet-ported raise-and-park ``communicate`` body to the outcome contract.

        ``CancelledError`` propagates untouched: the body already finalized the turn.
        """
        self._iteration = iteration - 1
        last = _LastEndStatus()
        callback = CompositeStreamCallback([last, stream_callback] if stream_callback is not None else [last])
        try:
            record = await body(user_input, stream_callback=callback, timeout=timeout, should_stop=should_stop)
        except (AgentCrashError, TurnTimeoutError) as err:
            fallback = AgentEndStatus.TIMEOUT if isinstance(err, TurnTimeoutError) else AgentEndStatus.CRASHED
            partial = self.pending_turn or TurnRecord(
                iteration=iteration,
                user_input=user_input,
                agent_output="",
                crashed=True,
                crash_reason=truncate_crash_message(str(err)),
            )
            self.pending_turn = None
            self._iteration_was_incremented = False
            failed = fallback
            if last.status is AgentEndStatus.CRASHED or last.status is AgentEndStatus.TIMEOUT:
                failed = last.status
            return TurnOutcome(record=partial, status=failed, error=str(err))
        return TurnOutcome(record=record, status=last.status or AgentEndStatus.COMPLETED, error=None)

    @abstractmethod
    async def stop(self) -> None:
        """Stop the agent and clean up resources."""
        pass

    async def kill(self) -> None:
        """Force-terminate any in-flight subprocess started by this agent.

        Safe to call at any time, including when no subprocess is active.
        Used by the orchestrator to escape SDKs that ignore cooperative
        cancellation. Default implementation is a no-op.
        """
        return None

    async def discard_pending_turn(self) -> None:
        """Clear ``pending_turn`` and roll back the iteration counter.

        Rolls back when either signal says a turn was attempted: the
        ``_iteration_was_incremented`` flag (survives partial-record assembly
        swallowing an exception, which leaves ``pending_turn=None`` — so the
        flag, not ``pending_turn``, is the reliable signal) or a non-None
        ``pending_turn`` (for callers, e.g. tests, that set it directly).

        Idempotent: after the first call both signals are cleared. Call only
        after a failed ``communicate()``; never after a success.
        """
        should_rollback = self._iteration_was_incremented or self.pending_turn is not None
        self.pending_turn = None
        self._iteration_was_incremented = False
        if should_rollback and self._iteration > 0:
            self._iteration -= 1

    def kill_sync(self) -> None:
        """Synchronous variant of ``kill`` for callers on non-asyncio threads.

        Invoked by ``ThreadedWatchdog`` from its timer thread, which cannot
        await coroutines. Safe to call at any time. Default implementation
        is a no-op; concrete agents override to SIGKILL any in-flight
        subprocess by PID.
        """
        return None

    def get_state(self) -> AgentState:
        """Get the current state of the agent.

        Returns:
            Current agent state
        """
        return self._state

    def get_sdk_options(self) -> dict[str, Any] | None:
        """Get the raw SDK options used for the last agent query.

        Returns:
            Dictionary of SDK option field names to values, or None if not available.
        """
        return None

    def get_environment_info(self) -> dict[str, Any]:
        """Agent-specific routing/environment details to persist into the run's
        ``EvaluationResult.environment_info``.

        Lets an agent surface non-default endpoint/model routing (e.g. a custom
        base URL or wire protocol) so runs are auditable and comparable across
        operators. The orchestrator merges this into ``environment_info`` after
        the agent starts.

        The base emits ``system_prompt_semantics`` (the contract's class default, or
        ``"unknown"`` when the harness does not honor a system prompt) and the
        ``harness_contract`` itself, so every agent — including out-of-tree SPI
        agents — records both. Overrides should spread ``super().get_environment_info()`` rather
        than returning a bare dict, or that guarantee is lost for that agent.

        Returns:
            A flat dict of JSON-serializable keys to merge.
        """
        return {
            "system_prompt_semantics": self.contract.system_prompt_semantics or "unknown",
            "harness_contract": self.contract.model_dump(mode="json"),
        }


class _LastEndStatus:
    """Remembers the last ``AgentEndEvent.status`` a legacy turn body emitted."""

    def __init__(self) -> None:
        self.status: AgentEndStatus | None = None

    def on_event(self, event: StreamEvent) -> None:
        if isinstance(event, AgentEndEvent):
            self.status = event.status
