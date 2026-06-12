"""Agent registration and factory pattern for BYOA (bring-your-own-agent) support."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast


# Imports kept TYPE_CHECKING-only (with future annotations) so this module imports
# nothing from coder_eval at runtime. That keeps the dependency one-way — the
# plugin loader and models layer import the registry, never the reverse — so there
# is no import cycle (CodeQL py/cyclic-import).
if TYPE_CHECKING:
    from coder_eval.agent import Agent
    from coder_eval.models import AgentKind, ApiRoute, BaseAgentConfig

MethodConfigT = TypeVar("MethodConfigT", bound="BaseAgentConfig")
AgentClassT = TypeVar("AgentClassT")


@dataclass
class AgentRegistration[ConfigT: BaseAgentConfig]:
    """Metadata for a registered agent.

    Stores the agent class and its expected config class for runtime validation.
    """

    agent_class: type[Agent[Any]]
    config_class: type[ConfigT]


class AgentRegistry:
    """Global registry for custom agents.

    Provides a decorator-based registration pattern that decouples agent
    implementations from the orchestrator factory. Keyed by the agent *kind
    string* so a built-in :class:`AgentKind` member and a plugin-supplied raw
    string collide on the same key (``AgentKind`` is a ``StrEnum``): an external
    plugin can register a brand-new kind that is not an enum member.
    """

    _registry: ClassVar[dict[str, AgentRegistration[Any]]] = {}

    @classmethod
    def register(
        cls, agent_kind: str | AgentKind, config_class: type[MethodConfigT]
    ) -> Callable[[type[AgentClassT]], type[AgentClassT]]:
        """Decorator to register an agent class (identity-preserving).

        Usage:
            @AgentRegistry.register(AgentKind.CLAUDE_CODE, ClaudeCodeAgentConfig)
            class ClaudeCodeAgent(Agent[ClaudeCodeAgentConfig]):
                ...

        Args:
            agent_kind: The agent kind this agent implements — an ``AgentKind``
                member (built-ins) or a raw kind string (plugins).
            config_class: The config class this agent expects (e.g., ClaudeCodeAgentConfig)

        Returns:
            A decorator that registers and returns the agent class unchanged (preserves type)
        """

        def decorator(agent_cls: type[AgentClassT]) -> type[AgentClassT]:
            kind = str(agent_kind)
            existing = cls._registry.get(kind)
            # Re-registering the SAME classes is legitimate (idempotent built-in
            # reload via load_plugins(force=True)). Re-registering a kind with a
            # DIFFERENT implementation is a silent shadow: which agent runs would
            # depend on entry-point discovery order, which isn't stable across
            # environments — a reproducibility hole. Reject it loudly.
            if existing is not None and (existing.agent_class, existing.config_class) != (agent_cls, config_class):
                raise ValueError(
                    f"Agent kind {kind!r} is already registered to "
                    f"{existing.agent_class.__name__} ({existing.config_class.__name__}); "
                    f"{agent_cls.__name__} ({config_class.__name__}) cannot shadow it. "
                    f"Two plugins must not claim the same agent.type."
                )
            cls._registry[kind] = AgentRegistration(
                agent_class=agent_cls,  # type: ignore[arg-type]
                config_class=config_class,
            )
            return agent_cls

        return decorator

    @classmethod
    def get(cls, agent_kind: str | AgentKind) -> AgentRegistration[Any] | None:
        """Look up a registered agent by kind.

        Args:
            agent_kind: The agent kind to look up (``AgentKind`` member or raw string)

        Returns:
            AgentRegistration if found, None otherwise
        """
        return cls._registry.get(str(agent_kind))

    @classmethod
    def list_kinds(cls) -> list[str]:
        """Registered agent kind strings (sorted for stable error messages)."""
        return sorted(cls._registry)

    @classmethod
    def registrations(cls) -> list[AgentRegistration[Any]]:
        """All registered agent registrations (for config-class enumeration)."""
        return list(cls._registry.values())

    @classmethod
    def unregistered_kind_error(cls, agent_kind: str | AgentKind) -> ValueError:
        """The single ``ValueError`` for an unknown kind (shared by the factory and
        ``parse_agent_config``) so both report identically and list valid kinds."""
        return ValueError(f"No agent registered for type {str(agent_kind)!r}. Registered kinds: {cls.list_kinds()}")


def create_agent(
    agent_kind: str | AgentKind,
    config: BaseAgentConfig,
    route: ApiRoute | None = None,
    **kwargs: Any,
) -> Agent[Any]:
    """Factory function to create an agent by kind.

    Validates that the config matches the agent's registered config class.

    Args:
        agent_kind: The agent kind to instantiate (``AgentKind`` member or raw string)
        config: Configuration object (must match the registered agent's config class)
        route: Optional API routing configuration
        **kwargs: Additional arguments passed to the agent constructor

    Returns:
        An instance of the requested agent type

    Plugins must already be loaded: callers reach a config object through
    ``parse_agent_config`` (which loads plugins), and the orchestrator / CLI also
    load them up-front. ``create_agent`` deliberately does NOT import
    ``coder_eval.plugins`` itself, so ``plugins`` -> ``agents.registry`` stays a
    one-way edge (no import cycle).

    Raises:
        ValueError: If the agent_kind is not registered
        TypeError: If the config type doesn't match the agent's expected config class
    """
    registration = AgentRegistry.get(agent_kind)
    if not registration:
        raise AgentRegistry.unregistered_kind_error(agent_kind)

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
