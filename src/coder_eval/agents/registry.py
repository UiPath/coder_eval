"""Agent registration and factory pattern for BYOA (bring-your-own-agent) support."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from coder_eval.models import AgentKind, ApiRoute, BaseAgentConfig


if TYPE_CHECKING:
    from coder_eval.agent import Agent

MethodConfigT = TypeVar("MethodConfigT", bound=BaseAgentConfig)
AgentClassT = TypeVar("AgentClassT")


@dataclass
class AgentRegistration[ConfigT: BaseAgentConfig]:
    """Metadata for a registered agent.

    Stores the agent class and its expected config class for runtime validation.
    """

    agent_class: type["Agent[Any]"]
    config_class: type[ConfigT]


class AgentRegistry:
    """Global registry for custom agents.

    Provides a decorator-based registration pattern that decouples agent
    implementations from the orchestrator factory.
    """

    _registry: ClassVar[dict[AgentKind, AgentRegistration[Any]]] = {}

    @classmethod
    def register(
        cls, agent_kind: AgentKind, config_class: type[MethodConfigT]
    ) -> Callable[[type[AgentClassT]], type[AgentClassT]]:
        """Decorator to register an agent class (identity-preserving).

        Usage:
            @AgentRegistry.register(AgentKind.CLAUDE_CODE, ClaudeCodeAgentConfig)
            class ClaudeCodeAgent(Agent[ClaudeCodeAgentConfig]):
                ...

        Args:
            agent_kind: The AgentKind enum value this agent implements
            config_class: The config class this agent expects (e.g., ClaudeCodeAgentConfig)

        Returns:
            A decorator that registers and returns the agent class unchanged (preserves type)
        """

        def decorator(agent_cls: type[AgentClassT]) -> type[AgentClassT]:
            cls._registry[agent_kind] = AgentRegistration(
                agent_class=agent_cls,  # type: ignore[arg-type]
                config_class=config_class,
            )
            return agent_cls

        return decorator

    @classmethod
    def get(cls, agent_kind: AgentKind) -> AgentRegistration[Any] | None:
        """Look up a registered agent by kind.

        Args:
            agent_kind: The AgentKind to look up

        Returns:
            AgentRegistration if found, None otherwise
        """
        return cls._registry.get(agent_kind)


def create_agent(
    agent_kind: AgentKind,
    config: BaseAgentConfig,
    route: ApiRoute | None = None,
    **kwargs: Any,
) -> "Agent[Any]":
    """Factory function to create an agent by kind.

    Validates that the config matches the agent's registered config class.

    Args:
        agent_kind: The AgentKind to instantiate
        config: Configuration object (must match the registered agent's config class)
        route: Optional API routing configuration
        **kwargs: Additional arguments passed to the agent constructor

    Returns:
        An instance of the requested agent type

    Raises:
        ValueError: If the agent_kind is not registered
        TypeError: If the config type doesn't match the agent's expected config class
    """
    registration = AgentRegistry.get(agent_kind)
    if not registration:
        registered_kinds = list(AgentRegistry._registry.keys())
        raise ValueError(f"No agent registered for {agent_kind!r}. Registered agents: {registered_kinds}")

    # Type check: ensure config matches the registered agent's config class
    if not isinstance(config, registration.config_class):
        raise TypeError(
            f"Agent {agent_kind!r} expects {registration.config_class.__name__} "
            f"but received {type(config).__name__}. "
            f"Did you pass --type {agent_kind} with mismatched config?"
        )

    # Instantiate the agent with the typed config
    # Cast to Any to allow pyright to resolve the generic class instantiation
    return cast(Any, registration.agent_class)(config, route=route, **kwargs)
