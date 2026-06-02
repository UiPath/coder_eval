"""Phase-0 characterization tests for config-merge behavior.

Pins the CURRENT behavior of the two independent merge implementations —
layers 1-4 in ``resolve_task_for_variant`` and layer 5 in ``apply_overrides`` —
so the declarative-merge refactor (c/2026-06-01-declarative-merge-strategies.md)
can prove parity as it consolidates them onto one resolver.

Tests marked ``@pytest.mark.divergence`` pin a behavior the refactor
INTENTIONALLY changes (a layer-4-vs-layer-5 inconsistency, an order decision, or
a layer-5 crash). Each such test names the phase that flips it. When that phase
lands, the assertion is updated to the unified behavior and the marker removed.
"""

from __future__ import annotations

from typing import Any

import pytest

from coder_eval.models import (
    ConfigLineageEntry,
    DockerDriverConfig,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
    TemplateDirSource,
    parse_agent_config,
)
from coder_eval.orchestration.experiment import resolve_task_for_variant
from coder_eval.orchestration.overrides import OverrideError, apply_overrides


HAIKU = "claude-haiku-4-5-20251001"


def _make_task(agent: dict | None = None, **kwargs: Any) -> TaskDefinition:
    defaults: dict[str, Any] = {
        "task_id": "test-task",
        "description": "Test task",
        "initial_prompt": "Do something",
        "sandbox": {"driver": "tempdir"},
        "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "File exists"}],
    }
    if agent is not None:
        defaults["agent"] = agent
    defaults.update(kwargs)
    return TaskDefinition(**defaults)


def _default_exp(agent: dict | None = None, **defaults_kwargs: Any) -> ExperimentDefinition:
    agent = agent if agent is not None else {"type": "claude-code", "permission_mode": "acceptEdits"}
    return ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent=agent, **defaults_kwargs),
        variants=[ExperimentVariant(variant_id="default")],
    )


