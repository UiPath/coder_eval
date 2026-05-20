"""Tests for ``!``-prefixed negation in ``get_ignore_patterns``."""

from __future__ import annotations

import pytest

from coder_eval.resources import get_ignore_patterns, load_default_ignore_patterns


def test_default_patterns_include_dist_and_node_modules() -> None:
    defaults = load_default_ignore_patterns()
    assert "dist" in defaults
    assert "node_modules" in defaults


def test_negation_removes_default_pattern() -> None:
    result = get_ignore_patterns(["!dist", "!node_modules"])
    assert "dist" not in result
    assert "node_modules" not in result
    assert ".venv" in result, "untouched defaults should still be present"


def test_negation_and_addition_combine() -> None:
    result = get_ignore_patterns(["!dist", "*.bak"])
    assert "dist" not in result
    assert "*.bak" in result
    assert ".git" in result


def test_negation_of_unknown_pattern_is_noop() -> None:
    before = load_default_ignore_patterns()
    result = get_ignore_patterns(["!not-a-default"])
    assert result == before


def test_empty_or_missing_overrides_yield_defaults() -> None:
    assert get_ignore_patterns(None) == load_default_ignore_patterns()
    assert get_ignore_patterns([]) == load_default_ignore_patterns()


def test_leading_whitespace_is_stripped_so_negation_still_applies() -> None:
    # `" !dist"` would otherwise be added as a literal pattern (and never
    # match anything), silently leaving `dist` ignored — opposite of intent.
    result = get_ignore_patterns([" !dist", "  !node_modules\t"])
    assert "dist" not in result
    assert "node_modules" not in result


def test_bare_negation_raises() -> None:
    with pytest.raises(ValueError, match="bare negation"):
        get_ignore_patterns(["!"])


def test_empty_or_whitespace_entry_raises() -> None:
    with pytest.raises(ValueError, match="empty or whitespace"):
        get_ignore_patterns([""])
    with pytest.raises(ValueError, match="empty or whitespace"):
        get_ignore_patterns(["   "])


# ── field_validator hooks: malformed YAML fails at config-load, not mid-run ──


def test_sandboxconfig_ignore_patterns_field_validator_rejects_bare_negation() -> None:
    from pydantic import ValidationError

    from coder_eval.models.sandbox import SandboxConfig

    with pytest.raises(ValidationError, match="bare negation"):
        SandboxConfig(ignore_patterns=["!"])


def test_sandboxconfig_ignore_patterns_field_validator_strips_whitespace() -> None:
    from coder_eval.models.sandbox import SandboxConfig

    cfg = SandboxConfig(ignore_patterns=[" !dist", "  *.bak\t"])
    assert cfg.ignore_patterns == ["!dist", "*.bak"]


def test_agentconfig_ignore_patterns_field_validator_rejects_empty() -> None:
    from pydantic import ValidationError

    from coder_eval.models.agent_config import AgentConfig
    from coder_eval.models.enums import AgentKind

    with pytest.raises(ValidationError, match="empty or whitespace"):
        AgentConfig(type=AgentKind.CLAUDE_CODE, ignore_patterns=[""])
