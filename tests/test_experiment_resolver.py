"""Tests for experiment config resolution (merge logic)."""

import pytest

from coder_eval.models import (
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    PostRunCommand,
    PreRunCommand,
    PromptPrefix,
    PromptReplace,
    PromptSuffix,
    PromptTemplate,
    TaskDefinition,
    TemplateDirSource,
)
from coder_eval.orchestration.experiment import _apply_prompt_overrides, resolve_task_for_variant


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

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.permission_mode == "bypassPermissions"
        assert lineage["agent.permission_mode"].source == "task"

    def test_task_model_not_clobbered_by_default_experiment(self):
        """Task model should not be overridden when experiment IS the default (no --experiment flag)."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "model": "custom-model"})
        # When no --experiment is given, experiment == default_experiment
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, default_exp, default_exp.variants[0])
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

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.disallowed_tools == ["Bash"]
        assert lineage["agent.disallowed_tools"].source == "task"

    def test_scalar_overrides(self):
        """Scalar fields (task_timeout, turn_timeout) resolve through precedence under run_limits."""
        from coder_eval.models import RunLimits

        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, run_limits=RunLimits(task_timeout=900))
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(run_limits=RunLimits(task_timeout=300, turn_timeout=60)),
            variants=[
                ExperimentVariant(variant_id="fast", run_limits=RunLimits(task_timeout=120, turn_timeout=30)),
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.task_timeout == 120  # variant wins
        assert lineage["run_limits.task_timeout"].source == "variant"
        assert lineage["run_limits.turn_timeout"].source == "variant"

    def test_resolved_task_preserves_non_agent_fields(self):
        """Resolution should not alter task_id, description, criteria, sandbox, etc."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, task_id="my-task", description="My test")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.task_id == "my-task"
        assert resolved.description == "My test"
        assert len(resolved.success_criteria) == 1


class TestDefaultExperimentScalarOverrides:
    """Tests for layer-1 default experiment scalar resolution under run_limits."""

    def test_default_experiment_task_timeout_applied(self):
        """default.yaml base.run_limits.task_timeout should be applied when task has no override."""
        from coder_eval.models import RunLimits

        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, run_limits=RunLimits(task_timeout=600)),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.task_timeout == 600

    def test_default_experiment_turn_timeout_applied(self):
        """default.yaml base.run_limits.turn_timeout should be applied."""
        from coder_eval.models import RunLimits

        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, run_limits=RunLimits(turn_timeout=60)),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.turn_timeout == 60

    def test_experiment_base_overrides_default_experiment_scalars(self):
        """experiment.defaults run_limits (layer 2) should override default_experiment.defaults (layer 1)."""
        from coder_eval.models import RunLimits

        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, run_limits=RunLimits(task_timeout=300)),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)

        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(run_limits=RunLimits(task_timeout=600)),
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.task_timeout == 600

    def test_explicit_task_timeout_not_overwritten_by_default_experiment(self):
        """Task that explicitly sets task_timeout should NOT be overwritten by default experiment."""
        from coder_eval.models import RunLimits

        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, run_limits=RunLimits(task_timeout=600)),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None, run_limits=RunLimits(task_timeout=900))

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.task_timeout == 900

    def test_variant_overrides_default_experiment_scalars(self):
        """variant scalars (layer 4) should override default_experiment.defaults (layer 1)."""
        from coder_eval.models import RunLimits

        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}, run_limits=RunLimits(task_timeout=300)),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)

        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1", run_limits=RunLimits(task_timeout=120))],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.task_timeout == 120


