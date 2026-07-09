"""Unit tests for `_build_sandbox_layers` — the sandbox synthetic-layer builder.

`_build_sandbox_layers` is the named helper extracted from
`resolve_task_for_variant`. It encodes two subtleties the merge engine itself
does not: (1) the experiment/variant TOP-LEVEL `driver` field is layered AFTER
the nested `sandbox.driver` dumps so it overrides a nested driver while a
nested-only driver still contributes, and (2) `template_sources` is popped from
every sandbox dump and re-added as dedicated synthetic layers in TASK-FIRST
append order (task base → experiment-defaults overlay → variant overlay), which
is NOT the same as layer-precedence order.

These tests assert resolved behavior by feeding the helper's output through the
same `resolve_root("sandbox", …)` call the production code uses.
"""

from coder_eval.models import (
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    SandboxConfig,
    TaskDefinition,
    TemplateDirSource,
)
from coder_eval.orchestration.config_merge import resolve_root
from coder_eval.orchestration.experiment import _build_sandbox_layers


def _make_task(sandbox: dict | None = None) -> TaskDefinition:
    """Minimal task; `sandbox` overrides the default `{driver: tempdir}` block."""
    return TaskDefinition(
        task_id="test-task",
        description="Test task",
        initial_prompt="Do something",
        sandbox=sandbox if sandbox is not None else {"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "test.py", "description": "File exists"}],
    )


def _empty_default_experiment() -> ExperimentDefinition:
    return ExperimentDefinition(experiment_id="default", variants=[ExperimentVariant(variant_id="default")])


def _resolve_sandbox(
    default_experiment: ExperimentDefinition,
    experiment: ExperimentDefinition,
    task: TaskDefinition,
    variant: ExperimentVariant,
) -> SandboxConfig:
    """Build sandbox layers and resolve them exactly as the production call site does."""
    layers = _build_sandbox_layers(default_experiment, experiment, task, variant)
    resolved = resolve_root("sandbox", layers) or task.sandbox
    assert resolved is not None
    return resolved


class TestDriverPrecedence:
    def test_variant_top_level_beats_task_beats_exp_defaults_beats_nested(self):
        """Full driver precedence chain: variant > task > exp-defaults top-level > nested.

        exp-defaults supplies a nested `sandbox.driver` AND a top-level `driver`
        (top-level wins). The task sets its own driver, and the variant's
        top-level driver wins over everything.
        """
        default_exp = _empty_default_experiment()
        # Nested driver on exp-defaults.sandbox is the lowest contributor; its
        # top-level driver overrides it; the task overrides that; variant wins.
        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(
                sandbox=SandboxConfig(driver="tempdir"),
                driver="docker",
            ),
            variants=[ExperimentVariant(variant_id="v", driver="tempdir")],
        )
        task = _make_task(sandbox={"driver": "docker"})
        variant = experiment.variants[0]

        resolved = _resolve_sandbox(default_exp, experiment, task, variant)
        assert resolved.driver == variant.driver == "tempdir"

    def test_task_driver_beats_exp_defaults_top_level(self):
        """With no variant driver, the task's driver wins over exp-defaults top-level."""
        default_exp = _empty_default_experiment()
        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(driver="tempdir"),
            variants=[ExperimentVariant(variant_id="v")],
        )
        task = _make_task(sandbox={"driver": "docker"})
        resolved = _resolve_sandbox(default_exp, experiment, task, experiment.variants[0])
        assert resolved.driver == task.sandbox.driver == "docker"

    def test_nested_only_exp_defaults_driver_still_contributes(self):
        """A nested-only `experiment.defaults.sandbox.driver` (no top-level driver)
        still contributes when the task did not set its own driver."""
        default_exp = _empty_default_experiment()
        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(sandbox=SandboxConfig(driver="docker")),
            variants=[ExperimentVariant(variant_id="v")],
        )
        # Task sandbox left at the default (driver unset → not in exclude_unset dump).
        task = _make_task(sandbox={})
        resolved = _resolve_sandbox(default_exp, experiment, task, experiment.variants[0])
        assert resolved.driver == "docker"

    def test_variant_driver_layer_carries_variant_id_detail(self):
        """The variant driver layer records `detail=variant.variant_id`."""
        default_exp = _empty_default_experiment()
        experiment = ExperimentDefinition(
            experiment_id="exp",
            variants=[ExperimentVariant(variant_id="my-variant", driver="docker")],
        )
        task = _make_task()
        layers = _build_sandbox_layers(default_exp, experiment, task, experiment.variants[0])
        driver_layers = [
            layer for layer in layers if layer.source == "variant" and layer.patch.get("driver") is not None
        ]
        assert len(driver_layers) == 1
        assert driver_layers[0].detail == "my-variant"


class TestTemplateSourcesOrdering:
    def test_resolves_task_first(self):
        """template_sources resolve task-first: task → exp-defaults → variant,
        regardless of layer precedence."""
        default_exp = _empty_default_experiment()
        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(template_sources=[TemplateDirSource(path="/exp")]),
            variants=[ExperimentVariant(variant_id="v", template_sources=[TemplateDirSource(path="/variant")])],
        )
        task = _make_task(
            sandbox={"driver": "tempdir", "template_sources": [{"type": "template_dir", "path": "/task"}]}
        )
        resolved = _resolve_sandbox(default_exp, experiment, task, experiment.variants[0])
        assert resolved.template_sources is not None
        paths = [s.path for s in resolved.template_sources]
        assert paths == ["/task", "/exp", "/variant"]

    def test_no_template_sources_anywhere_resolves_none(self):
        """When no layer sets template_sources, the resolved list is None."""
        default_exp = _empty_default_experiment()
        experiment = ExperimentDefinition(experiment_id="exp", variants=[ExperimentVariant(variant_id="v")])
        task = _make_task(sandbox={"driver": "tempdir"})
        resolved = _resolve_sandbox(default_exp, experiment, task, experiment.variants[0])
        assert resolved.template_sources is None


class TestEnvPassthroughExtraAppend:
    def test_appends_across_layers(self):
        """`docker.env_passthrough_extra` appends in layer order via its field
        strategy (not via a synthetic layer)."""
        default_exp = _empty_default_experiment()
        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(
                sandbox=SandboxConfig(docker={"env_passthrough_extra": ["FROM_EXP"]}),  # type: ignore[arg-type]
            ),
            variants=[ExperimentVariant(variant_id="v")],
        )
        task = _make_task(sandbox={"driver": "docker", "docker": {"env_passthrough_extra": ["FROM_TASK"]}})
        resolved = _resolve_sandbox(default_exp, experiment, task, experiment.variants[0])
        # exp-defaults layer precedes the task layer → exp entry first, task appended.
        assert resolved.docker.env_passthrough_extra == ["FROM_EXP", "FROM_TASK"]
