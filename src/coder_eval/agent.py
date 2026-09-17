"""Abstract base class for coding agents."""

# by-design model-hub ↔ registry type-level cycle; runtime imports are lazy per CE017
# pyright: reportImportCycles=false

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from .models import AgentState as AgentState
from .models import ApiRoute, BaseAgentConfig, HarnessContract, TimingBasis, ToolNameMap
from .streaming.callbacks import StreamCallback
from .streaming.emitter import Clock, TurnEmitter, TurnOutcome
from .streaming.events import StopReason
from .timing import TurnClock


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

    _state: AgentState = AgentState.WORKING

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

    def _mark_stopped(self) -> None:
        """Common ``stop()`` tail: enter FINISHED. Subclasses call this after their own teardown."""
        self._state = AgentState.FINISHED

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
