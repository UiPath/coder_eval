"""Abstract base class for coding agents."""

from abc import ABC, abstractmethod
from typing import Any

from .models import AgentState as AgentState
from .models import TurnRecord
from .streaming.callbacks import StreamCallback


class Agent(ABC):
    """Abstract base class for all coding agent implementations.

    Concrete implementations should handle the specific SDK/CLI
    interactions for different agents (Claude Code, Aider, etc.).
    """

    pending_turn: TurnRecord | None = None
    """Side-channel for partial turn records from failed ``communicate()`` calls.

    Implementations must set this to a ``crashed=True`` TurnRecord before
    raising any mid-turn exception that carries captured telemetry. Callers
    must read this slot after every failed ``communicate()`` call, then call
    ``discard_pending_turn()`` to clear it. Outside ``communicate()``, this
    slot is always None.
    """

    @abstractmethod
    async def start(self, working_directory: str, *, env_path_prepend: list[str] | None = None) -> None:
        """Initialize and start the agent.

        Args:
            working_directory: Path to the working directory for the agent
            env_path_prepend: Optional absolute directories to prepend to PATH for any
                subprocess the agent spawns (typically resolved sandbox mock dirs).
                Implementations that don't shell out may ignore this argument.
        """
        pass

    @abstractmethod
    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
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
        """Clear ``pending_turn`` and roll back per-turn bookkeeping.

        Idempotent: safe to call when ``pending_turn`` is already None.
        Call only after a failed ``communicate()``; never after a success.
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

    @abstractmethod
    def get_state(self) -> AgentState:
        """Get the current state of the agent.

        Returns:
            Current agent state
        """
        pass

    def get_sdk_options(self) -> dict[str, Any] | None:
        """Get the raw SDK options used for the last agent query.

        Returns:
            Dictionary of SDK option field names to values, or None if not available.
        """
        return None
