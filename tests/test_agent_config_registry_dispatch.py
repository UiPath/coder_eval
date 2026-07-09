"""Phase 2: registry-driven agent-config dispatch + open ``type`` string.

Covers parse_agent_config dispatch by registered kind, TaskDefinition's
base-typed agent field with SerializeAsAny round-trip, and the registry-driven
``-D agent.*`` validation path.
"""

import pytest
from pydantic import ValidationError

from coder_eval.models import (
    AgentKind,
    BaseAgentConfig,
    ClaudeCodeAgentConfig,
    CodexAgentConfig,
    EvaluationResult,
    FinalStatus,
    NoneAgentConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.config_merge import MergeError, validate_paths


def _now():
    from datetime import datetime

    return datetime(2026, 6, 11, 0, 0, 0)


# Minimal agent-independent criterion (TaskDefinition requires >=1 criterion).
CRIT = [{"type": "file_exists", "path": "x", "description": "x exists"}]


def test_parse_agent_config_dispatches_by_registered_kind():
    assert isinstance(parse_agent_config(type="claude-code"), ClaudeCodeAgentConfig)
    assert isinstance(parse_agent_config(type="codex"), CodexAgentConfig)
    assert isinstance(parse_agent_config(type="none"), NoneAgentConfig)


def test_parse_agent_config_enum_and_string_equivalent():
    assert type(parse_agent_config(type=AgentKind.CODEX)) is type(parse_agent_config(type="codex"))


def test_parse_agent_config_none_type_returns_base():
    cfg = parse_agent_config(model="claude-opus-4-8")
    assert type(cfg) is BaseAgentConfig
    assert cfg.type is None
    assert cfg.model == "claude-opus-4-8"


def test_parse_agent_config_unregistered_kind_lists_registered():
    with pytest.raises(ValueError, match=r"No agent registered for type 'bogus'.*Registered kinds"):
        parse_agent_config(type="bogus")


def test_task_definition_coerces_agent_dict_to_subclass():
    task = TaskDefinition(
        task_id="t1",
        description="d",
        initial_prompt="hi",
        agent={"type": "claude-code", "sdk_options": {"effort": "high"}},
        success_criteria=CRIT,
    )
    assert type(task.agent) is ClaudeCodeAgentConfig
    assert task.agent.sdk_options == {"effort": "high"}


def test_task_definition_round_trip_preserves_subclass_fields():
    """SerializeAsAny guard: subclass-only fields survive model_dump()/reload."""
    task = TaskDefinition(
        task_id="t1",
        description="d",
        initial_prompt="hi",
        agent={"type": "claude-code", "sdk_options": {"effort": "high"}, "claude_settings": {"a": 1}},
        success_criteria=CRIT,
    )
    dumped = task.model_dump()
    assert dumped["agent"]["sdk_options"] == {"effort": "high"}
    assert dumped["agent"]["claude_settings"] == {"a": 1}

    reloaded = TaskDefinition.model_validate(dumped)
    assert type(reloaded.agent) is ClaudeCodeAgentConfig
    assert reloaded.agent.sdk_options == {"effort": "high"}
    assert reloaded.agent == task.agent


def test_task_definition_accepts_prebuilt_subclass_instance():
    cfg = ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE, sdk_options={"effort": "low"})
    task = TaskDefinition(task_id="t1", description="d", initial_prompt="hi", agent=cfg, success_criteria=CRIT)
    assert type(task.agent) is ClaudeCodeAgentConfig
    assert task.agent.sdk_options == {"effort": "low"}


def test_task_definition_unknown_agent_type_raises():
    with pytest.raises(ValueError, match=r"No agent registered for type 'nope'"):
        TaskDefinition(
            task_id="t1", description="d", initial_prompt="hi", agent={"type": "nope"}, success_criteria=CRIT
        )


def test_is_none_agent_true_with_string_type():
    task = TaskDefinition(task_id="t1", description="d", agent={"type": "none"}, success_criteria=CRIT)
    assert task.is_none_agent is True


def test_validate_paths_did_you_mean_for_agent_subclass_field():
    """-D agent.<typo> resolves against registered config classes via the registry."""
    with pytest.raises(MergeError) as exc:
        validate_paths(["agent.sdk_option"])  # typo of sdk_options (a ClaudeCodeAgentConfig field)
    assert exc.value.suggestion == "sdk_options"


def test_validate_paths_accepts_real_agent_subclass_field():
    validate_paths(["agent.sdk_options", "agent.model", "agent.claude_settings"])  # no raise


def test_evaluation_result_agent_type_accepts_arbitrary_string():
    result = EvaluationResult(
        task_id="t1",
        task_description="d",
        agent_type="delegate_sdk",  # a plugin kind that is not an AgentKind member
        started_at=_now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=0,
    )
    assert result.agent_type == "delegate_sdk"


def test_invalid_field_value_still_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_agent_config(type="claude-code", permission_mode="not-a-real-mode")


class _PluginAgentConfig(BaseAgentConfig):
    """A plugin-style config: NOT a member of the built-in AgentConfig union."""

    from typing import Literal as _Literal

    type: _Literal["plugin-kind"]  # type: ignore[assignment]
    plugin_only_field: str = "sentinel"


@pytest.fixture
def _registered_plugin_kind():
    from coder_eval.agents.registry import AgentRegistry

    class _PluginAgent:
        def __init__(self, config, route=None, **kwargs):
            self.config = config

    AgentRegistry.register("plugin-kind", _PluginAgentConfig)(_PluginAgent)
    try:
        yield
    finally:
        AgentRegistry._registry.pop("plugin-kind", None)


def test_plugin_kind_round_trips_through_task_and_result(_registered_plugin_kind):
    """A non-union plugin subclass keeps its own fields across model_dump()/reload
    on BOTH TaskDefinition.agent and EvaluationResult.agent_config (SerializeAsAny)."""
    task = TaskDefinition(
        task_id="t1",
        description="d",
        initial_prompt="hi",
        agent={"type": "plugin-kind", "plugin_only_field": "kept"},
        success_criteria=CRIT,
    )
    assert type(task.agent) is _PluginAgentConfig
    reloaded_task = TaskDefinition.model_validate(task.model_dump())
    assert type(reloaded_task.agent) is _PluginAgentConfig
    assert reloaded_task.agent.plugin_only_field == "kept"

    result = EvaluationResult(
        task_id="t1",
        task_description="d",
        agent_type="plugin-kind",
        agent_config=parse_agent_config(type="plugin-kind", plugin_only_field="kept"),
        started_at=_now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=0,
    )
    dumped = result.model_dump()
    assert dumped["agent_config"]["plugin_only_field"] == "kept"
    reloaded_result = EvaluationResult.model_validate(dumped)
    assert type(reloaded_result.agent_config) is _PluginAgentConfig
    assert reloaded_result.agent_config.plugin_only_field == "kept"
