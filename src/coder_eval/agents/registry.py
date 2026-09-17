"""Agent registration and factory pattern for BYOA (bring-your-own-agent) support."""

# by-design model-hub ↔ registry type-level cycle; runtime imports are lazy per CE017
# pyright: reportImportCycles=false

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, TypeVar, cast, get_args


# TYPE_CHECKING-only imports, so this module imports nothing from coder_eval at
# module load and the dependency edge stays one-way (CodeQL py/cyclic-import).
# Rationale: .claude/notes/agents.md § Why the registry rejects a re-registration
if TYPE_CHECKING:
    from coder_eval.agent import Agent
    from coder_eval.models import AgentKind, ApiRoute, BaseAgentConfig

SPI_VERSION: Final[int] = 1

MethodConfigT = TypeVar("MethodConfigT", bound="BaseAgentConfig")
AgentClassT = TypeVar("AgentClassT")


def _validate_registration(kind: str, agent_cls: type, config_class: type) -> None:
    """Reject an ``(agent class, config class)`` pair the resolver cannot trust.

    Raises:
        TypeError: the agent class declares no ``HarnessContract``, its ``tool_names``
            presence does not match the contract's tool-list rows, or the config
            class is not a ``BaseAgentConfig`` with ``extra="forbid"`` whose ``type``
            Literal names ``kind``.
    """
    from coder_eval.models import BaseAgentConfig, Enforcement, HarnessContract, ToolNameMap

    agent_name = agent_cls.__name__
    config_name = config_class.__name__
    contract = getattr(agent_cls, "contract", None)
    if not isinstance(contract, HarnessContract):
        raise TypeError(
            f"Agent kind {kind!r}: {agent_name} must declare `contract = HarnessContract(...)` "
            + "as a class attribute, so the resolver knows which agent fields the harness honors."
        )
    lists_enforced = Enforcement.ENFORCED in (contract.allowed_tools, contract.disallowed_tools)
    tool_names = getattr(agent_cls, "tool_names", None)
    has_map = isinstance(tool_names, ToolNameMap) if lists_enforced else tool_names is None
    if not has_map:
        raise TypeError(
            f"Agent kind {kind!r}: {agent_name} must declare `tool_names = ToolNameMap(...)` exactly when its "
            + "contract enforces allowed_tools or disallowed_tools, and leave it None otherwise."
        )
    if not issubclass(config_class, BaseAgentConfig):
        raise TypeError(f"Agent kind {kind!r}: config class {config_name} must subclass BaseAgentConfig.")
    if config_class.model_config.get("extra") != "forbid":
        raise TypeError(
            f"Agent kind {kind!r}: config class {config_name} must keep extra='forbid', "
            + "so an unknown agent key in YAML is an error rather than silently dropped."
        )
    type_field = config_class.model_fields.get("type")
    literal_kinds = {str(arg) for arg in get_args(type_field.annotation)} if type_field is not None else set()
    if kind not in literal_kinds:
        raise TypeError(
            f"Agent kind {kind!r}: config class {config_name} must declare "
            + f"`type: Literal[{kind!r}]` (its `type` annotation admits {sorted(literal_kinds)})."
        )


@dataclass
class AgentRegistration[ConfigT: BaseAgentConfig]:
    """Metadata for a registered agent.

    Stores the agent class and its expected config class for runtime validation.
    """

    agent_class: type[Agent[Any]]
    config_class: type[ConfigT]


class AgentRegistry:
    """Global registry for custom agents.

    Keyed by the agent *kind string*, so a built-in :class:`AgentKind` member and
    a plugin-supplied raw string collide on one key — which is what lets a plugin
    register a brand-new kind that is not an enum member.
    """

    _registry: ClassVar[dict[str, AgentRegistration[Any]]] = {}

    @classmethod
    def register(
        cls, agent_kind: str | AgentKind, config_class: type[MethodConfigT], *, spi_version: int
    ) -> Callable[[type[AgentClassT]], type[AgentClassT]]:
        """Decorator to register an agent class (identity-preserving).

        Usage:
            @AgentRegistry.register(AgentKind.CLAUDE_CODE, ClaudeCodeAgentConfig, spi_version=SPI_VERSION)
            class ClaudeCodeAgent(Agent[ClaudeCodeAgentConfig]):
                ...

        Args:
            agent_kind: The agent kind this agent implements — an ``AgentKind``
                member (built-ins) or a raw kind string (plugins).
            config_class: The config class this agent expects (e.g., ClaudeCodeAgentConfig)
            spi_version: The ``SPI_VERSION`` the agent was written against.

        Returns:
            A decorator that registers and returns the agent class unchanged (preserves type)

        Raises:
            TypeError: ``spi_version`` is not this core's ``SPI_VERSION``.
        """
        if spi_version != SPI_VERSION:
            raise TypeError(
                f"Agent kind {str(agent_kind)!r} was written against coder_eval SPI {spi_version!r}, "
                + f"but this coder_eval provides SPI {SPI_VERSION}. Install a plugin version built for "
                + f"SPI {SPI_VERSION}, or a coder_eval version that provides SPI {spi_version!r}."
            )

        def decorator(agent_cls: type[AgentClassT]) -> type[AgentClassT]:
            kind = str(agent_kind)
            _validate_registration(kind, agent_cls, config_class)
            existing = cls._registry.get(kind)
            # Re-registering the SAME classes is legitimate (an idempotent
            # built-in reload); a DIFFERENT implementation for the same kind is a
            # silent shadow, so it is rejected loudly.
            # Rationale: .claude/notes/agents.md § Why the registry rejects a re-registration
            if existing is not None and (existing.agent_class, existing.config_class) != (agent_cls, config_class):
                raise ValueError(
                    f"Agent kind {kind!r} is already registered to "
                    + f"{existing.agent_class.__name__} ({existing.config_class.__name__}); "
                    + f"{agent_cls.__name__} ({config_class.__name__}) cannot shadow it. "
                    + "Two plugins must not claim the same agent.type."
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

    Plugins must ALREADY be loaded: this deliberately does not import
    ``coder_eval.plugins`` itself, so the edge stays one-way. Callers reach a
    config through ``parse_agent_config``, which loads them.

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
            + f"but received {type(config).__name__}. "
            + f"Did you pass --type {agent_kind} with mismatched config?"
        )

    # Instantiate the agent with the typed config
    # Cast to Any to allow pyright to resolve the generic class instantiation
    return cast(Any, registration.agent_class)(config, route=route, **kwargs)
