"""End-to-end tests for config lineage tracking across all 5 layers."""

from pathlib import Path
from unittest.mock import patch

import yaml

from coder_eval.models import (
    ConfigLineageEntry,
    ExperimentBase,
    ExperimentDefinition,
    ExperimentVariant,
)
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import (
    _apply_cli_overrides,
    _build_agent_lineage,
    resolve_all_tasks,
    resolve_task_for_variant,
)
from coder_eval.orchestration.task_loader import load_task


def _write_task_yaml(path: Path, task_id: str, agent: dict | None = None, **extras) -> Path:
    """Write a minimal task YAML file."""
    data = {
        "task_id": task_id,
        "description": f"Test task {task_id}",
        "initial_prompt": "Do something",
        "sandbox": {"driver": "tempdir"},
        "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "File exists"}],
    }
    if agent:
        data["agent"] = agent
    data.update(extras)
    task_file = path / f"{task_id}.yaml"
    task_file.write_text(yaml.dump(data))
    return task_file


def _make_default_experiment() -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="default",
        base=ExperimentBase(agent={"type": "claude-code", "permission_mode": "acceptEdits", "max_turns": 3}),
        variants=[ExperimentVariant(variant_id="default")],
    )


class TestBuildAgentLineage:
    def test_single_layer(self):
        lineage = _build_agent_lineage([("default", {"type": "claude-code", "model": "sonnet"})])
        assert lineage["agent.type"].source == "default"
        assert lineage["agent.model"].value == "sonnet"

    def test_later_layer_wins(self):
        lineage = _build_agent_lineage(
            [
                ("default", {"type": "claude-code", "model": "sonnet"}),
                ("variant", {"model": "opus"}),
            ]
        )
        assert lineage["agent.type"].source == "default"
        assert lineage["agent.model"].source == "variant"
        assert lineage["agent.model"].value == "opus"

    def test_none_layers_skipped(self):
        lineage = _build_agent_lineage(
            [
                ("default", {"type": "claude-code"}),
                ("task", None),
                ("variant", {"model": "opus"}),
            ]
        )
        assert len(lineage) == 2
        assert "agent.type" in lineage
        assert "agent.model" in lineage


class TestScalarLineage:
    """Scalar lineage is now tracked inline within resolve_task_for_variant."""

    def _no_scalars_default_experiment(self) -> ExperimentDefinition:
        """Default experiment with no scalar overrides (only agent config)."""
        return ExperimentDefinition(
            experiment_id="default",
            base=ExperimentBase(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="default")],
        )

    def test_task_only(self):
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
            max_iterations=3,
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        _resolved, lineage = resolve_task_for_variant(
            self._no_scalars_default_experiment(), task, experiment, experiment.variants[0]
        )
        assert lineage["max_iterations"].source == "task"
        assert lineage["max_iterations"].value == 3

    def test_variant_overrides_experiment_base(self):
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
            max_iterations=1,
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            base=ExperimentBase(max_iterations=5, task_timeout=300),
            variants=[ExperimentVariant(variant_id="v1", max_iterations=2)],
        )
        _resolved, lineage = resolve_task_for_variant(
            self._no_scalars_default_experiment(), task, experiment, experiment.variants[0]
        )
        assert lineage["max_iterations"].source == "variant"
        assert lineage["max_iterations"].value == 2
        assert lineage["task_timeout"].source == "experiment-base"
        assert lineage["task_timeout"].value == 300

    def test_pydantic_default_not_tracked(self):
        """Scalars using Pydantic defaults (not explicitly set) should not appear in lineage."""
        from coder_eval.models import TaskDefinition

        # max_iterations NOT passed — uses Pydantic default of 3
        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        _resolved, lineage = resolve_task_for_variant(
            self._no_scalars_default_experiment(), task, experiment, experiment.variants[0]
        )
        assert "max_iterations" not in lineage
        assert "task_timeout" not in lineage
        assert "turn_timeout" not in lineage

    def test_explicit_null_task_timeout_tracked(self):
        """Explicitly setting task_timeout to null should appear in lineage, overriding default."""
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
            task_timeout=None,
        )
        default_exp = ExperimentDefinition(
            experiment_id="default",
            base=ExperimentBase(agent={"type": "claude-code"}, task_timeout=600),
            variants=[ExperimentVariant(variant_id="default")],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.task_timeout is None
        assert lineage["task_timeout"].source == "task"
        assert lineage["task_timeout"].value is None

    def test_default_experiment_scalars_tracked(self):
        """Default experiment scalar overrides appear in lineage as source='default'."""
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
        )
        default_exp = ExperimentDefinition(
            experiment_id="default",
            base=ExperimentBase(agent={"type": "claude-code"}, max_iterations=5, turn_timeout=300),
            variants=[ExperimentVariant(variant_id="default")],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        _resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert lineage["max_iterations"].source == "default"
        assert lineage["max_iterations"].value == 5
        assert lineage["turn_timeout"].source == "default"
        assert lineage["turn_timeout"].value == 300

    def test_task_overrides_default_experiment(self):
        """Explicitly-set task scalars override default experiment scalars."""
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
            max_iterations=10,
        )
        default_exp = ExperimentDefinition(
            experiment_id="default",
            base=ExperimentBase(agent={"type": "claude-code"}, max_iterations=5),
            variants=[ExperimentVariant(variant_id="default")],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        _resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert lineage["max_iterations"].source == "task"
        assert lineage["max_iterations"].value == 10


class TestResolveTaskForVariantLineage:
    def test_task_only_all_from_default(self):
        """Task with no agent — all agent keys from default experiment."""
        default_exp = _make_default_experiment()
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        _resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert lineage["agent.type"].source == "default"
        assert lineage["agent.permission_mode"].source == "default"

    def test_multi_layer_cascade(self):
        """Each layer overrides the previous for agent keys."""
        default_exp = _make_default_experiment()
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            agent={"type": "claude-code", "permission_mode": "bypassPermissions"},
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            base=ExperimentBase(agent={"model": "base-model"}),
            variants=[ExperimentVariant(variant_id="v1", agent={"model": "variant-model"})],
        )
        _resolved, lineage = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert lineage["agent.type"].source == "task"
        assert lineage["agent.permission_mode"].source == "task"
        assert lineage["agent.model"].source == "variant"
        assert lineage["agent.model"].value == "variant-model"


