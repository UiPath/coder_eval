"""Unit tests for the layer-5 ``-D`` override engine (``apply_overrides`` + parsing).

Path validation (``validate_paths``), the generic merge, and ``build_nested``-style
nesting now live in ``config_merge`` and are covered by
``tests/test_config_merge_engine.py``. This module covers the layer-5 wrapper:
scalar parsing and ``apply_overrides`` behavior + lineage.
"""

from __future__ import annotations

import pytest

from coder_eval.models import (
    AgentKind,
    ClaudeCodeAgentConfig,
    CodexAgentConfig,
    ConfigLineageEntry,
    DockerDriverConfig,
    FileExistsCriterion,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.overrides import (
    OverrideError,
    apply_overrides,
    parse_override,
    parse_scalar,
)


def _make_task(
    agent: object | None = None,
    run_limits: RunLimits | None = None,
    sandbox: SandboxConfig | None = None,
) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="x",
        initial_prompt="hi",
        agent=agent if agent is not None else parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=sandbox if sandbox is not None else SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="x", path="f.txt")],
        run_limits=run_limits,
    )


class TestParseScalar:
    def test_int(self):
        assert parse_scalar("30") == 30

    def test_true_canonical_bool(self):
        assert parse_scalar("true") is True

    def test_truthy_alias_stays_string(self):
        assert parse_scalar("on") == "on"

    def test_null_is_none(self):
        assert parse_scalar("null") is None

    def test_flow_list(self):
        assert parse_scalar("[a,b]") == ["a", "b"]

    def test_invalid_yaml_raises(self):
        with pytest.raises(OverrideError, match="not valid YAML"):
            parse_scalar("{unclosed: ")


class TestParseOverride:
    def test_first_equals_split(self):
        path, value = parse_override("agent.model=a=b")
        assert path == "agent.model"
        assert value == "a=b"

    def test_missing_equals_raises(self):
        with pytest.raises(OverrideError, match="PATH=VALUE"):
            parse_override("agent.model")

    def test_empty_path_raises(self):
        with pytest.raises(OverrideError, match="path cannot be empty"):
            parse_override("=x")

    def test_empty_value_is_none(self):
        path, value = parse_override("agent.model=")
        assert path == "agent.model"
        assert value is None


class TestApplyOverrides:
    def test_sets_agent_model(self):
        task = _make_task()
        apply_overrides(task, {"agent.model": "opus"})
        assert task.agent.model == "opus"

    def test_sdk_options_merge_preserves_existing(self):
        task = _make_task(agent=parse_agent_config(type="claude-code", sdk_options={"max_thinking_tokens": 1024}))
        apply_overrides(task, {"agent.sdk_options.effort": "high"})
        assert task.agent.sdk_options == {"max_thinking_tokens": 1024, "effort": "high"}

    def test_run_limits_field_merge_keeps_other_keys(self):
        task = _make_task(run_limits=RunLimits(task_timeout=600))
        apply_overrides(task, {"run_limits.max_turns": 30})
        assert task.run_limits.max_turns == 30
        assert task.run_limits.task_timeout == 600

    def test_run_limits_none_base(self):
        task = _make_task(run_limits=None)
        apply_overrides(task, {"run_limits.max_turns": 5})
        assert task.run_limits is not None
        assert task.run_limits.max_turns == 5

    def test_sandbox_driver(self):
        task = _make_task()
        apply_overrides(task, {"sandbox.driver": "docker"})
        assert task.sandbox.driver == "docker"

    def test_sandbox_nested_preserves_default_image(self):
        task = _make_task()
        default_image = task.sandbox.docker.image
        apply_overrides(task, {"sandbox.docker.network": "none"})
        assert task.sandbox.docker.network == "none"
        assert task.sandbox.docker.image == default_image

    def test_env_passthrough_extra_appends_via_dash_d(self):
        """`-D` honors the append strategy (the divergence fix)."""
        task = _make_task(
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(env_passthrough_extra=["BASE"]))
        )
        apply_overrides(task, {"sandbox.docker.env_passthrough_extra": ["CLI"]})
        assert task.sandbox.docker.env_passthrough_extra == ["BASE", "CLI"]

    def test_system_prompt_file_clears_sibling_via_dash_d(self):
        """`-D agent.system_prompt_file` clears an inherited system_prompt (no crash)."""
        task = _make_task(agent=parse_agent_config(type="claude-code", system_prompt="inherited"))
        apply_overrides(task, {"agent.system_prompt_file": "p.txt"})
        assert task.agent.system_prompt is None
        assert task.agent.system_prompt_file == "p.txt"

    def test_agent_type_injection_switches_subclass(self):
        task = _make_task(agent=parse_agent_config(type="claude-code"))
        apply_overrides(task, {}, agent_type="codex")
        assert isinstance(task.agent, CodexAgentConfig)

    def test_sdk_options_on_codex_raises(self):
        task = _make_task(agent=parse_agent_config(type="codex"))
        with pytest.raises(OverrideError, match="only supported for claude-code"):
            apply_overrides(task, {"agent.sdk_options.effort": "high"})

    def test_sdk_options_with_agent_type_codex_raises(self):
        task = _make_task(agent=parse_agent_config(type="claude-code"))
        with pytest.raises(OverrideError, match="only supported for claude-code"):
            apply_overrides(task, {"agent.sdk_options.effort": "high"}, agent_type="codex")

    def test_unknown_root_raises(self):
        task = _make_task()
        with pytest.raises(OverrideError, match="unknown override root 'foo'"):
            apply_overrides(task, {"foo.bar": 1})

    def test_unknown_key_raises_override_error(self):
        """A MergeError from the resolver surfaces as an OverrideError (core stays typer-free)."""
        task = _make_task()
        with pytest.raises(OverrideError, match="did you mean 'model'"):
            apply_overrides(task, {"agent.modle": "x"})

    def test_list_replacement_via_value(self):
        task = _make_task()
        apply_overrides(task, {"agent.allowed_tools": ["Read", "Write"]})
        assert task.agent.allowed_tools == ["Read", "Write"]


