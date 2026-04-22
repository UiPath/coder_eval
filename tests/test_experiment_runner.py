"""Tests for experiment orchestration standalone functions."""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from coder_eval.models import (
    EvaluationResult,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentResult,
    ExperimentVariant,
    ResolvedTask,
    TaskResult,
)
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import aggregate_results, resolve_all_tasks


def _write_task_yaml(path: Path, task_id: str, agent: dict | None = None) -> Path:
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
    task_file = path / f"{task_id}.yaml"
    task_file.write_text(yaml.dump(data))
    return task_file


def _make_config(run_dir: Path, **overrides) -> BatchRunConfig:
    """Create a BatchRunConfig with sensible defaults for testing."""
    defaults = {"run_dir": run_dir, "max_parallel": 1, "preserve_sandbox": False}
    defaults.update(overrides)
    return BatchRunConfig(**defaults)


class TestResolveAllTasks:
    @pytest.fixture
    def run_dir(self, tmp_path):
        d = tmp_path / "runs" / "test-run"
        d.mkdir(parents=True)
        return d

    @pytest.fixture
    def default_experiment(self):
        return ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
            variants=[ExperimentVariant(variant_id="default")],
        )

    def test_generates_correct_task_count(self, tmp_path, run_dir, default_experiment):
        """2 tasks x 3 variants = 6 resolved tasks."""
        task1 = _write_task_yaml(tmp_path, "task-a", agent={"type": "claude-code"})
        task2 = _write_task_yaml(tmp_path, "task-b", agent={"type": "claude-code"})

        experiment = ExperimentDefinition(
            experiment_id="test-exp",
            defaults=ExperimentDefaults(agent={"permission_mode": "bypassPermissions"}),
            variants=[
                ExperimentVariant(variant_id="sonnet", agent={"model": "sonnet"}),
                ExperimentVariant(variant_id="opus", agent={"model": "opus"}),
                ExperimentVariant(variant_id="haiku", agent={"model": "haiku"}),
            ],
        )

        config = _make_config(run_dir)
        resolved = resolve_all_tasks(
            task_files=[task1, task2],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        assert len(resolved) == 6  # 2 tasks x 3 variants

    def test_run_dir_nesting(self, tmp_path, run_dir, default_experiment):
        """Resolved tasks have correct nested run_dir paths."""
        task1 = _write_task_yaml(tmp_path, "task-a", agent={"type": "claude-code"})

        experiment = ExperimentDefinition(
            experiment_id="my-exp",
            variants=[
                ExperimentVariant(variant_id="variant-one"),
                ExperimentVariant(variant_id="variant-two"),
            ],
        )

        config = _make_config(run_dir)
        resolved = resolve_all_tasks(
            task_files=[task1],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        # Check directory structure: run_dir/variant_id/{task_id}/NN
        paths = [r.run_dir for r in resolved]
        assert run_dir / "variant-one" / "task-a" / "00" in paths
        assert run_dir / "variant-two" / "task-a" / "00" in paths

    def test_unique_task_variant_pairs(self, tmp_path, run_dir, default_experiment):
        """Each resolved task should have a unique (task_id, variant_id) pair."""
        task1 = _write_task_yaml(tmp_path, "task-a", agent={"type": "claude-code"})

        experiment = ExperimentDefinition(
            experiment_id="exp",
            variants=[ExperimentVariant(variant_id="variant1"), ExperimentVariant(variant_id="variant2")],
        )

        config = _make_config(run_dir)
        resolved = resolve_all_tasks(
            task_files=[task1],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        pairs = [(r.task.task_id, r.variant_id) for r in resolved]
        assert len(pairs) == len(set(pairs))  # all unique

    def test_single_variant_preserves_task_id(self, tmp_path, run_dir, default_experiment):
        """Single-variant experiment preserves the original task_id."""
        task1 = _write_task_yaml(tmp_path, "task-a", agent={"type": "claude-code"})

        experiment = ExperimentDefinition(
            experiment_id="single",
            variants=[ExperimentVariant(variant_id="only")],
        )

        config = _make_config(run_dir)
        resolved = resolve_all_tasks(
            task_files=[task1],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        assert resolved[0].task.task_id == "task-a"
        assert resolved[0].variant_id == "only"

    def test_returns_resolved_task_instances(self, tmp_path, run_dir, default_experiment):
        """resolve_all_tasks returns list[ResolvedTask], not list[dict]."""
        task1 = _write_task_yaml(tmp_path, "task-a", agent={"type": "claude-code"})

        experiment = ExperimentDefinition(
            experiment_id="typed",
            variants=[ExperimentVariant(variant_id="default")],
        )

        config = _make_config(run_dir)
        resolved = resolve_all_tasks(
            task_files=[task1],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        assert len(resolved) == 1
        rt = resolved[0]
        assert isinstance(rt, ResolvedTask)
        assert rt.variant_id == "default"
        assert rt.task.task_id == "task-a"


class TestAggregateResults:
    def _make_eval_result(
        self, task_id: str, status: str = "SUCCESS", score: float = 0.9, duration: float = 30.0, variant_id: str = ""
    ):
        return EvaluationResult(
            task_id=task_id,
            task_description=f"Test {task_id}",
            variant_id=variant_id,
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status=status,
            weighted_score=score,
            duration_seconds=duration,
            iteration_count=1,
            environment_info={},
        )

    def test_aggregate_results_builds_experiment_result(self):
        """aggregate_results should produce a valid ExperimentResult."""
        variant_ids = ["sonnet", "opus"]

        task_results = [
            TaskResult(
                task_id="task-a",
                variant_id="sonnet",
                result=self._make_eval_result("task-a", "SUCCESS", 0.9, variant_id="sonnet"),
                duration=30.0,
            ),
            TaskResult(
                task_id="task-a",
                variant_id="opus",
                result=self._make_eval_result("task-a", "FAILURE", 0.6, variant_id="opus"),
                duration=50.0,
            ),
        ]

        experiment_result = aggregate_results(
            experiment_id="test",
            description="Test experiment",
            variant_ids=variant_ids,
            task_results=task_results,
            total_duration=80.0,
        )

        assert isinstance(experiment_result, ExperimentResult)
        assert experiment_result.experiment_id == "test"
        assert len(experiment_result.task_summaries) == 1
        assert experiment_result.task_summaries[0].best_variant == "sonnet"
        assert experiment_result.variant_aggregates["sonnet"].tasks_succeeded == 1
        assert experiment_result.variant_aggregates["opus"].tasks_failed == 1

    def test_aggregate_multiple_tasks(self):
        """aggregate_results with multiple tasks produces correct summaries."""
        variant_ids = ["a", "b"]

        task_results = [
            TaskResult(
                task_id="t1",
                variant_id="a",
                result=self._make_eval_result("t1", "SUCCESS", 0.8, 10.0, variant_id="a"),
                duration=10.0,
            ),
            TaskResult(
                task_id="t1",
                variant_id="b",
                result=self._make_eval_result("t1", "SUCCESS", 0.9, 20.0, variant_id="b"),
                duration=20.0,
            ),
            TaskResult(
                task_id="t2",
                variant_id="a",
                result=self._make_eval_result("t2", "FAILURE", 0.3, 15.0, variant_id="a"),
                duration=15.0,
            ),
            TaskResult(
                task_id="t2",
                variant_id="b",
                result=self._make_eval_result("t2", "SUCCESS", 0.7, 25.0, variant_id="b"),
                duration=25.0,
            ),
        ]

        result = aggregate_results(
            experiment_id="multi",
            description="Multi task",
            variant_ids=variant_ids,
            task_results=task_results,
            total_duration=70.0,
        )

        assert len(result.task_summaries) == 2
        assert result.variant_aggregates["a"].tasks_run == 2
        assert result.variant_aggregates["b"].tasks_succeeded == 2

    def test_aggregate_separates_error_from_failed(self):
        """aggregate_results counts ERROR separately from FAILURE/TIMEOUT."""
        variant_ids = ["v"]

        task_results = [
            TaskResult(
                task_id="t1",
                variant_id="v",
                result=self._make_eval_result("t1", "SUCCESS", 0.9, 10.0, variant_id="v"),
                duration=10.0,
            ),
            TaskResult(
                task_id="t2",
                variant_id="v",
                result=self._make_eval_result("t2", "FAILURE", 0.2, 20.0, variant_id="v"),
                duration=20.0,
            ),
            TaskResult(
                task_id="t3",
                variant_id="v",
                result=self._make_eval_result("t3", "ERROR", 0.0, 5.0, variant_id="v"),
                duration=5.0,
            ),
            TaskResult(
                task_id="t4",
                variant_id="v",
                result=self._make_eval_result("t4", "TIMEOUT", 0.0, 30.0, variant_id="v"),
                duration=30.0,
            ),
        ]

        result = aggregate_results(
            experiment_id="error-test",
            description="Error separation test",
            variant_ids=variant_ids,
            task_results=task_results,
            total_duration=65.0,
        )

        agg = result.variant_aggregates["v"]
        assert agg.tasks_run == 4
        assert agg.tasks_succeeded == 1
        assert agg.tasks_failed == 2  # FAILURE + TIMEOUT
        assert agg.tasks_error == 1  # only ERROR
        assert agg.tasks_succeeded + agg.tasks_failed + agg.tasks_error == agg.tasks_run

    def test_aggregate_detects_ties(self):
        """aggregate_results marks tasks with equal top scores as ties."""
        variant_ids = ["a", "b"]

        task_results = [
            TaskResult(
                task_id="t1",
                variant_id="a",
                result=self._make_eval_result("t1", "SUCCESS", 0.8, 10.0, variant_id="a"),
                duration=10.0,
            ),
            TaskResult(
                task_id="t1",
                variant_id="b",
                result=self._make_eval_result("t1", "SUCCESS", 0.8, 20.0, variant_id="b"),
                duration=20.0,
            ),
        ]

        result = aggregate_results(
            experiment_id="tie-test",
            description="Tie test",
            variant_ids=variant_ids,
            task_results=task_results,
            total_duration=30.0,
        )

        assert len(result.task_summaries) == 1
        ts = result.task_summaries[0]
        assert ts.is_tie is True
        assert ts.score_spread == 0.0
