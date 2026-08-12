"""Abstract base class for coding agents."""

# by-design model-hub ↔ registry type-level cycle; runtime imports are lazy per CE017
# pyright: reportImportCycles=false

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, NoReturn, Protocol

from .errors import AgentCrashError, TurnTimeoutError
from .errors.agent import format_timeout_reason, truncate_crash_message
from .models import AgentState as AgentState
from .models import BaseAgentConfig, TurnRecord
from .streaming.callbacks import StreamCallback
from .streaming.collector import EventCollector
from .streaming.events import AgentEndStatus


logger = logging.getLogger(__name__)


class ConfigSupport(StrEnum):
    """How faithfully one agent backend implements a shared ``BaseAgentConfig`` field.

    A base-config field must mean the same thing on every harness, and where it
    cannot, the divergence has to be declared rather than discovered from a run that
    quietly did something else. Every field an agent does not fully implement is
    listed in its ``config_support`` map with one of these and a reason.
    """

    APPROXIMATED = "approximated"
    """Accepted and acted on, but with a documented divergence the operator must know
    about (e.g. Codex forwards ``disallowed_tools`` to the SDK, which does not enforce
    it). The agent warns at ``start()``; resolution does NOT reject."""

    UNHONORED = "unhonored"
    """Read by nothing — setting it changes no behavior. Resolution HARD-ERRORS when a
    task sets the field to anything other than its model default, because a silently
    dropped field means two harnesses reading one task file run different tasks."""


@dataclass(frozen=True)
class ConfigFieldSupport:
    """One entry in an agent's ``config_support`` map: the state plus why."""

    support: ConfigSupport
    reason: str


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
    """Side-channel for partial turn records from failed ``communicate()`` calls.

    Implementations must set this to a ``crashed=True`` TurnRecord before
    raising any mid-turn exception that carries captured telemetry. Callers
    must read this slot after every failed ``communicate()`` call, then call
    ``discard_pending_turn()`` to clear it. Outside ``communicate()``, this
    slot is always None.
    """

    # Shared turn-lifecycle bookkeeping. Class-level defaults so subclasses get
    # the behavior without re-declaring them in __init__ (they may still set
    # `_state` in start()). `_iteration_was_incremented` is set True right after
    # the counter bump at the top of `communicate()` and consumed by
    # `discard_pending_turn()`, which rolls the counter back exactly once per
    # failed turn — even when partial-record assembly leaves `pending_turn=None`.
    _state: AgentState = AgentState.WORKING
    _iteration: int = 0
    _iteration_was_incremented: bool = False

    # Capability flag: whether this agent honors the cooperative ``should_stop``
    # interrupt threaded through ``communicate()`` (early-stop-on-criterion).
    # Default False — arming early-stop on an agent that does not set this True
    # is rejected at resolution time. Concrete agents that check ``should_stop``
    # between messages override it to True.
    supports_cooperative_stop: ClassVar[bool] = False

    # Capability flag: whether this agent's constructor accepts the ``cost_log_tags``
    # kwarg (proxy-side actual-cost correlation headers for the LiteLLM backend).
    # Default False — the agent-agnostic ``create_agent`` factory must only forward
    # ``cost_log_tags`` to agents that set this True, else a route-driven kwarg would
    # crash every agent (NoOp/Codex/Antigravity/plugins) whose ``__init__`` lacks it.
    supports_cost_log_tags: ClassVar[bool] = False

    # Declared divergences from the shared ``BaseAgentConfig`` contract, keyed by field
    # name. Empty (the default) asserts the agent honors every field, so a new agent
    # opts in to scrutiny only where it must — but a field it silently drops without
    # declaring is a bug, not a shortcut. Read by
    # ``orchestration/config_support.py::validate_config_support`` at resolution and by
    # the parity table in docs/agents/HARNESS_PARITY.md.
    config_support: ClassVar[dict[str, ConfigFieldSupport]] = {}

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
    # Tiny, byte-identical fragments that recur across (and within) the agent
    # turn-loops. Each agent keeps its OWN outer try/except/finally bracket — the
    # brackets genuinely differ (flat vs nested, finally vs not) — and calls these
    # from inside its existing branches. They take the agent's own per-turn
    # ``finalize`` callable (the turn-state's method) so the helper never needs to
    # know how each agent assembles its AgentEndEvent payload.

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
        """
        pass

    @abstractmethod
    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        """Send a message to the agent and receive its response.

        Args:
            user_input: The message/prompt to send to the agent
            stream_callback: Optional callback for real-time event streaming
            timeout: Hard wall-clock deadline in seconds. When exceeded the
                agent must force-terminate any in-flight subprocess and raise
                TurnTimeoutError. Implementations should not rely solely on
                asyncio cancellation (the Claude Agent SDK uses anyio task
                groups that swallow cooperative cancellation).
            max_turns: Hard cap on inner-loop turns within this single
                ``communicate()`` call. When the agent would exceed it, the
                returned ``TurnRecord`` has ``max_turns_exhausted=True``.
                None defers to the underlying SDK default.
            should_stop: Cooperative early-stop poll for early-stop-on-criterion.
                When provided, an implementation that supports cooperative
                stopping (``supports_cooperative_stop=True``) should call it at
                each safe message boundary and, when it returns True, stop
                pulling further work and finalize the turn cleanly
                (``crashed=False``, no raise). ``None`` (default) preserves the
                pre-existing behavior exactly. Agents that do not support it
                accept the argument and ignore it (the orchestrator only passes
                it to a capable agent).

        Returns:
            TurnRecord containing the complete interaction

        Raises:
            RuntimeError: If agent is not started or communication fails.
            TurnTimeoutError: Timeout elapsed; implementations must set
                ``self.pending_turn`` to a ``crashed=True`` partial TurnRecord
                before raising if telemetry was captured.
            AgentCrashError: Agent failed mid-turn; same ``pending_turn`` contract.

        On success, ``pending_turn`` must be None and the completed TurnRecord
        is returned directly. On failure, ``pending_turn`` is set (if telemetry
        was available) before raising — rollback of per-turn bookkeeping happens
        exclusively in ``discard_pending_turn``, which the caller invokes after
        every failed ``communicate()``.

        Streaming contract: the agent is the SOLE emitter of the standardized
        event protocol (the orchestrator is a pure consumer). An implementation
        MUST emit exactly one ``AgentStartEvent`` at the top of ``communicate()``
        and exactly one matching ``AgentEndEvent`` on every exit path (success,
        crash, or timeout — emit it from ``finally``), with one ``TurnStartEvent``
        / ``TurnEndEvent`` pair per inner turn and ``ToolStartEvent`` /
        ``ToolEndEvent`` for each tool call (every ``ToolStart`` closed by a
        ``ToolEnd``, including ``status=unresolved`` for tools orphaned by a crash).
        Events fan out through an internal ``EventCollector`` (which builds the
        returned ``TurnRecord``) and the caller's ``stream_callback``; renderers
        and the task-log handler consume the same stream.
        """
        pass

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
        the agent starts. Default: nothing to add.

        Returns:
            A flat dict of JSON-serializable keys to merge; empty by default.
        """
        return {}
