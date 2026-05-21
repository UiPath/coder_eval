"""Tests for the `--driver` CLI override flowing through the 5-layer resolver.

CLAUDE.md item 10: every new BatchRunConfig field must be exercised across
all 5 merge layers. This file covers `BatchRunConfig.driver`.
"""

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentConfig, AgentKind, FileExistsCriterion, SandboxConfig, TaskDefinition
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import _apply_cli_overrides
from tests._path_helpers import tmp_subdir


def _make_task(driver: str = "tempdir") -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="x",
        initial_prompt="hi",
        agent=AgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver=driver),  # type: ignore[arg-type]
        success_criteria=[FileExistsCriterion(description="x", path="f.txt")],
    )


class TestDriverOverride:
    def test_cli_override_wins_over_task_yaml(self):
        """`--driver docker` flips a `driver: tempdir` task to docker."""
        task = _make_task(driver="tempdir")
        config = BatchRunConfig(run_dir=tmp_subdir("x"), driver="docker")
        _apply_cli_overrides(task, config)
        assert task.sandbox is not None and task.sandbox.driver == "docker"

    def test_no_override_preserves_task_yaml(self):
        """Absent CLI flag → task YAML's driver is left alone."""
        task = _make_task(driver="docker")
        config = BatchRunConfig(run_dir=tmp_subdir("x"))  # driver defaults to None
        _apply_cli_overrides(task, config)
        assert task.sandbox is not None and task.sandbox.driver == "docker"

    def test_lineage_records_cli_source(self):
        """Override is recorded so reports/audits can trace why driver changed."""
        from coder_eval.models import ConfigLineageEntry

        task = _make_task(driver="tempdir")
        config = BatchRunConfig(run_dir=tmp_subdir("x"), driver="docker")
        lineage: dict[str, ConfigLineageEntry] = {}
        _apply_cli_overrides(task, config, lineage=lineage)
        entry = lineage["sandbox.driver"]
        assert entry.value == "docker"
        assert entry.source == "cli"
        assert entry.source_detail == "--driver"

    def test_sandbox_always_present(self):
        """Sandbox is always present (uses default_factory), so it can always be overridden."""
        task = _make_task()
        # Even a minimal task without explicit sandbox has it set by default_factory
        assert task.sandbox is not None
        config = BatchRunConfig(run_dir=tmp_subdir("x"), driver="docker")
        # Override should succeed since sandbox is always present
        _apply_cli_overrides(task, config)
        assert task.sandbox.driver == "docker"

    def test_invalid_driver_value_rejected_at_config_layer(self):
        """`BatchRunConfig.driver` is Literal-typed; bogus values fail Pydantic validation."""
        with pytest.raises(ValidationError):
            BatchRunConfig(run_dir=tmp_subdir("x"), driver="bogus")  # type: ignore[arg-type]


class TestExperimentLayerDriver:
    """Layers 2 and 4 (experiment defaults and variant) must be able to set driver.

    Without this, `variant A in tempdir, variant B in docker` comparisons are
    impossible — see M1 from the multi-model review.
    """

    def _resolve(self, task: TaskDefinition, *, defaults_driver=None, variant_driver=None):
        from coder_eval.models import ExperimentDefaults, ExperimentDefinition, ExperimentVariant
        from coder_eval.orchestration.experiment import resolve_task_for_variant

        empty_default_exp = ExperimentDefinition(
            experiment_id="empty",
            variants=[ExperimentVariant(variant_id="v")],
        )
        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(driver=defaults_driver) if defaults_driver else None,
            variants=[ExperimentVariant(variant_id="v", driver=variant_driver)],
        )
        return resolve_task_for_variant(
            default_experiment=empty_default_exp,
            task=task,
            experiment=experiment,
            variant=experiment.variants[0],
        )

    def test_variant_driver_wins_over_task(self):
        task = _make_task(driver="tempdir")
        resolved, _, _ = self._resolve(task, variant_driver="docker")
        assert resolved.sandbox is not None and resolved.sandbox.driver == "docker"

    def test_experiment_defaults_driver_applied_when_task_did_not_set_it(self):
        task = _make_task()
        # Strip the model_fields_set marker by reconstructing without driver explicit
        task.sandbox = SandboxConfig()  # driver defaults to "tempdir", not "set"
        resolved, _, _ = self._resolve(task, defaults_driver="docker")
        assert resolved.sandbox is not None and resolved.sandbox.driver == "docker"

    def test_variant_driver_wins_over_experiment_defaults(self):
        task = _make_task()
        task.sandbox = SandboxConfig()
        resolved, lineage, _ = self._resolve(task, defaults_driver="docker", variant_driver="tempdir")
        assert resolved.sandbox is not None and resolved.sandbox.driver == "tempdir"
        assert lineage["sandbox.driver"].source == "variant"