class TestApplyOverridesLineage:
    def test_lineage_recorded_with_cli_source(self):
        task = _make_task()
        lineage: dict[str, ConfigLineageEntry] = {}
        apply_overrides(task, {"agent.model": "opus"}, lineage=lineage)
        entry = lineage["agent.model"]
        assert entry.source == "cli"
        assert entry.source_detail == "-D agent.model"

    def test_override_leaf_overwrites_lower_layer_lineage(self):
        task = _make_task()
        lineage = {"agent.model": ConfigLineageEntry(value="yaml-model", source="task")}
        apply_overrides(task, {"agent.model": "opus"}, lineage=lineage)
        assert lineage["agent.model"].source == "cli"
        assert lineage["agent.model"].source_detail == "-D agent.model"

    def test_agent_type_lineage_recorded(self):
        task = _make_task(agent=parse_agent_config(type="claude-code"))
        lineage: dict[str, ConfigLineageEntry] = {}
        apply_overrides(task, {}, agent_type="codex", lineage=lineage)
        assert lineage["agent.type"].source_detail == "--type"
        assert isinstance(task.agent, ClaudeCodeAgentConfig) is False

    def test_agent_type_lineage_uses_dash_d_when_explicit(self):
        """An explicit `-D agent.type` keeps its `-D` detail; --type does not clobber it."""
        task = _make_task(agent=parse_agent_config(type="claude-code"))
        lineage: dict[str, ConfigLineageEntry] = {}
        apply_overrides(task, {"agent.type": "codex"}, agent_type="claude-code", lineage=lineage)
        assert lineage["agent.type"].source_detail == "-D agent.type"

    def test_preserves_layers_1_4_lineage_for_untouched_fields(self):
        """Fix #1 guard: layer 5 must leave the layers-1-4 lineage for fields it
        doesn't touch untouched, adding only a cli entry for the field it sets."""
        task = _make_task(run_limits=RunLimits(max_turns=10))
        lineage: dict[str, ConfigLineageEntry] = {
            "agent.model": ConfigLineageEntry(value="haiku", source="variant"),
            "run_limits.max_turns": ConfigLineageEntry(value=10, source="task"),
        }
        apply_overrides(task, {"run_limits.turn_timeout": 45}, lineage=lineage)
        # untouched entries unchanged
        assert lineage["agent.model"].source == "variant"
        assert lineage["run_limits.max_turns"].source == "task"
        # only the touched field gained a cli entry
        assert lineage["run_limits.turn_timeout"].source == "cli"
        assert lineage["run_limits.turn_timeout"].source_detail == "-D run_limits.turn_timeout"