class TestTurnTimeoutResolution:
    """Regression tests for turn_timeout flowing through the field-merge resolver."""

    def test_turn_timeout_in_agent_dict_hoists_to_run_limits(self):
        """Legacy turn_timeout inside the agent dict hoists into run_limits.turn_timeout."""
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

        with pytest.warns(DeprecationWarning):
            resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.turn_timeout == 300

    def test_default_yaml_turn_timeout_preserved(self):
        """Regression: turn_timeout from actual default.yaml must survive resolution."""
        from coder_eval.orchestration.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment

        default_exp = load_experiment(DEFAULT_EXPERIMENT_PATH)
        assert default_exp.defaults is not None and default_exp.defaults.run_limits is not None
        expected_timeout = default_exp.defaults.run_limits.turn_timeout
        assert expected_timeout is not None, "default.yaml should define run_limits.turn_timeout"

        task = _make_task(agent=None)
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.turn_timeout == expected_timeout

    def test_run_limits_turn_timeout_overrides_agent_dict_in_same_layer(self):
        """run_limits.turn_timeout merged after agent-hoist within the same layer wins."""
        from coder_eval.models import RunLimits

        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(
                agent={"type": "claude-code", "turn_timeout": 200},
                run_limits=RunLimits(turn_timeout=400),
            ),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(agent=None)
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )

        with pytest.warns(DeprecationWarning):
            resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.run_limits is not None
        # agent-deprecated is merged BEFORE run_limits within the same layer, so run_limits wins.
        assert resolved.run_limits.turn_timeout == 400


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

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
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


class TestPostRunMerge:
    """Tests for experiment-level post_run defaults appended to task post_run."""

    def test_experiment_defaults_appended_after_task(self):
        """Task post_run runs first, then experiment defaults (cleanup-last semantics)."""
        default_exp = _make_default_experiment()
        task = _make_task(
            agent={"type": "claude-code"},
            post_run=[PostRunCommand(command="echo task-1"), PostRunCommand(command="echo task-2")],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(post_run=[PostRunCommand(command="echo cleanup")]),
            variants=[ExperimentVariant(variant_id="default")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert [p.command for p in resolved.post_run] == ["echo task-1", "echo task-2", "echo cleanup"]

    def test_experiment_defaults_only(self):
        """A task with no post_run still picks up experiment-defaults post_run."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(post_run=[PostRunCommand(command="echo cleanup")]),
            variants=[ExperimentVariant(variant_id="default")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert [p.command for p in resolved.post_run] == ["echo cleanup"]

    def test_no_post_run_anywhere(self):
        """When neither task nor defaults declare post_run, the resolved list is empty."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="default")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.post_run == []


class TestPreRunMerge:
    """Tests for experiment-level pre_run defaults prepended before task pre_run."""

    def test_experiment_defaults_prepended_before_task(self):
        """Experiment defaults run first (baseline setup), then task commands."""
        default_exp = _make_default_experiment()
        task = _make_task(
            agent={"type": "claude-code"},
            pre_run=[PreRunCommand(command="echo seed")],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(pre_run=[PreRunCommand(command="echo setup")]),
            variants=[ExperimentVariant(variant_id="default")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert [p.command for p in resolved.pre_run] == ["echo setup", "echo seed"]

    def test_experiment_defaults_only(self):
        """A task with no pre_run still picks up experiment-defaults pre_run."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(pre_run=[PreRunCommand(command="echo setup")]),
            variants=[ExperimentVariant(variant_id="default")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert [p.command for p in resolved.pre_run] == ["echo setup"]

    def test_task_pre_run_only(self):
        """When experiment has no defaults.pre_run, only task pre_run is used."""
        default_exp = _make_default_experiment()
        task = _make_task(
            agent={"type": "claude-code"},
            pre_run=[PreRunCommand(command="echo seed")],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="default")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert [p.command for p in resolved.pre_run] == ["echo seed"]

    def test_no_pre_run_anywhere(self):
        """When neither task nor defaults declare pre_run, the resolved list is empty."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="default")],
        )

        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.pre_run == []


class TestPromptMutations:
    """Tests for prompt mutation resolution via _apply_prompt_overrides."""

    def test_variant_prefix_mutation_applied(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="Do something")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="prefixed",
                    prompt_mutations=[PromptPrefix(content="Think step by step.")],
                )
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert resolved.initial_prompt == "Think step by step.\n\nDo something"

    def test_variant_suffix_mutation(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="Do something")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="suffixed",
                    prompt_mutations=[PromptSuffix(content="Include type hints.")],
                )
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert resolved.initial_prompt == "Do something\n\nInclude type hints."

    def test_variant_replace_mutation(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="Create a Python file")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="replaced",
                    prompt_mutations=[PromptReplace(pattern="Create", replacement="Write a minimal")],
                )
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert resolved.initial_prompt == "Write a minimal a Python file"

    def test_variant_template_mutation(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="Create a {language} file")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="templated",
                    prompt_mutations=[PromptTemplate(variables={"language": "Rust"})],
                )
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert resolved.initial_prompt == "Create a Rust file"

    def test_defaults_mutations_applied_before_variant(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="base")
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(
                prompt_mutations=[PromptPrefix(content="DEFAULT")],
            ),
            variants=[
                ExperimentVariant(
                    variant_id="combined",
                    prompt_mutations=[PromptSuffix(content="VARIANT")],
                )
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        # defaults prefix first, then variant suffix
        assert resolved.initial_prompt == "DEFAULT\n\nbase\n\nVARIANT"

    def test_variant_initial_prompt_overrides_task_prompt(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="original prompt")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="override", initial_prompt="completely new prompt")],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert resolved.initial_prompt == "completely new prompt"

    def test_variant_initial_prompt_skips_defaults_mutations(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="original")
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(
                prompt_mutations=[PromptPrefix(content="THIS SHOULD NOT APPEAR")],
            ),
            variants=[ExperimentVariant(variant_id="override", initial_prompt="replacement")],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert resolved.initial_prompt == "replacement"
        assert "THIS SHOULD NOT APPEAR" not in resolved.initial_prompt

    def test_no_mutations_preserves_prompt(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="unchanged prompt")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="baseline")],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert resolved.initial_prompt == "unchanged prompt"

    def test_multiple_mutations_compose(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="Create a file")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="multi",
                    prompt_mutations=[
                        PromptPrefix(content="STEP1", separator=": "),
                        PromptReplace(pattern="file", replacement="script"),
                        PromptSuffix(content="STEP3", separator=". "),
                    ],
                )
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        # prefix -> "STEP1: Create a file" -> replace -> "STEP1: Create a script"
        # -> suffix -> "STEP1: Create a script. STEP3"
        assert resolved.initial_prompt == "STEP1: Create a script. STEP3"

    def test_prompt_mutation_lineage_tracking(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="base")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="tracked",
                    prompt_mutations=[PromptPrefix(content="pre")],
                )
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert "initial_prompt" in lineage
        assert lineage["initial_prompt"].source == "mutation"
        assert "prefix" in lineage["initial_prompt"].source_detail

    def test_variant_initial_prompt_lineage(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="original")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="override", initial_prompt="new")],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert "initial_prompt" in lineage
        assert lineage["initial_prompt"].source == "variant"

    def test_regex_replace_mutation(self):
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, initial_prompt="version 123 build 456")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="regex",
                    prompt_mutations=[PromptReplace(pattern=r"\d+", replacement="N", regex=True)],
                )
            ],
        )

        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        _apply_prompt_overrides(resolved, experiment, experiment.variants[0], lineage)
        assert resolved.initial_prompt == "version N build N"


