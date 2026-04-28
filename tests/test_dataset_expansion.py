"""Tests for dataset fan-out: expand_dataset + resolve_all_tasks integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from coder_eval.models import (
    Dataset,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    TaskDefinition,
)
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import resolve_all_tasks
from coder_eval.orchestration.task_loader import expand_dataset


def _base_task_dict() -> dict[str, Any]:
    return {
        "task_id": "suite",
        "description": "Suite",
        "initial_prompt": "Prompt: ${row.prompt}",
        "sandbox": {"driver": "tempdir"},
        "success_criteria": [
            {
                "type": "file_contains",
                "path": "out.txt",
                "includes": ["${row.expected}"],
                "description": "Output matches ${row.expected}",
            }
        ],
    }


def _make_task_with_dataset(**dataset_kwargs) -> TaskDefinition:
    data = _base_task_dict()
    data["dataset"] = dataset_kwargs
    return TaskDefinition(**data)


class TestExpandDatasetNoDataset:
    def test_passthrough_when_no_dataset(self, tmp_path: Path) -> None:
        task = TaskDefinition(**_base_task_dict())
        expanded = expand_dataset(task, tmp_path)
        assert len(expanded) == 1
        assert expanded[0] is task  # same object, no copy


class TestExpandDatasetInline:
    def test_expands_rows_with_substitution(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(
            rows=[
                {"id": "r1", "prompt": "hello", "expected": "foo"},
                {"id": "r2", "prompt": "world", "expected": "bar"},
            ]
        )
        expanded = expand_dataset(task, tmp_path)
        assert [t.task_id for t in expanded] == ["suite/r1", "suite/r2"]
        assert expanded[0].initial_prompt == "Prompt: hello"
        assert expanded[1].initial_prompt == "Prompt: world"
        # Criterion string fields substituted:
        c0 = expanded[0].success_criteria[0]
        c1 = expanded[1].success_criteria[0]
        assert c0.type == "file_contains"
        assert c0.includes == ["foo"]  # type: ignore[attr-defined]
        assert c0.description == "Output matches foo"
        assert c1.includes == ["bar"]  # type: ignore[attr-defined]

    def test_dataset_cleared_on_expanded(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=[{"id": "r1", "prompt": "x", "expected": "y"}])
        expanded = expand_dataset(task, tmp_path)
        assert expanded[0].dataset is None

    def test_suite_id_and_row_id_populated(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(
            rows=[
                {"id": "r1", "prompt": "p1", "expected": "e1"},
                {"id": "r2", "prompt": "p2", "expected": "e2"},
            ]
        )
        expanded = expand_dataset(task, tmp_path)
        # Every expanded task should carry the parent suite_id and its own row_id.
        assert all(t.suite_id == "suite" for t in expanded)
        assert [t.row_id for t in expanded] == ["r1", "r2"]

    def test_non_dataset_task_has_no_suite_tags(self, tmp_path: Path) -> None:
        # Tasks without a dataset: pass through with suite_id/row_id unset.
        task = TaskDefinition(**_base_task_dict())
        expanded = expand_dataset(task, tmp_path)
        assert len(expanded) == 1
        assert expanded[0].suite_id is None
        assert expanded[0].row_id is None

    def test_custom_id_field(self, tmp_path: Path) -> None:
        data = _base_task_dict()
        data["dataset"] = {
            "id_field": "row_id",
            "rows": [
                {"row_id": "alpha", "prompt": "p1", "expected": "e1"},
                {"row_id": "beta", "prompt": "p2", "expected": "e2"},
            ],
        }
        task = TaskDefinition(**data)
        expanded = expand_dataset(task, tmp_path)
        assert [t.task_id for t in expanded] == ["suite/alpha", "suite/beta"]

    def test_max_rows_caps_expansion(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=[{"id": f"r{i}", "prompt": "x", "expected": "y"} for i in range(5)])
        expanded = expand_dataset(task, tmp_path, max_rows=2)
        assert [t.task_id for t in expanded] == ["suite/r0", "suite/r1"]

    def test_dataset_sample_caps_expansion(self, tmp_path: Path) -> None:
        # Task-level dataset.sample caps rows when no CLI max_rows is provided.
        task = _make_task_with_dataset(
            sample=3,
            rows=[{"id": f"r{i}", "prompt": "x", "expected": "y"} for i in range(10)],
        )
        expanded = expand_dataset(task, tmp_path)
        assert [t.task_id for t in expanded] == ["suite/r0", "suite/r1", "suite/r2"]

    def test_cli_max_rows_overrides_dataset_sample(self, tmp_path: Path) -> None:
        # CLI --sample wins over task-level dataset.sample, in both directions.
        task = _make_task_with_dataset(
            sample=3,
            rows=[{"id": f"r{i}", "prompt": "x", "expected": "y"} for i in range(10)],
        )
        # CLI tighter than task default
        tight = expand_dataset(task, tmp_path, max_rows=1)
        assert [t.task_id for t in tight] == ["suite/r0"]
        # CLI looser than task default
        loose = expand_dataset(task, tmp_path, max_rows=5)
        assert [t.task_id for t in loose] == [f"suite/r{i}" for i in range(5)]

    def test_no_cap_runs_full_dataset(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=[{"id": f"r{i}", "prompt": "x", "expected": "y"} for i in range(4)])
        expanded = expand_dataset(task, tmp_path)
        assert len(expanded) == 4


class TestExpandDatasetJsonl:
    def test_loads_jsonl(self, tmp_path: Path) -> None:
        ds_path = tmp_path / "rows.jsonl"
        ds_path.write_text(
            "\n".join(
                [
                    json.dumps({"id": "j1", "prompt": "jp1", "expected": "je1"}),
                    json.dumps({"id": "j2", "prompt": "jp2", "expected": "je2"}),
                    "",  # trailing blank line — tolerated
                ]
            )
        )
        task = _make_task_with_dataset(path="rows.jsonl")
        expanded = expand_dataset(task, tmp_path)
        assert [t.task_id for t in expanded] == ["suite/j1", "suite/j2"]
        assert expanded[0].initial_prompt == "Prompt: jp1"

    def test_missing_file(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(path="does_not_exist.jsonl")
        with pytest.raises(FileNotFoundError):
            expand_dataset(task, tmp_path)

    def test_relative_subdir_path(self, tmp_path: Path) -> None:
        # Dataset lives in a subdirectory relative to the task YAML.
        (tmp_path / "datasets").mkdir()
        ds_path = tmp_path / "datasets" / "rows.jsonl"
        ds_path.write_text(json.dumps({"id": "s1", "prompt": "sp", "expected": "se"}) + "\n")

        task = _make_task_with_dataset(path="datasets/rows.jsonl")
        expanded = expand_dataset(task, tmp_path)
        assert [t.task_id for t in expanded] == ["suite/s1"]

    def test_relative_parent_path(self, tmp_path: Path) -> None:
        # Dataset lives in a sibling directory; task YAML is nested deeper.
        (tmp_path / "datasets").mkdir()
        (tmp_path / "tasks").mkdir()
        ds_path = tmp_path / "datasets" / "rows.jsonl"
        ds_path.write_text(json.dumps({"id": "p1", "prompt": "pp", "expected": "pe"}) + "\n")

        task = _make_task_with_dataset(path="../datasets/rows.jsonl")
        expanded = expand_dataset(task, tmp_path / "tasks")
        assert [t.task_id for t in expanded] == ["suite/p1"]

    def test_absolute_path(self, tmp_path: Path) -> None:
        ds_path = tmp_path / "abs.jsonl"
        ds_path.write_text(json.dumps({"id": "a1", "prompt": "ap", "expected": "ae"}) + "\n")

        task = _make_task_with_dataset(path=str(ds_path))
        # Pass a different task_file_dir to confirm absolute paths are honored regardless.
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        expanded = expand_dataset(task, other_dir)
        assert [t.task_id for t in expanded] == ["suite/a1"]

    def test_malformed_jsonl_line(self, tmp_path: Path) -> None:
        ds_path = tmp_path / "rows.jsonl"
        ds_path.write_text('{"id": "ok", "prompt": "p", "expected": "e"}\n{not json}\n')
        task = _make_task_with_dataset(path="rows.jsonl")
        with pytest.raises(ValueError, match="invalid JSON on line 2"):
            expand_dataset(task, tmp_path)

    def test_non_object_row(self, tmp_path: Path) -> None:
        ds_path = tmp_path / "rows.jsonl"
        ds_path.write_text('["not", "an", "object"]\n')
        task = _make_task_with_dataset(path="rows.jsonl")
        with pytest.raises(ValueError, match="not a JSON object"):
            expand_dataset(task, tmp_path)


class TestExpandDatasetValidation:
    def test_missing_id_field(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=[{"prompt": "x", "expected": "y"}])
        with pytest.raises(ValueError, match="missing id_field 'id'"):
            expand_dataset(task, tmp_path)

    def test_duplicate_row_ids(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(
            rows=[
                {"id": "same", "prompt": "p1", "expected": "e1"},
                {"id": "same", "prompt": "p2", "expected": "e2"},
            ]
        )
        with pytest.raises(ValueError, match="Duplicate dataset row id"):
            expand_dataset(task, tmp_path)

    def test_empty_dataset(self, tmp_path: Path) -> None:
        ds_path = tmp_path / "rows.jsonl"
        ds_path.write_text("")
        task = _make_task_with_dataset(path="rows.jsonl")
        with pytest.raises(ValueError, match="empty"):
            expand_dataset(task, tmp_path)

    def test_unsafe_row_id_slash(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=[{"id": "bad/id", "prompt": "p", "expected": "e"}])
        with pytest.raises(ValueError, match="must match"):
            expand_dataset(task, tmp_path)

    def test_unsafe_row_id_space(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=[{"id": "bad id", "prompt": "p", "expected": "e"}])
        with pytest.raises(ValueError, match="must match"):
            expand_dataset(task, tmp_path)

    def test_unknown_row_var_in_prompt(self, tmp_path: Path) -> None:
        data = _base_task_dict()
        data["initial_prompt"] = "Prompt: ${row.does_not_exist}"
        data["dataset"] = {"rows": [{"id": "r1", "prompt": "p", "expected": "e"}]}
        task = TaskDefinition(**data)
        with pytest.raises(KeyError, match=r"row\.does_not_exist"):
            expand_dataset(task, tmp_path)

    def test_nested_value_rejected(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=[{"id": "r1", "prompt": {"nested": "dict"}, "expected": "e"}])
        with pytest.raises(TypeError, match="must be a scalar"):
            expand_dataset(task, tmp_path)


class TestDatasetModelValidation:
    def test_requires_rows_or_path(self) -> None:
        with pytest.raises(ValueError, match="either 'path' or 'rows'"):
            Dataset()

    def test_forbids_both_rows_and_path(self) -> None:
        with pytest.raises(ValueError, match="only one of"):
            Dataset(rows=[{"id": "r1"}], path="rows.jsonl")

    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValueError):
            Dataset.model_validate({"rows": [{"id": "r1"}], "unknown": "x"})


class TestResolveAllTasksIntegration:
    def _write_task_yaml(self, tmp_path: Path, task_id: str, with_dataset: bool) -> Path:
        data = {
            "task_id": task_id,
            "description": "Test",
            "initial_prompt": "Prompt: ${row.prompt}" if with_dataset else "Static",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "out.txt", "description": "File"}],
        }
        if with_dataset:
            data["dataset"] = {
                "rows": [
                    {"id": "row-a", "prompt": "a"},
                    {"id": "row-b", "prompt": "b"},
                ]
            }
        p = tmp_path / f"{task_id}.yaml"
        p.write_text(yaml.safe_dump(data))
        return p

    def _make_experiment(self, variant_ids: list[str]) -> tuple[ExperimentDefinition, ExperimentDefinition]:
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
            variants=[ExperimentVariant(variant_id="default")],
        )
        experiment = ExperimentDefinition(
            experiment_id="exp",
            variants=[ExperimentVariant(variant_id=vid) for vid in variant_ids],
        )
        return default_exp, experiment

    def test_rows_fan_out_across_variants(self, tmp_path: Path) -> None:
        task_file = self._write_task_yaml(tmp_path, "suite", with_dataset=True)
        default_exp, experiment = self._make_experiment(["v1", "v2"])
        config = BatchRunConfig(run_dir=tmp_path / "runs")

        resolved = resolve_all_tasks(
            task_files=[task_file],
            experiment=experiment,
            default_experiment=default_exp,
            config=config,
        )

        # 2 rows x 2 variants = 4 resolved tasks
        assert len(resolved) == 4
        task_ids = sorted({rt.task.task_id for rt in resolved})
        assert task_ids == ["suite/row-a", "suite/row-b"]
        variant_ids = sorted({rt.variant_id for rt in resolved})
        assert variant_ids == ["v1", "v2"]

        # run_dir reflects /variant/suite/row/NN nesting (NN = replicate index)
        for rt in resolved:
            assert rt.run_dir == config.run_dir / rt.variant_id / rt.task.task_id / "00"
            assert rt.replicate_index == 0

    def test_max_rows_applies(self, tmp_path: Path) -> None:
        task_file = self._write_task_yaml(tmp_path, "suite", with_dataset=True)
        default_exp, experiment = self._make_experiment(["v1"])
        config = BatchRunConfig(run_dir=tmp_path / "runs", max_rows=1)

        resolved = resolve_all_tasks(
            task_files=[task_file],
            experiment=experiment,
            default_experiment=default_exp,
            config=config,
        )
        assert len(resolved) == 1
        assert resolved[0].task.task_id == "suite/row-a"

    def test_non_dataset_task_unaffected(self, tmp_path: Path) -> None:
        task_file = self._write_task_yaml(tmp_path, "plain", with_dataset=False)
        default_exp, experiment = self._make_experiment(["v1"])
        config = BatchRunConfig(run_dir=tmp_path / "runs", max_rows=99)

        resolved = resolve_all_tasks(
            task_files=[task_file],
            experiment=experiment,
            default_experiment=default_exp,
            config=config,
        )
        assert len(resolved) == 1
        assert resolved[0].task.task_id == "plain"

    def test_experiment_agent_defaults_survive_dataset_expansion(self, tmp_path: Path) -> None:
        # Regression: expand_dataset used model_dump() (full) which inflated model_fields_set
        # on the expanded TaskDefinitions. The merge layer then saw allowed_tools=None as an
        # explicit task-level override and discarded the experiment default.
        #
        # Concrete example: experiment sets allowed_tools=["Skill"]; task sets agent.type only.
        # Before the fix, expanded rows got allowed_tools=None, so the agent couldn't invoke
        # the Skill tool and fell back to unrelated slash commands.
        task_data = {
            "task_id": "suite",
            "description": "Suite",
            "initial_prompt": "${row.prompt}",
            "sandbox": {"driver": "tempdir"},
            "agent": {"type": "claude-code"},
            "success_criteria": [{"type": "file_exists", "path": "out.txt", "description": "exists"}],
            "dataset": {"rows": [{"id": "r1", "prompt": "hello"}, {"id": "r2", "prompt": "world"}]},
        }
        task_file = tmp_path / "suite.yaml"
        task_file.write_text(yaml.safe_dump(task_data))

        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
            variants=[ExperimentVariant(variant_id="default")],
        )
        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "allowed_tools": ["Skill"]}),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        config = BatchRunConfig(run_dir=tmp_path / "runs")

        resolved = resolve_all_tasks(
            task_files=[task_file],
            experiment=experiment,
            default_experiment=default_exp,
            config=config,
        )

        assert len(resolved) == 2
        for rt in resolved:
            assert rt.task.agent is not None
            assert rt.task.agent.allowed_tools == ["Skill"], (
                f"experiment allowed_tools overridden for {rt.task.task_id}"
            )

    def test_resolved_task_carries_suite_tags(self, tmp_path: Path) -> None:
        # After row expansion + variant resolution, suite_id/row_id should be
        # preserved on the ResolvedTask.task so run_batch can copy them onto TaskResult.
        task_file = self._write_task_yaml(tmp_path, "suite", with_dataset=True)
        default_exp, experiment = self._make_experiment(["v1"])
        config = BatchRunConfig(run_dir=tmp_path / "runs")

        resolved = resolve_all_tasks(
            task_files=[task_file],
            experiment=experiment,
            default_experiment=default_exp,
            config=config,
        )
        tags = sorted((rt.task.suite_id, rt.task.row_id) for rt in resolved)
        assert tags == [("suite", "row-a"), ("suite", "row-b")]


class TestErrorPathPropagation:
    def test_error_task_result_preserves_suite_tags(self, tmp_path: Path) -> None:
        # When task loading/execution raises, the error TaskResult must still
        # carry suite_id/row_id so the rollup writer groups it into its suite.
        from coder_eval.orchestration.batch import _create_error_task_result

        tr = _create_error_task_result(
            tmp_path / "task.yaml",
            ValueError("boom"),
            task_id="suite/row-a",
            variant_id="v1",
            suite_id="suite",
            row_id="row-a",
        )
        assert tr.suite_id == "suite"
        assert tr.row_id == "row-a"
        assert tr.task_id == "suite/row-a"
        assert tr.result.final_status.category == "error"

    def test_error_task_result_without_suite_tags(self, tmp_path: Path) -> None:
        # Non-dataset error path: no suite tags, no rollup.
        from coder_eval.orchestration.batch import _create_error_task_result

        tr = _create_error_task_result(
            tmp_path / "plain.yaml",
            ValueError("boom"),
            task_id="plain",
            variant_id="v1",
        )
        assert tr.suite_id is None
        assert tr.row_id is None


class TestDatasetRepeatsFanout:
    def _make_experiment(self, repeats: int | None = None) -> ExperimentDefinition:
        variant_kwargs = {}
        if repeats is not None:
            variant_kwargs["repeats"] = repeats
        return ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
            variants=[
                ExperimentVariant(variant_id="v1", **variant_kwargs),
                ExperimentVariant(variant_id="v2", **variant_kwargs),
            ],
        )

    def test_rows_fan_out_times_repeats(self, tmp_path: Path) -> None:
        """2 rows x 2 variants x repeats=3 = 12 ResolvedTasks."""
        from coder_eval.orchestration.config import BatchRunConfig
        from coder_eval.orchestration.experiment import resolve_all_tasks

        task = _make_task_with_dataset(
            rows=[
                {"id": "r1", "prompt": "p1", "expected": "e1"},
                {"id": "r2", "prompt": "p2", "expected": "e2"},
            ]
        )
        task_file = tmp_path / "task.yaml"
        import yaml as _yaml

        task_file.write_text(_yaml.dump(task.model_dump(mode="json")))

        experiment = self._make_experiment(repeats=3)
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="default")],
        )
        config = BatchRunConfig(run_dir=tmp_path / "runs")

        resolved = resolve_all_tasks([task_file], experiment, default_exp, config)
        assert len(resolved) == 12

        # Each (row, variant) pair has replicate_index 0, 1, 2
        for vid in ("v1", "v2"):
            for row_id in ("r1", "r2"):
                task_id = f"suite/{row_id}"
                indices = sorted(
                    rt.replicate_index for rt in resolved if rt.variant_id == vid and rt.task.task_id == task_id
                )
                assert indices == [0, 1, 2]

    def test_run_dir_reflects_replicate_index(self, tmp_path: Path) -> None:
        from coder_eval.orchestration.config import BatchRunConfig
        from coder_eval.orchestration.experiment import resolve_all_tasks

        task = _make_task_with_dataset(rows=[{"id": "r1", "prompt": "p1", "expected": "e1"}])
        task_file = tmp_path / "task.yaml"
        import yaml as _yaml

        task_file.write_text(_yaml.dump(task.model_dump(mode="json")))

        experiment = self._make_experiment(repeats=3)
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="default")],
        )
        config = BatchRunConfig(run_dir=tmp_path / "runs")
        resolved = resolve_all_tasks([task_file], experiment, default_exp, config)

        subdirs = sorted(rt.run_dir.name for rt in resolved if rt.variant_id == "v1")
        assert subdirs == ["00", "01", "02"]

    def test_duplicate_detection_still_catches_true_dupes(self, tmp_path: Path) -> None:
        """Same task YAML loaded twice still raises on duplicate task IDs."""
        from coder_eval.orchestration.config import BatchRunConfig
        from coder_eval.orchestration.experiment import resolve_all_tasks

        data = {
            "task_id": "plain-task",
            "description": "d",
            "initial_prompt": "p",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "f.py", "description": "d"}],
        }
        import yaml as _yaml

        task_file = tmp_path / "task.yaml"
        task_file.write_text(_yaml.dump(data))
        task_file2 = tmp_path / "task2.yaml"
        task_file2.write_text(_yaml.dump(data))

        experiment = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        config = BatchRunConfig(run_dir=tmp_path / "runs")

        import pytest

        with pytest.raises(ValueError, match="Duplicate task IDs"):
            resolve_all_tasks([task_file, task_file2], experiment, experiment, config)
