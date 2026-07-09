"""Tests for BYOA plugin discovery (``coder_eval.plugins``)."""

import importlib.metadata

import pytest

import coder_eval.plugins as plugins
from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import AgentKind, ClaudeCodeAgentConfig


PLUGIN_ENTRY_POINT_GROUP = plugins.PLUGIN_ENTRY_POINT_GROUP
ensure_plugins_loaded = plugins.ensure_plugins_loaded
load_plugins = plugins.load_plugins


class _FakeEntryPoint:
    """Minimal stand-in for an ``importlib.metadata.EntryPoint``."""

    def __init__(self, name: str, register) -> None:
        self.name = name
        self.value = f"fake.{name}:register"
        self._register = register

    def load(self):
        return self._register


@pytest.fixture(autouse=True)
def _reset_loaded_flag():
    """Each test controls discovery state explicitly, and any kinds a test
    registers are rolled back so the process-global registry never leaks."""
    saved_loaded = plugins._loaded
    saved_registry = dict(AgentRegistry._registry)
    plugins._loaded = False
    yield
    plugins._loaded = saved_loaded
    AgentRegistry._registry.clear()
    AgentRegistry._registry.update(saved_registry)


def _patch_entry_points(monkeypatch, eps):
    def fake_entry_points(*, group):
        assert group == PLUGIN_ENTRY_POINT_GROUP
        return list(eps)

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


def test_load_plugins_invokes_register_with_registry(monkeypatch):
    seen = []
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("p1", lambda reg: seen.append(reg))])

    load_plugins()

    assert seen == [AgentRegistry]


def test_load_plugins_idempotent(monkeypatch):
    calls = []
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("p1", lambda reg: calls.append(1))])

    load_plugins()
    load_plugins()  # no-op: already loaded
    assert len(calls) == 1

    load_plugins(force=True)  # re-runs
    assert len(calls) == 2


def test_load_plugins_skips_failing_plugin(monkeypatch):
    succeeded = []

    def boom(reg):
        raise RuntimeError("plugin exploded")

    _patch_entry_points(
        monkeypatch,
        [_FakeEntryPoint("bad", boom), _FakeEntryPoint("good", lambda reg: succeeded.append(reg))],
    )

    # Must not propagate: a broken plugin never aborts startup.
    load_plugins()

    assert succeeded == [AgentRegistry]


def test_load_plugins_reraises_builtin_failure(monkeypatch):
    """A failure registering the built-in 'coder_eval' entry point is FATAL — not
    swallowed — so a broken built-in import fails loudly instead of as a later
    misleading 'No agent registered for claude-code'."""

    def boom(reg):
        raise RuntimeError("builtin broke")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("coder_eval", boom)])

    with pytest.raises(RuntimeError, match="builtin broke"):
        load_plugins()

    # Flag is cleared so a caller that catches and retries re-runs the scan
    # instead of getting a no-op against an empty registry.
    assert plugins._loaded is False


def test_register_builtins_raises_if_a_builtin_missing():
    """The rot-protection guard fires if a built-in kind fails to register."""
    from coder_eval.agents import register_builtins

    class _StubRegistry:
        @staticmethod
        def get(kind):
            # Simulate codex failing to register (e.g. a future lazy-import refactor).
            return None if str(kind) == "codex" else object()

    with pytest.raises(RuntimeError, match="codex"):
        register_builtins(_StubRegistry)


def test_ensure_plugins_loaded_runs_once(monkeypatch):
    calls = []
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("p1", lambda reg: calls.append(1))])

    ensure_plugins_loaded()
    ensure_plugins_loaded()
    assert len(calls) == 1


def test_registry_string_and_enum_keys_are_equivalent():
    """A built-in kind looks up identically by enum member or raw string."""
    load_plugins()
    assert AgentRegistry.get(AgentKind.CODEX) is AgentRegistry.get("codex")
    assert AgentRegistry.get("claude-code") is not None


def test_register_accepts_raw_string_kind():
    """Plugins register a kind that is not an AgentKind enum member."""

    class _Cfg(ClaudeCodeAgentConfig):
        pass

    class _Agent:
        def __init__(self, config, route=None, **kwargs):
            self.config = config

    try:
        AgentRegistry.register("totally-custom-kind", _Cfg)(_Agent)
        reg = AgentRegistry.get("totally-custom-kind")
        assert reg is not None
        assert reg.agent_class is _Agent
        assert "totally-custom-kind" in AgentRegistry.list_kinds()
    finally:
        AgentRegistry._registry.pop("totally-custom-kind", None)


def test_register_rejects_conflicting_kind_collision():
    """Two distinct implementations claiming the same kind must NOT silently
    last-write-win (which agent runs would then depend on entry-point discovery
    order — a reproducibility hole). The second registration raises."""

    class _CfgA(ClaudeCodeAgentConfig):
        pass

    class _CfgB(ClaudeCodeAgentConfig):
        pass

    class _AgentA:
        def __init__(self, config, route=None, **kwargs):
            self.config = config

    class _AgentB:
        def __init__(self, config, route=None, **kwargs):
            self.config = config

    try:
        AgentRegistry.register("collide-kind", _CfgA)(_AgentA)
        with pytest.raises(ValueError, match="already registered"):
            AgentRegistry.register("collide-kind", _CfgB)(_AgentB)
        # The incumbent is untouched — the conflict did not overwrite it.
        reg = AgentRegistry.get("collide-kind")
        assert reg is not None and reg.agent_class is _AgentA
    finally:
        AgentRegistry._registry.pop("collide-kind", None)


def test_register_same_kind_same_classes_is_idempotent():
    """Re-registering the IDENTICAL classes (e.g. load_plugins(force=True) re-running
    register_builtins) is a legitimate no-op, not a collision."""

    class _Cfg(ClaudeCodeAgentConfig):
        pass

    class _Agent:
        def __init__(self, config, route=None, **kwargs):
            self.config = config

    try:
        AgentRegistry.register("idem-kind", _Cfg)(_Agent)
        AgentRegistry.register("idem-kind", _Cfg)(_Agent)  # no raise
        reg = AgentRegistry.get("idem-kind")
        assert reg is not None and reg.agent_class is _Agent
    finally:
        AgentRegistry._registry.pop("idem-kind", None)


async def test_noop_agent_handles_non_enum_plugin_kind(tmp_path):
    """A NoneAgentConfig subclass with a non-enum string kind drives a clean turn —
    exercises the ``str(self.config.type)`` path in NoOpAgent.communicate (a plain
    string Literal has no ``.value``)."""
    from typing import Literal as _Literal

    from coder_eval.agents.noop_agent import NoOpAgent
    from coder_eval.models import NoneAgentConfig

    class _PluginNoneConfig(NoneAgentConfig):
        type: _Literal["none-plugin"] = "none-plugin"  # type: ignore[assignment]

    agent = NoOpAgent(_PluginNoneConfig())
    await agent.start(str(tmp_path))
    # Would raise AttributeError if communicate used `.type.value` instead of str(...).
    record = await agent.communicate("hello")
    await agent.stop()

    assert record.crashed is False
    assert record.iteration == 1


def test_builtin_entry_point_is_declared_and_registers_builtins():
    """The in-tree ``coder_eval`` entry point exists and registers the built-ins,
    so the discovery path is exercised by core itself (cannot silently rot)."""
    eps = {ep.name: ep for ep in importlib.metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)}
    assert "coder_eval" in eps, "built-in coder_eval plugin entry point is not declared"

    register = eps["coder_eval"].load()
    register(AgentRegistry)
    for kind in ("claude-code", "codex", "none"):
        assert AgentRegistry.get(kind) is not None
