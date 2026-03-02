"""Abstract base class for coding agents."""

from abc import ABC, abstractmethod
from typing import Any

from .models import AgentState, TurnRecord


class Agent(ABC):
    """Abstract base class for all coding agent implementations.

    Concrete implementations should handle the specific SDK/CLI
    interactions for different agents (Claude Code, Aider, etc.).
    """

    @abstractmethod
    async def start(self, working_directory: str) -> None:
        """Initialize and start the agent.

        Args:
            working_directory: Path to the working directory for the agent
        """
        pass

    @abstractmethod
    async def communicate(self, user_input: str) -> TurnRecord:
        """Send a message to the agent and receive its response.

        Args:
            user_input: The message/prompt to send to the agent

        Returns:
            TurnRecord containing the complete interaction

        Raises:
            RuntimeError: If agent is not started or communication fails
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the agent and clean up resources."""
        pass

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