def _live_task(agent: dict | None = None, *, sandbox: SandboxConfig | None = None, **kwargs: Any) -> TaskDefinition:
    """A resolved-style task usable directly with ``apply_overrides`` (layer 5)."""
    return TaskDefinition(
        task_id="t",
        description="x",
        initial_prompt="hi",
        agent=parse_agent_config(**(agent if agent is not None else {"type": "claude-code"})),
        sandbox=sandbox if sandbox is not None else SandboxConfig(driver="tempdir"),
        success_criteria=[{"type": "file_exists", "path": "f.txt", "description": "x"}],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Layers 1-4 (resolve_task_for_variant)
# ---------------------------------------------------------------------------


class TestLayers14Current:
    def test_agent_scalar_replace_variant_wins(self):
        default_exp = _default_exp()
        task = _make_task(agent={"type": "claude-code", "model": "task-model"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v", agent={"model": HAIKU})],
        )
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.model == HAIKU
        assert lineage["agent.model"].source == "variant"

    def test_sdk_options_per_key_shallow_update(self):
        """sdk_options keys merge across layers (additive); a higher layer overrides one key."""
        default_exp = _default_exp(agent={"type": "claude-code", "sdk_options": {"effort": "low"}})
        task = _make_task(agent={"type": "claude-code", "sdk_options": {"max_thinking_tokens": 1024}})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v", agent={"sdk_options": {"effort": "high"}})],
        )
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.sdk_options == {"effort": "high", "max_thinking_tokens": 1024}

    def test_sdk_options_nested_dict_value_merges_recursively(self):
        """UNIFIED in Phase 4: a dict-valued sdk option (``output_format``) set at a
        higher layer now RECURSIVELY merges; sibling sub-keys set lower survive
        (matching layer 5). (Was a shallow-replace divergence before the refactor.)
        """
        default_exp = _default_exp(
            agent={"type": "claude-code", "sdk_options": {"output_format": {"type": "json", "schema": "S"}}}
        )
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v", agent={"sdk_options": {"output_format": {"type": "xml"}}})],
        )
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        # schema:"S" survives — recursive merge of the output_format dict.
        assert resolved.agent.sdk_options["output_format"] == {"type": "xml", "schema": "S"}

    def test_nested_model_subobject_merges_deeply_preserving_siblings(self):
        """UNIFIED in Phase 4: a nested-model sub-object (``docker``) set at a higher
        layer now DEEP-merges; sibling keys a lower layer set survive (matching
        layer 5, and fixing the latent layer-4 sibling-loss bug). exp-defaults sets
        ``docker.image`` and the task sets ``docker.network`` — BOTH survive.
        """
        default_exp = _default_exp()
        task = _make_task(
            agent={"type": "claude-code"},
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(network="none")),
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(sandbox=SandboxConfig(docker=DockerDriverConfig(image="custom-image"))),
            variants=[ExperimentVariant(variant_id="v")],
        )
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.sandbox.docker.network == "none"  # from task
        assert resolved.sandbox.docker.image == "custom-image"  # from exp-defaults — preserved

    def test_run_limits_field_merge_keeps_unset_lower_keys(self):
        default_exp = _default_exp(run_limits=RunLimits(turn_timeout=60))
        task = _make_task(agent={"type": "claude-code"}, run_limits=RunLimits(task_timeout=300))
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v", run_limits=RunLimits(max_turns=5))],
        )
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.turn_timeout == 60  # from default experiment
        assert resolved.run_limits.task_timeout == 300  # from task
        assert resolved.run_limits.max_turns == 5  # from variant

    def test_env_passthrough_extra_appends(self):
        default_exp = _default_exp()
        task = _make_task(
            agent={"type": "claude-code"},
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(env_passthrough_extra=["TASK_VAR"])),
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(
                sandbox=SandboxConfig(docker=DockerDriverConfig(env_passthrough_extra=["EXP_VAR"]))
            ),
            variants=[ExperimentVariant(variant_id="v")],
        )
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        # exp-defaults appended first, then task.
        assert resolved.sandbox.docker.env_passthrough_extra == ["EXP_VAR", "TASK_VAR"]

    def test_template_sources_task_first_order(self):
        """template_sources resolve task-first: the task's base templates, then
        experiment-defaults and variant overlays appended after (the documented
        "appended after task's base templates" contract — preserved by the refactor).
        """
        default_exp = _default_exp()
        task = _make_task(
            agent={"type": "claude-code"},
            sandbox={"driver": "tempdir", "template_sources": [{"type": "template_dir", "path": "/task"}]},
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(template_sources=[TemplateDirSource(path="/exp")]),
            variants=[ExperimentVariant(variant_id="v", template_sources=[TemplateDirSource(path="/variant")])],
        )
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.sandbox.template_sources is not None
        paths = [s.path for s in resolved.sandbox.template_sources]
        assert paths == ["/task", "/exp", "/variant"]  # task base first, then overlays

    def test_system_prompt_file_at_higher_layer_clears_inherited_system_prompt(self):
        """A higher layer setting system_prompt_file clears an inherited system_prompt."""
        default_exp = _default_exp(agent={"type": "claude-code", "system_prompt": "inherited prompt"})
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v", agent={"system_prompt_file": "prompts/p.txt"})],
        )
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.system_prompt is None
        assert resolved.agent.system_prompt_file == "prompts/p.txt"

    def test_nested_exp_sandbox_driver_contributes_without_top_level(self):
        """An experiment-defaults `sandbox.driver` (nested, no top-level `driver`)
        still sets the resolved driver — the nested block contributes, the
        top-level field only OVERRIDES it when present."""
        default_exp = _default_exp()
        task = _make_task(agent={"type": "claude-code"}, sandbox=SandboxConfig())  # driver unset
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(sandbox=SandboxConfig(driver="docker")),
            variants=[ExperimentVariant(variant_id="v")],
        )
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.sandbox.driver == "docker"

    def test_driver_precedence_exp_defaults_lt_task_lt_variant(self):
        default_exp = _default_exp()
        task = _make_task(agent={"type": "claude-code"}, sandbox=SandboxConfig(driver="tempdir"))
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(driver="docker"),
            variants=[ExperimentVariant(variant_id="v", driver="tempdir")],
        )
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        # task explicitly set driver=tempdir (model_fields_set), variant re-set tempdir → variant wins source.
        assert resolved.sandbox.driver == "tempdir"
        assert lineage["sandbox.driver"].source == "variant"


