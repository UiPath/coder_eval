"""Regression: BaseAgentConfig no longer leaks claude_agent_sdk types.

``plugins`` stays on the vendor-neutral base (Codex + docker_runner read it) but is
retyped to the local ``LocalPluginConfig`` TypedDict; ``setting_sources`` moves down to
``ClaudeCodeAgentConfig`` (the only consumer).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coder_eval.models import (
    BaseAgentConfig,
    ClaudeCodeAgentConfig,
    CodexAgentConfig,
    LocalPluginConfig,
    NoneAgentConfig,
    parse_agent_config,
)


def test_setting_sources_only_on_claude_subclass() -> None:
    assert "setting_sources" not in BaseAgentConfig.model_fields
    assert "setting_sources" in ClaudeCodeAgentConfig.model_fields
    assert "setting_sources" not in CodexAgentConfig.model_fields
    assert "setting_sources" not in NoneAgentConfig.model_fields


def test_plugins_stays_shared_on_base() -> None:
    assert "plugins" in BaseAgentConfig.model_fields


def test_parse_claude_setting_sources_round_trip() -> None:
    cfg = parse_agent_config(type="claude-code", setting_sources=["project"])
    assert isinstance(cfg, ClaudeCodeAgentConfig)
    assert cfg.setting_sources == ["project"]


def test_parse_claude_plugins_round_trip_entries_are_dicts() -> None:
    cfg = parse_agent_config(type="claude-code", plugins=[{"type": "local", "path": "/x"}])
    assert cfg.plugins == [{"type": "local", "path": "/x"}]
    assert cfg.plugins is not None
    assert isinstance(cfg.plugins[0], dict)


def test_codex_shares_plugins() -> None:
    cfg = parse_agent_config(type="codex", plugins=[{"type": "local", "path": "/x"}])
    assert isinstance(cfg, CodexAgentConfig)
    assert cfg.plugins is not None
    assert isinstance(cfg.plugins[0], dict)


def test_codex_setting_sources_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_agent_config(type="codex", setting_sources=[])


def test_plugins_boundary_none_and_empty() -> None:
    assert parse_agent_config(type="claude-code").plugins is None
    assert parse_agent_config(type="claude-code", plugins=[]).plugins == []


def test_plugin_entry_missing_required_field_rejected() -> None:
    """TypedDict validation still rejects a malformed plugin entry (no 'path')."""
    with pytest.raises(ValidationError):
        parse_agent_config(type="claude-code", plugins=[{"type": "local"}])


def test_module_no_longer_binds_sdk_plugin_config() -> None:
    """The SDK ``SdkPluginConfig`` import is gone; ``SettingSource`` is now a local alias."""
    from coder_eval.models import agent_config as m

    assert not hasattr(m, "SdkPluginConfig")


def test_local_plugin_config_matches_sdk_shape() -> None:
    """SSOT drift guard: pin the local mirror against the real SDK shape.

    Fails loudly if a future claude_agent_sdk release changes SdkPluginConfig's shape,
    keeping the vendor-neutral copy honest. This is the one place the SDK type may be
    imported in tests — it is the canonical source being mirrored.
    """
    from typing import get_type_hints

    from claude_agent_sdk import SdkPluginConfig

    # Resolve forward refs: agent_config.py uses ``from __future__ import annotations``,
    # so LocalPluginConfig's raw __annotations__ are strings. Compare resolved hints.
    assert get_type_hints(LocalPluginConfig) == get_type_hints(SdkPluginConfig)


def test_setting_source_literal_matches_sdk_shape() -> None:
    """SSOT drift guard for the second hand-written SDK mirror: ``SettingSource``.

    Mirrors ``test_local_plugin_config_matches_sdk_shape`` for the local
    ``type SettingSource = Literal[...]`` alias. Fails loudly if a future
    claude_agent_sdk release adds/removes a setting source, so the vendor-neutral
    Literal can't silently reject a now-valid value. This is the one place the SDK
    type may be imported in tests — it is the canonical source being mirrored.
    """
    from typing import get_args

    from claude_agent_sdk import SettingSource as SdkSettingSource

    from coder_eval.models.agent_config import SettingSource as LocalSettingSource

    # ``SettingSource`` is a PEP 695 ``type`` alias (TypeAliasType); unwrap to the
    # underlying Literal before extracting its members.
    assert set(get_args(LocalSettingSource.__value__)) == set(get_args(SdkSettingSource))


def test_codex_model_dump_omits_setting_sources() -> None:
    """Freeze the cross-repo task.json contract: codex/none agent_config dumps omit
    ``setting_sources`` (it moved to ClaudeCodeAgentConfig), while claude-code retains it.
    """
    codex_dump = parse_agent_config(type="codex").model_dump()
    none_dump = parse_agent_config(type="none").model_dump()
    claude_dump = parse_agent_config(type="claude-code").model_dump()

    assert "setting_sources" not in codex_dump
    assert "setting_sources" not in none_dump
    assert "setting_sources" in claude_dump


def test_local_plugin_config_exported_sdk_plugin_config_not() -> None:
    from coder_eval.models import LocalPluginConfig  # noqa: F401

    with pytest.raises(ImportError):
        from coder_eval.models import SdkPluginConfig  # noqa: F401
