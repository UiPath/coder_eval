"""Tests for experiment config resolution (merge logic)."""

import pytest

from coder_eval.models import (
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    TaskDefinition,
    TemplateDirSource,
)
from coder_eval.orchestration.experiment import resolve_task_for_variant


def _make_task(agent: dict | None = None, **kwargs) -> TaskDefinition:
    """Create a minimal TaskDefinition."""
    defaults = {
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


def _make_default_experiment() -> ExperimentDefinition:
    """Create the default experiment (mimics experiments/default.yaml)."""
    return ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
        variants=[ExperimentVariant(variant_id="default")],
    )


class TestResolveTaskForVariant:
    def test_default_fills_missing_agent(self):
        """Task without agent gets defaults from default experiment."""
        default_exp = _make_default_experiment()
        task = _make_task(agent=None)
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent is not None
        assert resolved.agent.type == "claude-code"
        assert resolved.agent.permission_mode == "acceptEdits"
        assert lineage["agent.type"].source == "default"

    def test_task_agent_overrides_default(self):
        """Task agent fields override default experiment fields."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "permission_mode": "bypassPermissions"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.permission_mode == "bypassPermissions"
        assert lineage["agent.permission_mode"].source == "task"

    def test_task_model_not_clobbered_by_default_experiment(self):
        """Task model should not be overridden when experiment IS the default (no --experiment flag)."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "model": "custom-model"})
        # When no --experiment is given, experiment == default_experiment
        resolved, lineage = resolve_task_for_variant(default_exp, task, default_exp, default_exp.variants[0])
        assert resolved.agent.model == "custom-model"
        assert lineage["agent.model"].source == "task"

    def test_task_overrides_experiment_defaults(self):
        """Task agent settings override experiment defaults."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "permission_mode": "acceptEdits"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(agent={"permission_mode": "bypassPermissions"}),
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.permission_mode == "acceptEdits"
        assert lineage["agent.permission_mode"].source == "task"

    def test_variant_overrides_base(self):
        """Variant settings override experiment base."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(agent={"model": "base-model"}),
            variants=[ExperimentVariant(variant_id="variant1", agent={"model": "variant-model"})],
        )

        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.model == "variant-model"
        assert lineage["agent.model"].source == "variant"

    def test_full_precedence_chain(self):
        """Full 4-layer merge: default < experiment-defaults < task < variant."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(
                agent={
                    "type": "claude-code",
                    "permission_mode": "acceptEdits",
                    "allowed_tools": ["Read"],
                }
            ),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(
            agent={
                "type": "claude-code",
                "allowed_tools": ["Read", "Write", "Bash"],
            }
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(agent={"permission_mode": "bypassPermissions"}),
            variants=[ExperimentVariant(variant_id="opus", agent={"model": "claude-opus-4-20250514"})],
        )

        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.type == "claude-code"
        assert resolved.agent.allowed_tools == ["Read", "Write", "Bash"]  # from task (layer 3)
        assert (
            resolved.agent.permission_mode == "bypassPermissions"
        )  # from experiment-defaults (layer 2, task didn't set it)
        assert resolved.agent.model == "claude-opus-4-20250514"  # from variant (layer 4)
        # Lineage assertions
        assert lineage["agent.allowed_tools"].source == "task"
        assert lineage["agent.permission_mode"].source == "experiment-defaults"
        assert lineage["agent.model"].source == "variant"

    def test_list_fields_replace_atomically(self):
        """List fields (allowed_tools) in later layers fully replace earlier values."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "allowed_tools": ["Read", "Write", "Bash"]})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="limited", agent={"allowed_tools": ["Read", "Bash"]})],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.allowed_tools == ["Read", "Bash"]

    def test_disallowed_tools_from_experiment_defaults(self):
        """disallowed_tools in experiment defaults propagates to resolved agent config."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(agent={"disallowed_tools": ["TodoWrite"]}),
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.disallowed_tools == ["TodoWrite"]
        assert lineage["agent.disallowed_tools"].source == "experiment-defaults"

    def test_disallowed_tools_from_variant_overrides_defaults(self):
        """Variant disallowed_tools fully replaces experiment defaults."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(agent={"disallowed_tools": ["TodoWrite"]}),
            variants=[
                ExperimentVariant(variant_id="v1", agent={"disallowed_tools": ["TodoWrite", "Agent"]}),
            ],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.disallowed_tools == ["TodoWrite", "Agent"]

    def test_disallowed_tools_from_task_overrides_experiment(self):
        """Task-level disallowed_tools wins over experiment defaults."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "disallowed_tools": ["Bash"]})
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(agent={"disallowed_tools": ["TodoWrite"]}),
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.disallowed_tools == ["Bash"]
        assert lineage["agent.disallowed_tools"].source == "task"

    def test_scalar_overrides(self):
        """Scalar fields (max_iterations, task_timeout) resolve through precedence."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, max_iterations=3)
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(max_iterations=5, task_timeout=300),
            variants=[ExperimentVariant(variant_id="fast", max_iterations=2, task_timeout=120)],
        )

        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.max_iterations == 2  # variant wins
        assert resolved.task_timeout == 120  # variant wins
        assert lineage["max_iterations"].source == "variant"
        assert lineage["task_timeout"].source == "variant"

    def test_resolved_task_preserves_non_agent_fields(self):
        """Resolution should not alter task_id, description, criteria, sandbox, etc."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, task_id="my-task", description="My test")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.task_id == "my-task"
        assert resolved.description == "My test"
        assert len(resolved.success_criteria) == 1


class TestDefaultExperimentScalarOverrides:
    """Tests for layer-1 default experiment scalar resolution."""

    def test_default_experiment_max_iterations_applied(self):
        """default.yaml base.max_iterations should override task's Pydantic default."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(
                agent={"type": "claude-code", "permission_mode": "acceptEdits"},
                max_iterations=5,
            ),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)
        assert task.max_iterations == 3  # Pydantic default

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.max_iterations == 5

    def test_default_experiment_task_timeout_applied(self):
        """default.yaml base.task_timeout should be applied when task has no explicit timeout."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, task_timeout=600),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.task_timeout == 600

    def test_default_experiment_turn_timeout_applied(self):
        """default.yaml base.turn_timeout should be applied."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, turn_timeout=60),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.turn_timeout == 60

    def test_experiment_base_overrides_default_experiment_scalars(self):
        """experiment.defaults scalars (layer 2) should override default_experiment.defaults (layer 1)."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, max_iterations=5),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)

        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(max_iterations=10),
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.max_iterations == 10

    def test_explicit_task_scalar_not_overwritten_by_default_experiment(self):
        """Task that explicitly sets max_iterations should NOT be overwritten by default experiment."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, max_iterations=5),
            variants=[ExperimentVariant(variant_id="default")],
        )
        # Task explicitly sets max_iterations=7 (layer 3 > layers 1-2)
        task = _make_task(agent=None, max_iterations=7)

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.max_iterations == 7

    def test_explicit_task_timeout_not_overwritten_by_default_experiment(self):
        """Task that explicitly sets task_timeout should NOT be overwritten by default experiment."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, task_timeout=600),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None, task_timeout=900)

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.task_timeout == 900

    def test_variant_overrides_default_experiment_scalars(self):
        """variant scalars (layer 4) should override default_experiment.defaults (layer 1)."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, max_iterations=5),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1", max_iterations=2)],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.max_iterations == 2


