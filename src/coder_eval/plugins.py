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
registration does not recurse). A plugin whose ``register`` raises stops the load
with a ``PluginLoadError`` that names it: a broken plugin is never skipped.
"""

from __future__ import annotations


PLUGIN_ENTRY_POINT_GROUP = "coder_eval.plugins"


class PluginLoadError(RuntimeError):
    """A ``coder_eval.plugins`` entry point failed to import or register."""


_loaded = False


def load_plugins(*, force: bool = False) -> None:
    """Discover and run every ``coder_eval.plugins`` entry point's ``register`` hook.

    Idempotent: a second call is a no-op unless ``force=True``. The ``_loaded``
    flag is set *before* iterating so a plugin re-entering via
    :func:`ensure_plugins_loaded` during its own import does not recurse.

    Raises:
        PluginLoadError: an entry point failed to load or its ``register`` raised.
            The flag is cleared, so a retry re-runs the scan.
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
        except Exception as e:
            _loaded = False
            raise PluginLoadError(
                f"coder_eval plugin {ep.name!r} ({ep.value}) failed to load: {e}. "
                + "Fix or uninstall the package that provides it."
            ) from e


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
