"""Plugin discovery for bring-your-own-agent (BYOA) extensions.

External packages extend coder-eval by declaring a Python entry point in the
``coder_eval.plugins`` group whose target is a ``register(registry)`` callable::

    [project.entry-points."coder_eval.plugins"]
    my_plugin = "my_pkg.plugin:register"

At startup :func:`load_plugins` scans every installed distribution for that
group, imports each target, and calls it with the :class:`AgentRegistry` so the
plugin can add its agent kinds. coder-eval registers its own built-in agents
through the *same* group (the ``coder_eval`` entry point ->
:func:`coder_eval.agents.register_builtins`), so the discovery path is exercised
by core itself and cannot silently rot.

Discovery is idempotent and re-entrancy-safe (the ``_loaded`` flag is set before
the scan, so a plugin that imports back into coder-eval during its own
registration does not recurse). A plugin whose ``register`` raises is logged and
skipped — one broken plugin never aborts startup.
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

PLUGIN_ENTRY_POINT_GROUP = "coder_eval.plugins"

# coder-eval's own built-in agents register through this same entry point. A
# failure registering it is a real breakage (empty registry), NOT a skippable
# third-party plugin error — so it is fatal rather than logged-and-skipped.
BUILTIN_PLUGIN_NAME = "coder_eval"

_loaded = False


def load_plugins(*, force: bool = False) -> None:
    """Discover and run every ``coder_eval.plugins`` entry point's ``register`` hook.

    Idempotent: a second call is a no-op unless ``force=True``. The ``_loaded``
    flag is set *before* iterating so a plugin re-entering via
    :func:`ensure_plugins_loaded` during its own import does not recurse.
    """
    global _loaded
    if _loaded and not force:
        return
    _loaded = True

    from importlib.metadata import entry_points

    from coder_eval.agents.registry import AgentRegistry

    for ep in entry_points(group=PLUGIN_ENTRY_POINT_GROUP):
        try:
            register = ep.load()
            register(AgentRegistry)
        except Exception:
            # Built-in registration failing leaves the registry empty and would
            # surface later as a misleading "No agent registered for 'claude-code'".
            # Keep it fatal so the real (import/registration) cause fails loudly.
            if ep.name == BUILTIN_PLUGIN_NAME:
                # Clear the flag so a caller that catches and retries re-runs the
                # scan instead of getting a no-op against an empty registry.
                _loaded = False
                raise
            logger.exception("Failed to load coder_eval plugin %r (%s); skipping", ep.name, ep.value)


def ensure_plugins_loaded() -> None:
    """Run :func:`load_plugins` once if it has not already run.

    Safety-net for entry paths that do not go through CLI init (direct library
    use, tests): registry consumers call this before reading the registry so
    registration is always populated. ``create_agent`` calls it today; the
    config-dispatch consumers (``parse_agent_config`` and the agent-root config
    merge) wire it in once they become registry-driven.
    """
    if not _loaded:
        load_plugins()
