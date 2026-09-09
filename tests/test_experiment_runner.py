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
    FinalStatus,
    PreservationMode,
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
    defaults = {"run_dir": run_dir, "max_parallel": 1, "preservation_mode": PreservationMode.NONE}
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
        resolved, _ = resolve_all_tasks(
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
        resolved, _ = resolve_all_tasks(
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
        resolved, _ = resolve_all_tasks(
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
        resolved, _ = resolve_all_tasks(
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
        resolved, _ = resolve_all_tasks(
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

    def test_repeats_100_raises_before_any_sandbox(self, tmp_path, run_dir, default_experiment):
        """repeats=100 must raise ValueError at resolution time, not at execution time."""
        task1 = _write_task_yaml(tmp_path, "task-a", agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="over-limit",
            variants=[ExperimentVariant(variant_id="v")],
        )
        config = _make_config(run_dir, repeats=100)
        with pytest.raises(ValueError, match="repeats must be <= 99"):
            resolve_all_tasks(
                task_files=[task1],
                experiment=experiment,
                default_experiment=default_experiment,
                config=config,
            )

    def test_skips_invalid_yaml_continues_with_rest(self, tmp_path, run_dir, default_experiment):
        """Bad task YAML is recorded in skipped + excluded; valid tasks still run."""
        good = _write_task_yaml(tmp_path, "task-good", agent={"type": "claude-code"})
        bad = tmp_path / "task-bad.yaml"
        # Missing required fields (task_id, initial_prompt, etc.) — Pydantic should reject.
        bad.write_text("description: 'malformed task'\n")

        experiment = ExperimentDefinition(
            experiment_id="test-exp",
            variants=[ExperimentVariant(variant_id="v")],
        )
        config = _make_config(run_dir)
        resolved, skipped = resolve_all_tasks(
            task_files=[good, bad],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        assert len(resolved) == 1
        assert resolved[0].task.task_id == "task-good"
        assert len(skipped) == 1
        assert skipped[0].path == str(bad)
        assert "ValueError" in skipped[0].reason or "ValidationError" in skipped[0].reason

    def test_skip_true_excludes_task_from_resolution(self, tmp_path, run_dir, default_experiment):
        """`skip: true` in the YAML excludes the task and records it in `skipped`.

        Regression guard for MST-9675: TaskDefinition previously had no ``skip``
        field, so authors writing ``skip: true`` to quarantine a known-blocked
        task got a silent no-op — the task still ran (and often errored). Honor
        the field at resolve_all_tasks time and report it via SkippedTask with
        a ``"skip: true"`` reason prefix so consumers can distinguish opt-outs
        from load failures.
        """
        blocked = _write_task_yaml(tmp_path, "task-blocked", agent={"type": "claude-code"})
        # Mutate the loaded YAML to add `skip: true` while keeping every other
        # field valid — this is the same shape as a real quarantine.
        data = yaml.safe_load(blocked.read_text())
        data["skip"] = True
        blocked.write_text(yaml.dump(data))

        live = _write_task_yaml(tmp_path, "task-live", agent={"type": "claude-code"})

        experiment = ExperimentDefinition(
            experiment_id="test-exp",
            variants=[ExperimentVariant(variant_id="v")],
        )
        config = _make_config(run_dir)
        resolved, skipped = resolve_all_tasks(
            task_files=[blocked, live],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        # Live task still resolves; quarantined task is held back.
        assert [rt.task.task_id for rt in resolved] == ["task-live"]
        assert len(skipped) == 1
        assert skipped[0].path == str(blocked)
        assert skipped[0].reason.startswith("skip: true")
        assert "task-blocked" in skipped[0].reason

    def test_include_skipped_runs_skip_true_tasks(self, tmp_path, run_dir, default_experiment):
        """`config.include_skipped` (--include-skipped) overrides `skip: true`.

        A quarantined / opt-in task stays excluded from the nightly/CI (default),
        but an on-demand run passing --include-skipped resolves it like any other
        task — no entry in `skipped`. This is the bypass that makes `skip: true`
        usable for "run sometimes, not daily" instead of permanent quarantine.
        """
        blocked = _write_task_yaml(tmp_path, "task-blocked", agent={"type": "claude-code"})
        data = yaml.safe_load(blocked.read_text())
        data["skip"] = True
        blocked.write_text(yaml.dump(data))

        live = _write_task_yaml(tmp_path, "task-live", agent={"type": "claude-code"})

        experiment = ExperimentDefinition(
            experiment_id="test-exp",
            variants=[ExperimentVariant(variant_id="v")],
        )
        config = _make_config(run_dir, include_skipped=True)
        resolved, skipped = resolve_all_tasks(
            task_files=[blocked, live],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        # Both tasks resolve; nothing is held back as skipped.
        assert sorted(rt.task.task_id for rt in resolved) == ["task-blocked", "task-live"]
        assert skipped == []

    def test_skip_true_bypasses_dataset_fanout(self, tmp_path, run_dir, default_experiment):
        """`skip: true` on a dataset-backed task records ONE skip — no per-row fan-out.

        The skip short-circuit sits before `expand_dataset`, so a quarantined
        dataset task never reads its JSONL / inline rows. Guards against any
        future refactor that moves the skip check past the dataset expander.
        """
        blocked = tmp_path / "blocked-dataset.yaml"
        blocked.write_text(
            yaml.safe_dump(
                {
                    "task_id": "blocked-dataset",
                    "description": "Quarantined dataset task",
                    "initial_prompt": "Prompt: ${row.prompt}",
                    "sandbox": {"driver": "tempdir"},
                    "success_criteria": [{"type": "file_exists", "path": "out.txt", "description": "f"}],
                    "skip": True,
                    "dataset": {
                        "rows": [
                            {"id": "row-a", "prompt": "a"},
                            {"id": "row-b", "prompt": "b"},
                            {"id": "row-c", "prompt": "c"},
                        ]
                    },
                }
            )
        )

        experiment = ExperimentDefinition(experiment_id="test-exp", variants=[ExperimentVariant(variant_id="v")])
        config = _make_config(run_dir)
        resolved, skipped = resolve_all_tasks(
            task_files=[blocked],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        assert resolved == []
        # Critical: one entry, not three. The dataset rows must not fan out.
        assert len(skipped) == 1
        assert skipped[0].reason.startswith("skip: true")
        assert "blocked-dataset" in skipped[0].reason

    def test_skip_true_bypasses_variant_resolution(self, tmp_path, run_dir, default_experiment):
        """`skip: true` records ONE skip regardless of how many variants exist.

        The skip short-circuit runs before the inner variant loop, so variant
        resolution / file-path injection / prompt overrides / CLI overrides
        all stay cold. Guards against any future refactor that moves the
        skip check inside the variant loop and would inflate skip-count by
        the variant fan-out factor.
        """
        blocked = _write_task_yaml(tmp_path, "blocked-multi-variant", agent={"type": "claude-code"})
        data = yaml.safe_load(blocked.read_text())
        data["skip"] = True
        blocked.write_text(yaml.dump(data))

        experiment = ExperimentDefinition(
            experiment_id="three-variants",
            variants=[
                ExperimentVariant(variant_id="sonnet"),
                ExperimentVariant(variant_id="opus"),
                ExperimentVariant(variant_id="haiku"),
            ],
        )
        config = _make_config(run_dir)
        resolved, skipped = resolve_all_tasks(
            task_files=[blocked],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        assert resolved == []
        # Not 3 — the skip must short-circuit the variant loop, not run per-variant.
        assert len(skipped) == 1

    def test_skip_true_survives_tag_filter(self, tmp_path, run_dir, default_experiment):
        """`skip: true` is recorded BEFORE tag filtering runs — `skipped` is preserved.

        Regression guard for the false-positive concern that tag-filter logic
        might swallow `skip: true` entries. `experiment.py` appends to `skipped`
        at task-load time and `continue`s; tag filtering operates only on the
        `resolved` list later, so `skipped` is never touched by tag logic.

        We exercise both axes:
          * `exclude_tags` matching the skipped task — `skipped` still has the entry.
          * `include_tags` NOT matching the skipped task — same.
        """
        blocked = _write_task_yaml(tmp_path, "task-blocked-tagged", agent={"type": "claude-code"})
        data = yaml.safe_load(blocked.read_text())
        data["skip"] = True
        data["tags"] = ["smoke", "blocked"]
        blocked.write_text(yaml.dump(data))

        experiment = ExperimentDefinition(experiment_id="test-exp", variants=[ExperimentVariant(variant_id="v")])

        # Axis 1: exclude_tags matches the skipped task's tags.
        resolved, skipped = resolve_all_tasks(
            task_files=[blocked],
            experiment=experiment,
            default_experiment=default_experiment,
            config=_make_config(run_dir, exclude_tags={"smoke"}),
        )
        assert resolved == []
        assert len(skipped) == 1
        assert skipped[0].reason.startswith("skip: true")

        # Axis 2: include_tags doesn't match the skipped task's tags.
        resolved, skipped = resolve_all_tasks(
            task_files=[blocked],
            experiment=experiment,
            default_experiment=default_experiment,
            config=_make_config(run_dir, include_tags={"never-matches-anything"}),
        )
        assert resolved == []
        assert len(skipped) == 1
        assert skipped[0].reason.startswith("skip: true")

    def test_resolve_accumulates_skip_and_load_failures(self, tmp_path, run_dir, default_experiment):
        """`skipped` collects both intentional skips AND load failures, distinguishable by `reason`.

        Confirms the dual-purpose contract on `SkippedTask`: a single PR run
        with one quarantined task, one malformed task, and one good task
        produces one resolved entry and TWO skip entries with different
        reason prefixes (`"skip: true"` vs the exception-type prefix).
        """
        good = _write_task_yaml(tmp_path, "task-good", agent={"type": "claude-code"})

        blocked = _write_task_yaml(tmp_path, "task-blocked", agent={"type": "claude-code"})
        data = yaml.safe_load(blocked.read_text())
        data["skip"] = True
        blocked.write_text(yaml.dump(data))

        bad = tmp_path / "task-bad.yaml"
        bad.write_text("description: 'malformed task'\n")

        experiment = ExperimentDefinition(experiment_id="test-exp", variants=[ExperimentVariant(variant_id="v")])
        config = _make_config(run_dir)
        resolved, skipped = resolve_all_tasks(
            task_files=[good, blocked, bad],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )

        assert [rt.task.task_id for rt in resolved] == ["task-good"]
        assert len(skipped) == 2
        reasons = {s.path: s.reason for s in skipped}
        assert reasons[str(blocked)].startswith("skip: true")
        # Load-failure reason carries the exception type, NOT the skip prefix.
        bad_reason = reasons[str(bad)]
        assert not bad_reason.startswith("skip: true")
        assert "ValueError" in bad_reason or "ValidationError" in bad_reason


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


class TestReplicateAggregation:
    def _make_eval_result(
        self,
        task_id: str,
        status: str = "SUCCESS",
        score: float = 0.9,
        duration: float = 30.0,
        variant_id: str = "v",
    ) -> EvaluationResult:
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

    def _make_tr(
        self,
        task_id: str,
        variant_id: str,
        score: float,
        status: str = "SUCCESS",
        replicate_index: int = 0,
        duration: float = 10.0,
    ) -> TaskResult:
        return TaskResult(
            task_id=task_id,
            variant_id=variant_id,
            result=self._make_eval_result(
                task_id, status=status, score=score, duration=duration, variant_id=variant_id
            ),
            duration=duration,
            replicate_index=replicate_index,
        )

    def test_three_replicates_collapse_to_single_variant_result(self):
        reps = [
            self._make_tr("t", "v", score=0.8, replicate_index=0),
            self._make_tr("t", "v", score=0.9, replicate_index=1),
            self._make_tr("t", "v", score=1.0, replicate_index=2),
        ]
        result = aggregate_results(
            experiment_id="e", description="", variant_ids=["v"], task_results=reps, total_duration=30.0
        )
        assert len(result.task_summaries) == 1
        vr = result.task_summaries[0].variant_results[0]
        assert abs(vr.weighted_score - 0.9) < 1e-9
        assert vr.replicate_count == 3
        assert result.task_summaries[0].replicate_count == 3

    def test_mixed_statuses_picks_worst(self):
        reps = [
            self._make_tr("t", "v", score=1.0, status="SUCCESS", replicate_index=0),
            self._make_tr("t", "v", score=1.0, status="SUCCESS", replicate_index=1),
            self._make_tr("t", "v", score=0.0, status="ERROR", replicate_index=2),
        ]
        result = aggregate_results(
            experiment_id="e", description="", variant_ids=["v"], task_results=reps, total_duration=30.0
        )
        vr = result.task_summaries[0].variant_results[0]
        assert vr.final_status.category == "error"

    def test_variant_aggregate_task_count_matches_unique_tasks(self):
        trs = [self._make_tr("t1", "v", score=0.8, replicate_index=r) for r in range(3)] + [
            self._make_tr("t2", "v", score=0.9, replicate_index=r) for r in range(3)
        ]
        result = aggregate_results(
            experiment_id="e", description="", variant_ids=["v"], task_results=trs, total_duration=30.0
        )
        agg = result.variant_aggregates["v"]
        assert agg.tasks_run == 2  # 2 unique tasks, not 6
        assert agg.replicate_count == 3

    def test_replicate_count_degrades_on_mixed_lengths(self):
        trs_a = [self._make_tr("t", "a", score=0.8, replicate_index=r) for r in range(3)]
        trs_b = [self._make_tr("t", "b", score=0.9, replicate_index=r) for r in range(5)]
        result = aggregate_results(
            experiment_id="e",
            description="",
            variant_ids=["a", "b"],
            task_results=trs_a + trs_b,
            total_duration=30.0,
        )
        # TaskExperimentSummary uses min(rep_counts)
        assert result.task_summaries[0].replicate_count == 3

    def test_per_replicate_scores_populated(self):
        reps = [self._make_tr("t", "v", score=s, replicate_index=i) for i, s in enumerate([0.5, 0.7, 0.9])]
        result = aggregate_results(
            experiment_id="e", description="", variant_ids=["v"], task_results=reps, total_duration=30.0
        )
        scores = result.per_replicate_scores["v"]["t"]
        assert len(scores) == 3
        assert abs(scores[0] - 0.5) < 1e-9
        assert abs(scores[2] - 0.9) < 1e-9

    def test_single_replicate_is_unchanged(self):
        trs = [
            self._make_tr("t1", "v", score=0.8),
            self._make_tr("t2", "v", score=0.6),
        ]
        result = aggregate_results(
            experiment_id="e", description="", variant_ids=["v"], task_results=trs, total_duration=20.0
        )
        agg = result.variant_aggregates["v"]
        assert agg.tasks_run == 2
        assert agg.replicate_count == 1
        for ts in result.task_summaries:
            assert ts.replicate_count == 1

    def test_error_replicate_excluded_from_duration(self):
        reps = [
            self._make_tr("t", "v", score=1.0, status="SUCCESS", replicate_index=0, duration=10.0),
            self._make_tr("t", "v", score=0.0, status="ERROR", replicate_index=1, duration=5.0),
        ]
        result = aggregate_results(
            experiment_id="e", description="", variant_ids=["v"], task_results=reps, total_duration=15.0
        )
        vr = result.task_summaries[0].variant_results[0]
        # Only non-errored replicate duration included
        assert abs(vr.duration_seconds - 10.0) < 1e-9

    def test_all_replicates_errored_gives_zero_duration(self):
        reps = [
            self._make_tr("t", "v", score=0.0, status="ERROR", replicate_index=0, duration=3.0),
            self._make_tr("t", "v", score=0.0, status="ERROR", replicate_index=1, duration=4.0),
        ]
        result = aggregate_results(
            experiment_id="e", description="", variant_ids=["v"], task_results=reps, total_duration=7.0
        )
        vr = result.task_summaries[0].variant_results[0]
        assert abs(vr.duration_seconds - 0.0) < 1e-9
        assert vr.final_status == FinalStatus.ERROR


class TestAggregationCountsErroredRowsAsMisses:
    """An ERRORED row must count as 0.0, not vanish from both sides.

    ``weighted_score is None`` and ``final_status.category == "ungraded"`` look
    interchangeable and are not: an ERROR / BUILD_FAILED row also has no score.
    Filtering on the score dropped it from the numerator AND the denominator, so
    a nightly where one image build failed reported a HIGHER headline score than
    a clean one, and A/B comparisons were biased toward whichever variant errored
    more. Only an ungraded row leaves both sides — nothing measured it.
    """

    @staticmethod
    def _result(task_id: str, variant_id: str, status: str, score: float | None) -> EvaluationResult:
        return EvaluationResult(
            task_id=task_id,
            task_description="d",
            variant_id=variant_id,
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status=status,  # type: ignore[arg-type]
            weighted_score=score,
            duration_seconds=1.0,
            iteration_count=1,
            environment_info={},
        )

    def _aggregate(self, rows: list[tuple[str, str, str, float | None]]) -> ExperimentResult:
        return aggregate_results(
            experiment_id="e",
            description="d",
            variant_ids=sorted({vid for _, vid, _, _ in rows}),
            task_results=[
                TaskResult(
                    task_id=task_id,
                    variant_id=variant_id,
                    result=self._result(task_id, variant_id, status, score),
                    duration=1.0,
                )
                for task_id, variant_id, status, score in rows
            ],
            total_duration=1.0,
        )

    def test_an_errored_row_drags_the_average_down_like_a_miss(self):
        clean = self._aggregate([("a", "v", "SUCCESS", 1.0), ("b", "v", "FAILURE", 0.0)])
        errored = self._aggregate([("a", "v", "SUCCESS", 1.0), ("b", "v", "ERROR", None)])

        assert clean.variant_aggregates["v"].average_score == 0.5
        assert errored.variant_aggregates["v"].average_score == 0.5, (
            "an infrastructure-failure night must not score HIGHER than a clean one"
        )

    def test_a_build_failure_is_a_miss_too(self):
        result = self._aggregate([("a", "v", "SUCCESS", 1.0), ("b", "v", "BUILD_FAILED", None)])
        assert result.variant_aggregates["v"].average_score == 0.5

    def test_an_ungraded_row_leaves_both_sides(self):
        result = self._aggregate([("a", "v", "SUCCESS", 1.0), ("b", "v", "NOT_GRADED", None)])
        assert result.variant_aggregates["v"].average_score == 1.0

    def test_a_fully_ungraded_variant_has_no_average(self):
        result = self._aggregate([("a", "v", "NOT_GRADED", None)])
        assert result.variant_aggregates["v"].average_score is None

    def test_score_spread_still_sees_a_fully_errored_arm(self):
        """A task where variant B fully errored reported spread 0.0 instead of
        the real gap, because B had no score to compare."""
        result = self._aggregate([("a", "x", "SUCCESS", 0.8), ("a", "y", "ERROR", None)])
        assert result.task_summaries[0].score_spread == pytest.approx(0.8)

    def test_no_arbitrary_winner_when_nothing_was_scored(self):
        """`variants[0]` named whichever arm the input happened to list first,
        with `is_tie=False` asserting it was a real result — so swapping the
        inputs flipped the reported winner."""
        one = self._aggregate([("a", "x", "NOT_GRADED", None), ("a", "y", "NOT_GRADED", None)])
        other = self._aggregate([("a", "y", "NOT_GRADED", None), ("a", "x", "NOT_GRADED", None)])

        assert one.task_summaries[0].best_variant == other.task_summaries[0].best_variant
        assert one.task_summaries[0].is_tie is True