class TestRepeatsPrecedence:
    def _make_default_experiment(self) -> ExperimentDefinition:
        return ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
            variants=[ExperimentVariant(variant_id="default")],
        )

    def _make_task(self) -> TaskDefinition:
        return TaskDefinition(
            task_id="t1",
            description="Test",
            initial_prompt="Do something",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
        )

    def test_default_when_nothing_set(self, tmp_path):
        from coder_eval.orchestration.config import BatchRunConfig

        default_exp = self._make_default_experiment()
        task = self._make_task()
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        config = BatchRunConfig(run_dir=tmp_path)
        _resolved, lineage, effective_repeats = resolve_task_for_variant(
            default_exp, task, experiment, experiment.variants[0], config
        )
        assert effective_repeats == 1
        assert lineage["repeats"].source == "default"

    def test_experiment_defaults_wins_over_default(self, tmp_path):
        from coder_eval.orchestration.config import BatchRunConfig

        default_exp = self._make_default_experiment()
        task = self._make_task()
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(repeats=2),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        config = BatchRunConfig(run_dir=tmp_path)
        _resolved, lineage, effective_repeats = resolve_task_for_variant(
            default_exp, task, experiment, experiment.variants[0], config
        )
        assert effective_repeats == 2
        assert lineage["repeats"].source == "experiment-defaults"

    def test_variant_wins_over_experiment_defaults(self, tmp_path):
        from coder_eval.orchestration.config import BatchRunConfig

        default_exp = self._make_default_experiment()
        task = self._make_task()
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(repeats=2),
            variants=[ExperimentVariant(variant_id="v1", repeats=5)],
        )
        config = BatchRunConfig(run_dir=tmp_path)
        _resolved, lineage, effective_repeats = resolve_task_for_variant(
            default_exp, task, experiment, experiment.variants[0], config
        )
        assert effective_repeats == 5
        assert lineage["repeats"].source == "variant"

    def test_cli_wins_over_variant(self, tmp_path):
        from coder_eval.orchestration.config import BatchRunConfig

        default_exp = self._make_default_experiment()
        task = self._make_task()
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1", repeats=5)],
        )
        config = BatchRunConfig(run_dir=tmp_path, repeats=1)
        _resolved, lineage, effective_repeats = resolve_task_for_variant(
            default_exp, task, experiment, experiment.variants[0], config
        )
        assert effective_repeats == 1
        assert lineage["repeats"].source == "cli"

    def test_lineage_records_source(self, tmp_path):
        from coder_eval.orchestration.config import BatchRunConfig

        default_exp = self._make_default_experiment()
        task = self._make_task()
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(repeats=3),
            variants=[ExperimentVariant(variant_id="v1", repeats=7)],
        )
        config = BatchRunConfig(run_dir=tmp_path, repeats=2)
        _resolved, lineage, effective_repeats = resolve_task_for_variant(
            default_exp, task, experiment, experiment.variants[0], config
        )
        assert lineage["repeats"].source == "cli"
        assert lineage["repeats"].value == 2
        assert effective_repeats == 2

    def test_rejects_repeats_over_99(self, tmp_path):
        from coder_eval.orchestration.config import BatchRunConfig

        default_exp = self._make_default_experiment()
        task = self._make_task()
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        config = BatchRunConfig(run_dir=tmp_path, repeats=100)
        with pytest.raises(ValueError, match="repeats must be <= 99"):
            resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0], config)