class TestLayers14Lineage:
    """Regression guard for Fix #1: snapshot the full layers-1-4 lineage map for a
    rich task x variant fixture. Phase 4 asserts layer 5 leaves these untouched
    for fields ``-D``/``.env`` does not set.
    """

    def test_rich_lineage_snapshot(self):
        default_exp = _default_exp(agent={"type": "claude-code", "permission_mode": "acceptEdits"})
        task = _make_task(
            agent={"type": "claude-code", "sdk_options": {"effort": "high"}},
            run_limits=RunLimits(task_timeout=300),
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(
                agent={"model": "base-model"},
                run_limits=RunLimits(turn_timeout=60),
                driver="tempdir",
            ),
            variants=[ExperimentVariant(variant_id="v", agent={"model": HAIKU}, driver="docker")],
        )
        _resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])

        sources = {k: v.source for k, v in lineage.items()}
        assert sources["agent.type"] == "task"
        assert sources["agent.permission_mode"] == "default"
        assert sources["agent.model"] == "variant"
        assert sources["agent.sdk_options.effort"] == "task"
        assert sources["run_limits.turn_timeout"] == "experiment-defaults"
        assert sources["run_limits.task_timeout"] == "task"
        assert sources["sandbox.driver"] == "variant"


# ---------------------------------------------------------------------------
# Layer 5 (apply_overrides)
# ---------------------------------------------------------------------------


class TestLayer5Current:
    def test_sdk_options_recursive_merge(self):
        task = _live_task(agent={"type": "claude-code", "sdk_options": {"max_thinking_tokens": 1024}})
        apply_overrides(task, {"agent.sdk_options.effort": "high"})
        assert task.agent.sdk_options == {"max_thinking_tokens": 1024, "effort": "high"}

    def test_run_limits_sibling_keys_preserved(self):
        task = _live_task(run_limits=RunLimits(task_timeout=600))
        apply_overrides(task, {"run_limits.max_turns": 30})
        assert task.run_limits.max_turns == 30
        assert task.run_limits.task_timeout == 600

    def test_list_field_replaces(self):
        task = _live_task(agent={"type": "claude-code", "allowed_tools": ["Read"]})
        apply_overrides(task, {"agent.allowed_tools": ["Write", "Bash"]})
        assert task.agent.allowed_tools == ["Write", "Bash"]

    def test_env_passthrough_extra_appends(self):
        """UNIFIED in Phase 3: ``-D`` on env_passthrough_extra now APPENDS to the
        resolved list (honors the field's ``append`` strategy), matching the
        variant layer. (Was a replace divergence before the refactor.)
        """
        task = _live_task(
            agent={"type": "claude-code"},
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(env_passthrough_extra=["BASE_VAR"])),
        )
        apply_overrides(task, {"sandbox.docker.env_passthrough_extra": ["CLI_VAR"]})
        assert task.sandbox.docker.env_passthrough_extra == ["BASE_VAR", "CLI_VAR"]  # appended

    def test_system_prompt_file_override_clears_sibling(self):
        """UNIFIED in Phase 3: ``-D agent.system_prompt_file`` on a task whose
        resolved agent already carries ``system_prompt`` now CLEARS the sibling
        (no crash), matching the layer-4 exclusion behavior.
        """
        task = _live_task(agent={"type": "claude-code", "system_prompt": "existing"})
        apply_overrides(task, {"agent.system_prompt_file": "prompts/p.txt"})
        assert task.agent.system_prompt is None
        assert task.agent.system_prompt_file == "prompts/p.txt"

    def test_sdk_options_on_codex_raises_friendly(self):
        task = _live_task(agent={"type": "codex"})
        with pytest.raises(OverrideError, match="only supported for claude-code"):
            apply_overrides(task, {"agent.sdk_options.effort": "high"})

    def test_lineage_cli_source_for_touched_paths_only(self):
        task = _live_task()
        lineage: dict[str, ConfigLineageEntry] = {
            "agent.model": ConfigLineageEntry(value="yaml-model", source="task"),
            "run_limits.max_turns": ConfigLineageEntry(value=10, source="variant"),
        }
        apply_overrides(task, {"run_limits.max_turns": 20}, lineage=lineage)
        # touched path relabeled to cli; untouched task entry preserved.
        assert lineage["run_limits.max_turns"].source == "cli"
        assert lineage["run_limits.max_turns"].source_detail == "-D run_limits.max_turns"
        assert lineage["agent.model"].source == "task"