class TestTurnTimeoutResolution:
    """Regression tests for turn_timeout not being clobbered by scalar path."""

    def test_turn_timeout_in_agent_dict_survives_resolution(self):
        """turn_timeout set inside defaults.agent dict must not be clobbered by scalar path.

        Regression: the scalar override path would unconditionally overwrite
        resolved_agent.turn_timeout with None when turn_timeout was only in the
        agent dict (not at the defaults level).
        """
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "turn_timeout": 300}),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.turn_timeout == 300, f"Expected 300, got {resolved.agent.turn_timeout}"

    def test_default_yaml_turn_timeout_preserved(self):
        """Regression: turn_timeout from actual default.yaml must survive resolution."""
        from coder_eval.orchestration.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment

        default_exp = load_experiment(DEFAULT_EXPERIMENT_PATH)
        expected_timeout = default_exp.defaults.turn_timeout
        assert expected_timeout is not None, "default.yaml should define turn_timeout"

        task = _make_task(agent=None)
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.turn_timeout == expected_timeout

    def test_scalar_turn_timeout_overrides_agent_dict(self):
        """When turn_timeout is set at both defaults level AND in agent dict, scalar wins."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(
                agent={"type": "claude-code", "turn_timeout": 200},
                turn_timeout=400,
            ),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.turn_timeout == 400


class TestTemplateSourcesOverlay:
    """Tests for variant-level template_sources overlay (append semantics)."""

    def test_full_template_sources_precedence_chain(self):
        """Task + base + variant template_sources are concatenated in order."""
        default_exp = _make_default_experiment()
        task = _make_task(
            agent={"type": "claude-code"},
            sandbox={
                "driver": "tempdir",
                "template_sources": [{"type": "template_dir", "path": "/task"}],
            },
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(template_sources=[TemplateDirSource(path="/base")]),
            variants=[
                ExperimentVariant(
                    variant_id="full",
                    template_sources=[TemplateDirSource(path="/variant")],
                )
            ],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.sandbox.template_sources is not None
        paths = [s.path for s in resolved.sandbox.template_sources]
        assert paths == ["/task", "/base", "/variant"]

    def test_no_template_sources_anywhere(self):
        """When no layer has template_sources, sandbox.template_sources is unchanged."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="bare")],
        )

        resolved, _lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.sandbox.template_sources is None

    def test_repo_source_in_variant_after_task_sources_rejected(self):
        """RepoSource in variant overlay is rejected when task already has sources."""
        from coder_eval.models import RepoSource

        default_exp = _make_default_experiment()
        task = _make_task(
            agent={"type": "claude-code"},
            sandbox={
                "driver": "tempdir",
                "template_sources": [{"type": "template_dir", "path": "/base"}],
            },
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="bad",
                    template_sources=[RepoSource(url="https://github.com/example/repo.git")],
                )
            ],
        )

        with pytest.raises(ValueError, match="RepoSource must be the first element"):
            resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