class TestApplyCliOverridesLineage:
    def test_cli_model_override(self):
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            agent={"type": "claude-code"},
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
        )
        lineage: dict[str, ConfigLineageEntry] = {}
        config = BatchRunConfig(run_dir=Path("/tmp/run"), max_parallel=1, agent_model="opus-override")
        with patch("coder_eval.config.settings") as mock_settings:
            mock_settings.default_agent_model = None
            mock_settings.default_permission_mode = None
            mock_settings.default_max_turns = None
            _apply_cli_overrides(task, config, lineage)
        assert lineage["agent.model"].source == "cli"
        assert lineage["agent.model"].source_detail == "--model"
        assert lineage["agent.model"].value == "opus-override"

    def test_snapshot_overrides_tracked(self):
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            agent={"type": "claude-code"},
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
        )
        lineage: dict[str, ConfigLineageEntry] = {}
        config = BatchRunConfig(
            run_dir=Path("/tmp/run"), max_parallel=1, snapshot_mode="hybrid", snapshot_checkpoint_freq=2
        )
        with patch("coder_eval.config.settings") as mock_settings:
            mock_settings.default_agent_model = None
            mock_settings.default_permission_mode = None
            mock_settings.default_max_turns = None
            _apply_cli_overrides(task, config, lineage)
        assert lineage["sandbox.snapshots.mode"].source == "cli"
        assert lineage["sandbox.snapshots.mode"].source_detail == "--snapshot-mode"
        assert lineage["sandbox.snapshots.checkpoint_frequency"].source == "cli"
        assert lineage["sandbox.snapshots.checkpoint_frequency"].value == 2

    def test_env_model_override(self):
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do",
            agent={"type": "claude-code"},
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "t.py", "description": "x"}],
        )
        lineage: dict[str, ConfigLineageEntry] = {}
        config = BatchRunConfig(run_dir=Path("/tmp/run"), max_parallel=1)
        with patch("coder_eval.config.settings") as mock_settings:
            mock_settings.default_agent_model = "env-model"
            mock_settings.default_permission_mode = None
            mock_settings.default_max_turns = None
            _apply_cli_overrides(task, config, lineage)
        assert lineage["agent.model"].source == "cli"
        assert lineage["agent.model"].source_detail == ".env DEFAULT_AGENT_MODEL"
        assert lineage["agent.model"].value == "env-model"


class TestResolveAllTasksLineage:
    def test_source_yaml_and_lineage_on_resolved_task(self, tmp_path):
        """resolve_all_tasks populates source_yaml and config_lineage on ResolvedTask."""
        task_file = _write_task_yaml(tmp_path, "task-a", agent={"type": "claude-code"})
        default_exp = _make_default_experiment()
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        run_dir = tmp_path / "runs" / "test-run"
        run_dir.mkdir(parents=True)
        config = BatchRunConfig(run_dir=run_dir, max_parallel=1)

        with patch("coder_eval.config.settings") as mock_settings:
            mock_settings.default_agent_model = None
            mock_settings.default_permission_mode = None
            mock_settings.default_max_turns = None
            resolved = resolve_all_tasks(
                task_files=[task_file],
                experiment=experiment,
                default_experiment=default_exp,
                config=config,
            )

        assert len(resolved) == 1
        rt = resolved[0]
        assert rt.source_yaml != ""
        assert "task-a" in rt.source_yaml
        assert len(rt.config_lineage) > 0
        # Lineage stored as ConfigLineageEntry objects
        assert "agent.type" in rt.config_lineage
        assert rt.config_lineage["agent.type"].value == "claude-code"


class TestLoadTaskReturnsYaml:
    def test_returns_tuple(self, tmp_path):
        """load_task returns (TaskDefinition, raw_yaml_str)."""
        task_file = _write_task_yaml(tmp_path, "task-x", agent={"type": "claude-code"})
        task, raw_yaml = load_task(task_file)
        assert task.task_id == "task-x"
        assert "task-x" in raw_yaml
        assert isinstance(raw_yaml, str)
