"""Tests for n_trials expansion in the experiment resolver."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coder_eval.models import (
    ConfigLineageEntry,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    SimulationConfig,
    TaskDefinition,
)
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import _expand_trials, resolve_all_tasks, resolve_task_for_variant


def _base_task_kwargs() -> dict[str, Any]:
    return {
        "task_id": "chat-task",
        "description": "Dialog task",
        "initial_prompt": "Hi there.",
        "agent": {"type": "claude-code"},
        "sandbox": {"driver": "tempdir"},
        "success_criteria": [{"type": "file_exists", "path": "out.txt", "description": "file"}],
    }


def _sim_block(**overrides: Any) -> SimulationConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "persona": "A user",
        "goal": "do it",
        "n_trials": 1,
        "stop_on_criteria_pass": False,
    }
    base.update(overrides)
    return SimulationConfig(**base)


class TestExpandTrials:
    def test_single_trial_uses_zero_padded_subdir(self, tmp_path: Path):
        task = TaskDefinition(**_base_task_kwargs(), simulation=_sim_block(n_trials=1))
        out = _expand_trials(
            resolved_task=task,
            task_file=tmp_path / "task.yaml",
            variant_id="default",
            source_yaml="",
            lineage={},
            base_run_dir=tmp_path / "runs",
        )
        assert len(out) == 1
        # task_id is NOT suffixed — replicate_index disambiguates siblings via path.
        assert out[0].task.task_id == "chat-task"
        assert out[0].replicate_index == 0
        # Layout uses the shared build_task_run_dir helper: <task>/NN
        assert out[0].run_dir == tmp_path / "runs" / "default" / "chat-task" / "00"

    def test_multi_trial_expansion(self, tmp_path: Path):
        task = TaskDefinition(**_base_task_kwargs(), simulation=_sim_block(n_trials=3))
        out = _expand_trials(
            resolved_task=task,
            task_file=tmp_path / "task.yaml",
            variant_id="v1",
            source_yaml="",
            lineage={},
            base_run_dir=tmp_path / "runs",
        )
        assert len(out) == 3
        # All replicates share the same task_id — disambiguation is by
        # replicate_index + path, uniform with the single-shot n=1 case.
        assert [r.task.task_id for r in out] == ["chat-task", "chat-task", "chat-task"]
        assert [r.replicate_index for r in out] == [0, 1, 2]
        run_dirs = [r.run_dir for r in out]
        assert run_dirs == [
            tmp_path / "runs" / "v1" / "chat-task" / "00",
            tmp_path / "runs" / "v1" / "chat-task" / "01",
            tmp_path / "runs" / "v1" / "chat-task" / "02",
        ]

    def test_simulation_disabled_treated_as_single(self, tmp_path: Path):
        """If simulation.enabled=False, n_trials is ignored — it's a single-shot task."""
        task = TaskDefinition(**_base_task_kwargs(), simulation=_sim_block(enabled=False, n_trials=5))
        out = _expand_trials(
            resolved_task=task,
            task_file=tmp_path / "task.yaml",
            variant_id="default",
            source_yaml="",
            lineage={},
            base_run_dir=tmp_path / "runs",
        )
        assert len(out) == 1
        assert out[0].replicate_index == 0
        assert out[0].task.task_id == "chat-task"

    def test_no_simulation_block_single(self, tmp_path: Path):
        task = TaskDefinition(**_base_task_kwargs())
        out = _expand_trials(
            resolved_task=task,
            task_file=tmp_path / "task.yaml",
            variant_id="default",
            source_yaml="",
            lineage={},
            base_run_dir=tmp_path / "runs",
        )
        assert len(out) == 1
        assert out[0].replicate_index == 0

    def test_trials_have_independent_lineage_dicts(self, tmp_path: Path):
        task = TaskDefinition(**_base_task_kwargs(), simulation=_sim_block(n_trials=2))
        shared_lineage = {"marker": ConfigLineageEntry(value="original", source="default")}
        out = _expand_trials(
            resolved_task=task,
            task_file=tmp_path / "task.yaml",
            variant_id="v",
            source_yaml="",
            lineage=shared_lineage,
            base_run_dir=tmp_path / "runs",
        )
        # Mutating one trial's lineage must not affect siblings
        out[0].config_lineage["touched_by_trial_0"] = ConfigLineageEntry(value=1, source="task")
        assert "touched_by_trial_0" not in out[1].config_lineage


