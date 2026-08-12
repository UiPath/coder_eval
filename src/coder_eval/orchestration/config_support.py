"""Resolution-time guard on per-agent ``BaseAgentConfig`` support declarations.

A base-config field must mean the same thing on every harness. Where a backend
cannot implement one, it says so on its agent class (``Agent.config_support``)
instead of dropping the field at runtime, and this module turns the strictest of
those declarations — :attr:`~coder_eval.agent.ConfigSupport.UNHONORED` — into a
hard error at resolution.

The error fires only when the resolved task actually *sets* the field to
something other than the config model's default. A default-valued field carries
no intent, so rejecting it would break every task on the harness rather than the
ones whose author expected the field to do something.

:attr:`~coder_eval.agent.ConfigSupport.APPROXIMATED` fields deliberately do NOT
raise here: they are honored, just imperfectly, and each agent already warns
about its own divergence at ``start()`` where the concrete resolved value is in
hand. Silence at resolution, loud in the task log.

Mirrors ``early_stop.py::validate_early_stop`` in shape and call sites: a
``ValueError`` subclass so the run path's resolve -> ``typer.BadParameter``
conversion covers it, caught explicitly by ``plan`` to flip its exit code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coder_eval.agent import ConfigSupport


if TYPE_CHECKING:
    from coder_eval.models import BaseAgentConfig, TaskDefinition


class AgentConfigSupportError(ValueError):
    """Raised when a task sets a config field the chosen agent does not implement."""


def _is_default(config: BaseAgentConfig, field: str) -> bool:
    """True when ``field`` still holds the config model's declared default.

    Compares against the field's default rather than checking
    ``model_fields_set``, because by the time a task resolves, the five-layer
    merge has explicitly set nearly every field — ``model_fields_set`` would
    report the whole block as author intent. The default is what "the author did
    not ask for anything here" actually looks like on a merged config.
    """
    model_field = type(config).model_fields.get(field)
    if model_field is None:
        # The field does not exist on THIS agent's config subclass, so the task
        # cannot have set it. A declaration naming a field its own config lacks is
        # a typo, but not a task author's problem — let it pass silently here and
        # let the lint rule catch it.
        return True
    default: Any = model_field.get_default(call_default_factory=True)
    return getattr(config, field, default) == default


def validate_config_support(task: TaskDefinition) -> None:
    """Reject a resolved task that sets a field its agent declares unhonored.

    Called after the config layers have merged — the same seats as
    ``validate_early_stop`` (``resolve_all_tasks`` post-CLI overrides, the
    ``plan`` per-variant loop, and defensively in ``Orchestrator._setup``).
    No-op for an agent that declares nothing (every built-in but Codex and
    Antigravity) and for a task that leaves the declared fields at their default.

    Raises:
        AgentConfigSupportError: on any set-but-unhonored field.
    """
    config = task.agent
    if config is None or config.type is None:
        return

    # Lazily import the registry + plugin loader so this module stays free of
    # runtime coder_eval imports beyond the ABC itself (mirrors early_stop).
    from coder_eval.agents.registry import AgentRegistry
    from coder_eval.plugins import ensure_plugins_loaded

    ensure_plugins_loaded()
    registration = AgentRegistry.get(str(config.type))
    if registration is None:
        # Not this guard's failure to report: an unregistered type already raises a
        # clear "is the providing plugin installed?" error where the agent is built.
        return

    offenders = [
        (field, note)
        for field, note in registration.agent_class.config_support.items()
        if note.support is ConfigSupport.UNHONORED and not _is_default(config, field)
    ]
    if not offenders:
        return

    details = "; ".join(f"agent.{field} ({note.reason})" for field, note in offenders)
    raise AgentConfigSupportError(
        f"agent type {str(config.type)!r} does not implement: {details}. "
        + "Leaving these set would run a different task than the same file runs on another "
        + "harness. Remove them, or pick an agent type that implements them."
    )