class TestSandboxFieldMerge:
    """Tests for sandbox config field-merge across experiment layers."""

    @staticmethod
    def _make_default_experiment() -> ExperimentDefinition:
        return ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="default")],
        )

    @staticmethod
    def _make_task(**kwargs) -> TaskDefinition:
        from coder_eval.models import FileExistsCriterion, SandboxConfig

        defaults = {
            "task_id": "test-task",
            "description": "Test task",
            "initial_prompt": "Do something",
            "sandbox": SandboxConfig(driver="tempdir"),
            "success_criteria": [FileExistsCriterion(path="test.py", description="File exists")],
        }
        defaults.update(kwargs)
        return TaskDefinition(**defaults)

    def test_experiment_defaults_sandbox_merges_with_task(self, tmp_path):
        """env_passthrough_extra from experiment defaults appends to task values."""
        from coder_eval.models import DockerDriverConfig, SandboxConfig
        from coder_eval.orchestration.config import BatchRunConfig

        default_exp = self._make_default_experiment()
        task = self._make_task(
            sandbox=SandboxConfig(
                driver="docker",
                docker=DockerDriverConfig(env_passthrough_extra=["TASK_VAR"]),
            )
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(
                sandbox=SandboxConfig(docker=DockerDriverConfig(env_passthrough_extra=["EXP_VAR"]))
            ),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        config = BatchRunConfig(run_dir=tmp_path)
        resolved_task, _, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0], config)
        # Both experiment and task extras should be present
        assert "EXP_VAR" in resolved_task.sandbox.docker.env_passthrough_extra
        assert "TASK_VAR" in resolved_task.sandbox.docker.env_passthrough_extra
        # Experiment defaults come first, then task
        assert resolved_task.sandbox.docker.env_passthrough_extra == ["EXP_VAR", "TASK_VAR"]

    def test_task_sandbox_overrides_experiment_other_fields(self, tmp_path):
        """Task-level sandbox fields override experiment defaults."""
        from coder_eval.models import ResourceLimits, SandboxConfig
        from coder_eval.orchestration.config import BatchRunConfig

        default_exp = self._make_default_experiment()
        task = self._make_task(sandbox=SandboxConfig(driver="docker", limits=ResourceLimits(timeout=200)))
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(sandbox=SandboxConfig(limits=ResourceLimits(timeout=100))),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        config = BatchRunConfig(run_dir=tmp_path)
        resolved_task, _, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0], config)
        # Task timeout wins over experiment default
        assert resolved_task.sandbox.limits.timeout == 200