class TestSimulationMergeAcrossLayers:
    def _default_exp(self) -> ExperimentDefinition:
        return ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
            variants=[ExperimentVariant(variant_id="default")],
        )

    def test_experiment_defaults_fill_in_when_task_has_none(self):
        """Task without simulation inherits experiment defaults."""
        task = TaskDefinition(**_base_task_kwargs())
        default_exp = self._default_exp()
        exp = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(simulation={"enabled": True, "persona": "exp user", "goal": "exp goal"}),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.simulation is not None
        assert resolved.simulation.enabled is True
        assert resolved.simulation.persona == "exp user"
        assert "simulation" in lineage

    def test_variant_overrides_persona_only(self):
        """Variant overrides merge shallowly with task simulation."""
        task = TaskDefinition(
            **_base_task_kwargs(),
            simulation=_sim_block(persona="task persona", goal="task goal", stop_token="<<<DONE>>>"),
        )
        default_exp = self._default_exp()
        exp = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="terse", simulation={"persona": "terse user"})],
        )
        resolved, *_ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.simulation is not None
        # Variant overrode persona, everything else comes from task
        assert resolved.simulation.persona == "terse user"
        assert resolved.simulation.goal == "task goal"
        assert resolved.simulation.stop_token == "<<<DONE>>>"

    def test_no_simulation_anywhere_stays_none(self):
        """No layer provides simulation — resolved stays None (single-shot)."""
        task = TaskDefinition(**_base_task_kwargs())
        default_exp = self._default_exp()
        exp = ExperimentDefinition(experiment_id="test", variants=[ExperimentVariant(variant_id="v1")])
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.simulation is None
        assert "simulation" not in lineage

    def test_variant_can_enable_simulation_on_plain_task(self):
        """A single experiment can turn a single-shot task into a simulation task."""
        task = TaskDefinition(**_base_task_kwargs())
        default_exp = self._default_exp()
        exp = ExperimentDefinition(
            experiment_id="test",
            variants=[
                ExperimentVariant(
                    variant_id="dialog",
                    simulation={"enabled": True, "persona": "BA", "goal": "ship", "stop_on_criteria_pass": False},
                )
            ],
        )
        resolved, *_ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.simulation is not None
        assert resolved.simulation.enabled is True


class TestResolveAllTasksWithTrials:
    def test_end_to_end_expansion(self, tmp_path: Path):
        """Verify resolve_all_tasks expands trials across the full pipeline."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            """
task_id: dialog-task
description: Dialog test
initial_prompt: start
agent:
  type: claude-code
sandbox:
  driver: tempdir
success_criteria:
  - type: file_exists
    path: out.txt
    description: file
simulation:
  enabled: true
  persona: A user
  goal: do it
  n_trials: 3
  stop_on_criteria_pass: false
""".strip()
        )
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
            variants=[ExperimentVariant(variant_id="default")],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="v1"), ExperimentVariant(variant_id="v2")],
        )
        config = BatchRunConfig(run_dir=tmp_path / "runs")

        resolved = resolve_all_tasks([task_yaml], experiment, default_exp, config)

        # 2 variants * 3 trials = 6 ResolvedTasks
        assert len(resolved) == 6
        by_variant: dict[str, list[int]] = {}
        for rt in resolved:
            by_variant.setdefault(rt.variant_id, []).append(rt.replicate_index)
        assert sorted(by_variant["v1"]) == [0, 1, 2]
        assert sorted(by_variant["v2"]) == [0, 1, 2]

        # Every replicate shares the parent task_id; disambiguation is via
        # (variant_id, replicate_index) and per-replicate run_dir.
        assert {rt.task.task_id for rt in resolved} == {"dialog-task"}
