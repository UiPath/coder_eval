"""Phase-2 tests: merge strategies are readable off the real model fields.

These pin the declared (or type-aware) strategy for every field that matters to
the resolver, and confirm the annotations are inert — models still construct and
round-trip exactly as before.
"""

from __future__ import annotations

import pytest

from coder_eval.models import (
    BaseAgentConfig,
    ClaudeCodeAgentConfig,
    CodexAgentConfig,
    DockerDriverConfig,
    NodeEnvConfig,
    PythonEnvConfig,
    SandboxConfig,
    merge_strategy_of,
    parse_agent_config,
)


def _strategy(model: type, field: str) -> str:
    return merge_strategy_of(model.model_fields[field])


class TestSandboxStrategies:
    @pytest.mark.parametrize(
        ("model", "field", "expected"),
        [
            (SandboxConfig, "template_sources", "append"),
            (SandboxConfig, "mock_path_dirs", "replace"),
            (SandboxConfig, "ignore_patterns", "replace"),
            (SandboxConfig, "driver", "replace"),
            # nested models / dicts take the type-aware deep default (no annotation):
            (SandboxConfig, "docker", "deep"),
            (SandboxConfig, "python", "deep"),
            (SandboxConfig, "node", "deep"),
            (SandboxConfig, "limits", "deep"),
            (DockerDriverConfig, "env_passthrough_extra", "append"),
            (DockerDriverConfig, "env_passthrough", "replace"),
            (DockerDriverConfig, "extra_mounts", "replace"),
            (DockerDriverConfig, "network", "replace"),
            (PythonEnvConfig, "env_packages", "replace"),
            (NodeEnvConfig, "env_packages", "replace"),
        ],
    )
    def test_strategy(self, model, field, expected):
        assert _strategy(model, field) == expected


class TestAgentStrategies:
    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("allowed_tools", "replace"),
            ("disallowed_tools", "replace"),
            ("plugins", "replace"),
            ("ignore_patterns", "replace"),
            ("model", "replace"),
            ("permission_mode", "replace"),
        ],
    )
    def test_base_agent_strategy(self, field, expected):
        assert _strategy(BaseAgentConfig, field) == expected

    def test_claude_only_fields(self):
        assert _strategy(ClaudeCodeAgentConfig, "claude_settings") == "deep"
        # free-form dict -> deep by type-aware default (no annotation)
        assert _strategy(ClaudeCodeAgentConfig, "sdk_options") == "deep"
        # setting_sources moved down from the base to the Claude subclass (SDK-decoupling).
        assert _strategy(ClaudeCodeAgentConfig, "setting_sources") == "replace"
        assert "setting_sources" not in BaseAgentConfig.model_fields


class TestExclusionGroups:
    def test_present_on_base_and_inherited(self):
        expected = (("system_prompt", "system_prompt_file"),)
        assert BaseAgentConfig._merge_exclusive_groups == expected
        assert ClaudeCodeAgentConfig._merge_exclusive_groups == expected
        assert CodexAgentConfig._merge_exclusive_groups == expected

    def test_classvar_is_not_a_model_field(self):
        assert "_merge_exclusive_groups" not in BaseAgentConfig.model_fields
        # And not a Pydantic private attr — guards against a future refactor dropping
        # the ClassVar[...] annotation and silently turning it into a PrivateAttr.
        assert "_merge_exclusive_groups" not in BaseAgentConfig.__private_attributes__


class TestAnnotationsInert:
    """Models still construct + round-trip exactly as before (MergeField is a thin
    pass-through to Field)."""

    def test_sandbox_round_trip(self):
        s = SandboxConfig(
            driver="docker",
            docker=DockerDriverConfig(env_passthrough_extra=["X"], network="none"),
            template_sources=[{"type": "template_dir", "path": "/t"}],
        )
        assert SandboxConfig.model_validate(s.model_dump()) == s

    def test_agent_round_trip_with_settings_and_sdk_options(self):
        a = parse_agent_config(
            type="claude-code",
            sdk_options={"effort": "low"},
            claude_settings={"permissions": {"deny": ["Read(/x/**)"]}},
            allowed_tools=["Read", "Write"],
        )
        assert ClaudeCodeAgentConfig.model_validate(a.model_dump()) == a

    def test_validation_alias_still_works(self):
        s = SandboxConfig.model_validate({"additional_ignore_patterns": ["!dist"]})
        assert s.ignore_patterns == ["!dist"]

    def test_prompt_exclusivity_validator_still_fires(self):
        with pytest.raises(ValueError, match="system_prompt"):
            parse_agent_config(type="claude-code", system_prompt="a", system_prompt_file="b")
