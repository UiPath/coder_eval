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
    RowSelection,
    TaskDefinition,
)
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import resolve_all_tasks
from coder_eval.orchestration.task_loader import SplitSelectorError, expand_dataset


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

    def test_max_rows_random_caps_expansion(self, tmp_path: Path) -> None:
        # CLI --sample: a fixed-seed random N-row subset (reproducible, count == N).
        rows = [{"id": f"r{i}", "prompt": "x", "expected": "y"} for i in range(5)]
        expanded = expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, max_rows=2)
        ids = [t.row_id for t in expanded]
        assert len(ids) == 2
        assert set(ids) <= {f"r{i}" for i in range(5)}
        # Fixed seed => same subset on a second call.
        again = [t.row_id for t in expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, max_rows=2)]
        assert ids == again

    def test_max_rows_is_unbiased_across_paths(self, tmp_path: Path) -> None:
        # Regression: a first-N slice would draw all rows from the first block.
        # The random sample must be able to reach later blocks too.
        front = [{"id": f"a{i}", "prompt": "x", "expected": "y"} for i in range(20)]
        back = [{"id": f"z{i}", "prompt": "x", "expected": "y"} for i in range(20)]
        expanded = expand_dataset(_make_task_with_dataset(rows=front + back), tmp_path, max_rows=10)
        assert any((t.row_id or "").startswith("z") for t in expanded)

    def test_max_rows_at_or_above_size_runs_full(self, tmp_path: Path) -> None:
        rows = [{"id": f"r{i}", "prompt": "x", "expected": "y"} for i in range(3)]
        assert len(expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, max_rows=3)) == 3
        assert len(expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, max_rows=99)) == 3

    def test_no_cap_runs_full_dataset(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=[{"id": f"r{i}", "prompt": "x", "expected": "y"} for i in range(4)])
        expanded = expand_dataset(task, tmp_path)
        assert len(expanded) == 4


class TestExpandDatasetStratifiedSample:
    """sample_per_stratum: random N-per-stratum (the activation-suite sampling mode)."""

    @staticmethod
    def _stratified_rows() -> list[dict[str, Any]]:
        # Strata of uneven size, mirroring activation: big skills, a small skill,
        # and the shared-negative stratum (expected_skill == "").
        rows: list[dict[str, Any]] = []
        rows += [{"id": f"a-{i}", "prompt": "p", "expected": "e", "expected_skill": "skill-a"} for i in range(8)]
        rows += [{"id": f"b-{i}", "prompt": "p", "expected": "e", "expected_skill": "skill-b"} for i in range(5)]
        rows += [{"id": f"c-{i}", "prompt": "p", "expected": "e", "expected_skill": "skill-c"} for i in range(2)]
        rows += [{"id": f"n-{i}", "prompt": "p", "expected": "e", "expected_skill": ""} for i in range(6)]
        return rows

    @staticmethod
    def _counts_by_stratum(expanded: list[TaskDefinition]) -> dict[str, int]:
        # row_id prefix encodes the stratum: "a-*"/"b-*"/"c-*" skills, "n-*" negatives.
        counts: dict[str, int] = {}
        for t in expanded:
            assert t.row_id is not None
            counts[t.row_id.split("-")[0]] = counts.get(t.row_id.split("-")[0], 0) + 1
        return counts

    def test_caps_each_stratum_and_takes_small_whole(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=self._stratified_rows(), sample_per_stratum=3, sample_seed=42)
        expanded = expand_dataset(task, tmp_path)
        counts = self._counts_by_stratum(expanded)
        # Big strata capped at 3; the 2-row stratum 'c' taken whole (<= N).
        assert counts == {"a": 3, "b": 3, "c": 2, "n": 3}
        assert len(expanded) == 11

    def test_deterministic_under_seed(self, tmp_path: Path) -> None:
        rows = self._stratified_rows()
        ids1 = [
            t.row_id
            for t in expand_dataset(_make_task_with_dataset(rows=rows, sample_per_stratum=3, sample_seed=7), tmp_path)
        ]
        ids2 = [
            t.row_id
            for t in expand_dataset(_make_task_with_dataset(rows=rows, sample_per_stratum=3, sample_seed=7), tmp_path)
        ]
        assert ids1 == ids2

    def test_different_seeds_draw_differently(self, tmp_path: Path) -> None:
        rows = self._stratified_rows()
        ids_a = {
            t.row_id
            for t in expand_dataset(_make_task_with_dataset(rows=rows, sample_per_stratum=3, sample_seed=1), tmp_path)
        }
        ids_b = {
            t.row_id
            for t in expand_dataset(_make_task_with_dataset(rows=rows, sample_per_stratum=3, sample_seed=2), tmp_path)
        }
        # Same per-stratum counts, but at least one drawn row differs.
        assert ids_a != ids_b

    def test_unseeded_redraws_each_run(self, tmp_path: Path) -> None:
        # sample_seed=None (the default, and how the nightly activation suite is
        # wired) must re-draw on every run — a fresh nondeterministic RNG, NOT a
        # fixed seed. Guards against anyone defaulting the seed to a constant and
        # silently freezing which rows the nightly samples. Draw several times and
        # require >1 distinct result; the odds of all draws colliding by chance are
        # ~(1/11200)^(n-1) for these strata, i.e. effectively zero.
        rows = self._stratified_rows()
        draws = {
            frozenset(
                t.row_id for t in expand_dataset(_make_task_with_dataset(rows=rows, sample_per_stratum=3), tmp_path)
            )
            for _ in range(6)
        }
        assert len(draws) > 1

    def test_seeded_is_reproducible(self, tmp_path: Path) -> None:
        # The reproducibility half of the documented --sample vs sample_per_stratum
        # divergence: with an explicit sample_seed, two independent expansions draw
        # the identical set of rows. (sample_per_stratum is nondeterministic by
        # DEFAULT — see test_unseeded_redraws_each_run — but reproducible when seeded.)
        rows = self._stratified_rows()
        first = {
            t.row_id
            for t in expand_dataset(_make_task_with_dataset(rows=rows, sample_per_stratum=3, sample_seed=99), tmp_path)
        }
        second = {
            t.row_id
            for t in expand_dataset(_make_task_with_dataset(rows=rows, sample_per_stratum=3, sample_seed=99), tmp_path)
        }
        assert first == second

    def test_custom_stratify_field(self, tmp_path: Path) -> None:
        rows = [{"id": f"r{i}", "prompt": "p", "expected": "e", "bucket": "x" if i % 2 else "y"} for i in range(10)]
        task = _make_task_with_dataset(rows=rows, sample_per_stratum=2, stratify_field="bucket", sample_seed=0)
        expanded = expand_dataset(task, tmp_path)
        assert len(expanded) == 4  # 2 buckets x 2

    def test_cli_max_rows_overrides_stratified(self, tmp_path: Path) -> None:
        # CLI --sample (flat uniform-random N) wins over sample_per_stratum.
        task = _make_task_with_dataset(rows=self._stratified_rows(), sample_per_stratum=3, sample_seed=42)
        expanded = expand_dataset(task, tmp_path, max_rows=4)
        assert len(expanded) == 4  # flat 4, NOT 3-per-stratum

    def test_arg_applies_when_yaml_absent(self, tmp_path: Path) -> None:
        # CLI --sample-per-stratum drives the cap when the task YAML has none.
        # This is how the nightly activation suite is wired: the runner injects
        # the per-skill cap without editing the skills-repo task.
        task = _make_task_with_dataset(rows=self._stratified_rows())
        assert task.dataset is not None and task.dataset.sample_per_stratum is None
        expanded = expand_dataset(task, tmp_path, sample_per_stratum=3)
        assert self._counts_by_stratum(expanded) == {"a": 3, "b": 3, "c": 2, "n": 3}

    def test_arg_overrides_yaml_value(self, tmp_path: Path) -> None:
        # When both are set, the expand_dataset arg (CLI --sample-per-stratum) wins.
        task = _make_task_with_dataset(rows=self._stratified_rows(), sample_per_stratum=2)
        expanded = expand_dataset(task, tmp_path, sample_per_stratum=4)
        assert self._counts_by_stratum(expanded) == {"a": 4, "b": 4, "c": 2, "n": 4}

    def test_max_rows_overrides_arg(self, tmp_path: Path) -> None:
        # --sample (flat) beats --sample-per-stratum, same as it beats the YAML.
        task = _make_task_with_dataset(rows=self._stratified_rows())
        expanded = expand_dataset(task, tmp_path, max_rows=4, sample_per_stratum=3)
        assert len(expanded) == 4

    def test_cli_arg_is_nondeterministic_by_default(self, tmp_path: Path) -> None:
        # The CLI --sample-per-stratum flag (no dataset sample_seed) re-draws each
        # run, exactly like the YAML-configured path. This is how the nightly
        # activation suite is wired (eval_runner/cli.py --sample-per-stratum 20,
        # activation.yaml sets no sample_seed) — it must broaden coverage by
        # sampling different rows every night, NOT freeze to one fixed slice.
        rows = self._stratified_rows()
        task = _make_task_with_dataset(rows=rows)
        assert task.dataset is not None and task.dataset.sample_seed is None
        draws = {frozenset(t.row_id for t in expand_dataset(task, tmp_path, sample_per_stratum=3)) for _ in range(6)}
        assert len(draws) > 1

    def test_dataset_seed_wins_over_cli_arg(self, tmp_path: Path) -> None:
        # An explicit dataset.sample_seed always wins — the CLI flag does not
        # override it, so the selection matches the seeded YAML-only path.
        rows = self._stratified_rows()
        yaml_only = _make_task_with_dataset(rows=rows, sample_per_stratum=3, sample_seed=99)
        with_flag = _make_task_with_dataset(rows=rows, sample_seed=99)
        ids_yaml = [t.row_id for t in expand_dataset(yaml_only, tmp_path)]
        ids_flag = [t.row_id for t in expand_dataset(with_flag, tmp_path, sample_per_stratum=3)]
        assert ids_flag == ids_yaml


class TestExpandDatasetSplit:
    """CLI --split: keep only rows whose dataset.split_field value matches.

    The filter runs BEFORE either sampler, so a sampled split still has a
    predictable size — sampling first would leave an unpredictable (possibly
    zero) number of rows per split and silently destroy the train/test
    comparison the split exists to protect.
    """

    @staticmethod
    def _split_rows() -> list[dict[str, Any]]:
        return [
            {"id": "t1", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "t2", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "t3", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "h1", "prompt": "p", "expected": "e", "split": "test"},
            {"id": "h2", "prompt": "p", "expected": "e", "split": "test"},
        ]

    def test_keeps_only_matching_rows(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=self._split_rows())
        expanded = expand_dataset(task, tmp_path, split="train")
        assert [t.row_id for t in expanded] == ["t1", "t2", "t3"]
        # task_id / row_id rewriting is unchanged by the filter.
        assert [t.task_id for t in expanded] == ["suite/t1", "suite/t2", "suite/t3"]
        assert all(t.suite_id == "suite" for t in expanded)

    def test_unlabelled_dataset_passes_through(self, tmp_path: Path) -> None:
        # --split is global to the invocation. A run containing several
        # dataset-backed tasks would otherwise fail every task that does not use
        # splits, so a task with NO labelled row at all is left whole.
        rows = [{"id": f"r{i}", "prompt": "p", "expected": "e"} for i in range(4)]
        expanded = expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="train")
        assert [t.row_id for t in expanded] == ["r0", "r1", "r2", "r3"]

    def test_partially_labelled_excludes_unlabelled_rows(self, tmp_path: Path) -> None:
        # Safe direction: an unlabelled row never leaks into a named split.
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "b", "prompt": "p", "expected": "e"},
            {"id": "c", "prompt": "p", "expected": "e", "split": ""},
        ]
        expanded = expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="train")
        assert [t.row_id for t in expanded] == ["a"]

    def test_partial_labelling_warns_with_the_drop_count(self, tmp_path: Path, caplog) -> None:
        # Dropping the unlabelled rows is the safe direction, but it must not be SILENT:
        # every metric below is then computed over a smaller suite than the file suggests.
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "b", "prompt": "p", "expected": "e"},
        ]
        with caplog.at_level("WARNING", logger="coder_eval.orchestration.task_loader"):
            expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="train")
        assert len(caplog.records) == 1
        assert "1 row(s) carry no" in caplog.text

    def test_fully_labelled_split_warns_nothing(self, tmp_path: Path, caplog) -> None:
        # The first of the two negatives, and they matter more than the positive: a warning
        # that fires when nothing was dropped trains the reader to ignore it.
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "b", "prompt": "p", "expected": "e", "split": "test"},
        ]
        with caplog.at_level("WARNING", logger="coder_eval.orchestration.task_loader"):
            expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="train")
        assert caplog.records == []

    def test_partial_labelling_without_a_split_warns_nothing(self, tmp_path: Path, caplog) -> None:
        # No selector, no drop — the state is only dangerous when --split acts on it.
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "b", "prompt": "p", "expected": "e"},
        ]
        with caplog.at_level("WARNING", logger="coder_eval.orchestration.task_loader"):
            expanded = expand_dataset(_make_task_with_dataset(rows=rows), tmp_path)
        assert [t.row_id for t in expanded] == ["a", "b"]
        assert caplog.records == []

    def test_labelled_but_unmatched_raises_listing_available_splits(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(rows=self._split_rows())
        with pytest.raises(ValueError, match="no rows in split 'Train'") as exc:
            expand_dataset(task, tmp_path, split="Train")
        # Exact match, no case normalization — the message names what does exist.
        assert "'test', 'train'" in str(exc.value)

    def test_explicit_null_split_counts_as_unlabelled(self, tmp_path: Path) -> None:
        # `"split": null` is the natural JSONL shape for "not assigned yet", and it
        # matches the null -> "" convention row substitution already uses. It must
        # NOT read as the label "None" (which `str(row.get(field, ""))` would make
        # truthy), or a half-migrated dataset fails instead of passing through and
        # the error advertises a phantom split.
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": None},
            {"id": "b", "prompt": "p", "expected": "e", "split": None},
        ]
        expanded = expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="train")
        assert [t.row_id for t in expanded] == ["a", "b"]

    def test_null_split_rows_are_excluded_from_a_named_split(self, tmp_path: Path) -> None:
        # The partial-labelling half of the same rule: a null-split row is unlabelled,
        # so it is dropped from a named split rather than joining a "None" split.
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "b", "prompt": "p", "expected": "e", "split": None},
        ]
        expanded = expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="train")
        assert [t.row_id for t in expanded] == ["a"]

    def test_zero_is_a_real_split_label(self, tmp_path: Path) -> None:
        # Guards the null fix against an `or ""` implementation, which would make the
        # falsy-but-present value 0 unlabelled. Only None/"" mean "no label".
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": 0},
            {"id": "b", "prompt": "p", "expected": "e", "split": 1},
        ]
        assert [t.row_id for t in expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="0")] == ["a"]

    def test_non_string_split_values_compare_by_str(self, tmp_path: Path) -> None:
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": 1},
            {"id": "b", "prompt": "p", "expected": "e", "split": 2},
        ]
        expanded = expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="1")
        assert [t.row_id for t in expanded] == ["a"]

    def test_custom_split_field(self, tmp_path: Path) -> None:
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "fold": "train"},
            {"id": "b", "prompt": "p", "expected": "e", "fold": "test"},
        ]
        task = _make_task_with_dataset(rows=rows, split_field="fold")
        assert [t.row_id for t in expand_dataset(task, tmp_path, split="test")] == ["b"]

    def test_filter_runs_before_max_rows(self, tmp_path: Path) -> None:
        # Ordering guard: every sampled row must still carry the requested split.
        rows = self._split_rows()
        task = _make_task_with_dataset(rows=rows)
        expanded = expand_dataset(task, tmp_path, max_rows=2, split="train")
        assert len(expanded) == 2
        assert {t.row_id for t in expanded} <= {"t1", "t2", "t3"}

    def test_filter_runs_before_sample_per_stratum(self, tmp_path: Path) -> None:
        # Same assertion on the stratified path: strata are computed WITHIN the
        # filtered set, so a test row can never be drawn under --split train.
        rows = [
            {"id": "t1", "prompt": "p", "expected": "e", "split": "train", "expected_skill": "a"},
            {"id": "t2", "prompt": "p", "expected": "e", "split": "train", "expected_skill": "a"},
            {"id": "t3", "prompt": "p", "expected": "e", "split": "train", "expected_skill": "b"},
            {"id": "h1", "prompt": "p", "expected": "e", "split": "test", "expected_skill": "a"},
            {"id": "h2", "prompt": "p", "expected": "e", "split": "test", "expected_skill": "b"},
        ]
        task = _make_task_with_dataset(rows=rows, sample_per_stratum=1, sample_seed=0)
        expanded = expand_dataset(task, tmp_path, split="train")
        assert len(expanded) == 2  # one per stratum, within train only
        assert {t.row_id for t in expanded} <= {"t1", "t2", "t3"}

    def test_split_none_is_byte_identical_to_today(self, tmp_path: Path) -> None:
        # No-regression guard: with no --split, a dataset carrying mixed split
        # values expands exactly as it did before the filter existed.
        task = _make_task_with_dataset(rows=self._split_rows())
        assert [t.row_id for t in expand_dataset(task, tmp_path)] == ["t1", "t2", "t3", "h1", "h2"]
        assert [t.row_id for t in expand_dataset(task, tmp_path, split=None)] == ["t1", "t2", "t3", "h1", "h2"]

    def test_task_without_dataset_unaffected(self, tmp_path: Path) -> None:
        task = TaskDefinition(**_base_task_dict())
        assert expand_dataset(task, tmp_path, split="train") == [task]

    def test_duplicate_ids_across_splits_are_caught_under_a_filter(self, tmp_path: Path) -> None:
        # Id uniqueness is a property of the DATASET, not of whichever split you selected.
        # If the filter ran before the duplicate check, a copy-pasted row landing in the
        # other split would validate under every --split and only blow up on a full run —
        # and the documented optimize-skill workflow always passes --split, so it would
        # never be seen at all.
        rows = [
            {"id": "a", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "a", "prompt": "p", "expected": "e", "split": "test"},
        ]
        task = _make_task_with_dataset(rows=rows)
        with pytest.raises(ValueError, match="Duplicate dataset row id"):
            expand_dataset(task, tmp_path, split="train")

    def test_malformed_row_id_in_an_unselected_split_still_raises(self, tmp_path: Path) -> None:
        # Same argument as the duplicate-id test above, for the other two id checks: a
        # malformed id is malformed data whichever split you asked for. Validated only over
        # the SELECTED rows, it would pass under every `--split train` and surface at
        # promotion time — the most expensive moment to learn it.
        rows = [
            {"id": "ok", "prompt": "p", "expected": "e", "split": "train"},
            {"id": "bad id!", "prompt": "p", "expected": "e", "split": "test"},
        ]
        with pytest.raises(ValueError, match="must match"):
            expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="train")

    def test_missing_id_field_in_an_unselected_split_still_raises(self, tmp_path: Path) -> None:
        rows = [
            {"id": "ok", "prompt": "p", "expected": "e", "split": "train"},
            {"prompt": "p", "expected": "e", "split": "test"},
        ]
        with pytest.raises(ValueError, match="missing id_field 'id'"):
            expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, split="train")

    def test_malformed_row_id_beyond_the_sample_still_raises(self, tmp_path: Path) -> None:
        # The sampler narrows the row set too, and a malformed row the sample happened to
        # drop is still a malformed row. Validation runs over the whole dataset first.
        #
        # `--sample` uses a FIXED seed, so which row it drops is deterministic — but the
        # test must not silently stop exercising the case if that seed ever changes. So
        # first establish, on a well-formed twin of the same shape, that row 0 is the one
        # the sampler excludes; that is the premise the assertion below depends on.
        shape_twin = [{"id": "first", "prompt": "p", "expected": "e"}, {"id": "second", "prompt": "p", "expected": "e"}]
        sampled = expand_dataset(_make_task_with_dataset(rows=shape_twin), tmp_path, max_rows=1)
        assert [t.row_id for t in sampled] == ["second"], (
            "the fixed sample seed no longer drops row 0; re-pick the malformed row's position"
        )

        rows = [
            {"id": "bad id!", "prompt": "p", "expected": "e"},
            {"id": "ok", "prompt": "p", "expected": "e"},
        ]
        with pytest.raises(ValueError, match="must match"):
            expand_dataset(_make_task_with_dataset(rows=rows), tmp_path, max_rows=1)

    def test_split_field_defaults_to_split(self) -> None:
        task = _make_task_with_dataset(rows=[{"id": "a"}])
        assert task.dataset is not None
        assert task.dataset.split_field == "split"


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
        task = _make_task_with_dataset(paths=["rows.jsonl"])
        expanded = expand_dataset(task, tmp_path)
        assert [t.task_id for t in expanded] == ["suite/j1", "suite/j2"]
        assert expanded[0].initial_prompt == "Prompt: jp1"

    def test_missing_file(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(paths=["does_not_exist.jsonl"])
        with pytest.raises(FileNotFoundError):
            expand_dataset(task, tmp_path)

    def test_relative_subdir_path(self, tmp_path: Path) -> None:
        # Dataset lives in a subdirectory relative to the task YAML.
        (tmp_path / "datasets").mkdir()
        ds_path = tmp_path / "datasets" / "rows.jsonl"
        ds_path.write_text(json.dumps({"id": "s1", "prompt": "sp", "expected": "se"}) + "\n")

        task = _make_task_with_dataset(paths=["datasets/rows.jsonl"])
        expanded = expand_dataset(task, tmp_path)
        assert [t.task_id for t in expanded] == ["suite/s1"]

    def test_relative_parent_path(self, tmp_path: Path) -> None:
        # Dataset lives in a sibling directory; task YAML is nested deeper.
        (tmp_path / "datasets").mkdir()
        (tmp_path / "tasks").mkdir()
        ds_path = tmp_path / "datasets" / "rows.jsonl"
        ds_path.write_text(json.dumps({"id": "p1", "prompt": "pp", "expected": "pe"}) + "\n")

        task = _make_task_with_dataset(paths=["../datasets/rows.jsonl"])
        expanded = expand_dataset(task, tmp_path / "tasks")
        assert [t.task_id for t in expanded] == ["suite/p1"]

    def test_absolute_path(self, tmp_path: Path) -> None:
        ds_path = tmp_path / "abs.jsonl"
        ds_path.write_text(json.dumps({"id": "a1", "prompt": "ap", "expected": "ae"}) + "\n")

        task = _make_task_with_dataset(paths=[str(ds_path)])
        # Pass a different task_file_dir to confirm absolute paths are honored regardless.
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        expanded = expand_dataset(task, other_dir)
        assert [t.task_id for t in expanded] == ["suite/a1"]

    def test_malformed_jsonl_line(self, tmp_path: Path) -> None:
        ds_path = tmp_path / "rows.jsonl"
        ds_path.write_text('{"id": "ok", "prompt": "p", "expected": "e"}\n{not json}\n')
        task = _make_task_with_dataset(paths=["rows.jsonl"])
        with pytest.raises(ValueError, match="invalid JSON on line 2"):
            expand_dataset(task, tmp_path)

    def test_non_object_row(self, tmp_path: Path) -> None:
        ds_path = tmp_path / "rows.jsonl"
        ds_path.write_text('["not", "an", "object"]\n')
        task = _make_task_with_dataset(paths=["rows.jsonl"])
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
        task = _make_task_with_dataset(paths=["rows.jsonl"])
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
    def test_requires_one_source(self) -> None:
        with pytest.raises(ValueError, match="either 'paths' or 'rows'"):
            Dataset()

    def test_forbids_paths_and_rows(self) -> None:
        with pytest.raises(ValueError, match="only one of"):
            Dataset(paths=["a.jsonl"], rows=[{"id": "r1"}])

    def test_forbids_empty_paths(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Dataset(paths=[])

    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValueError):
            Dataset.model_validate({"rows": [{"id": "r1"}], "unknown": "x"})


class TestExpandDatasetMultiPath:
    def test_concatenates_files_in_order(self, tmp_path: Path) -> None:
        pos = tmp_path / "pos.jsonl"
        neg = tmp_path / "neg.jsonl"
        pos.write_text(
            json.dumps({"id": "p1", "prompt": "pp1", "expected": "ee1", "label": "yes"})
            + "\n"
            + json.dumps({"id": "p2", "prompt": "pp2", "expected": "ee2", "label": "yes"})
            + "\n",
            encoding="utf-8",
        )
        neg.write_text(
            json.dumps({"id": "n1", "prompt": "nn1", "expected": "ne1", "label": "no"}) + "\n", encoding="utf-8"
        )
        task = _make_task_with_dataset(paths=["pos.jsonl", "neg.jsonl"])
        expanded = expand_dataset(task, tmp_path)
        assert [t.row_id for t in expanded] == ["p1", "p2", "n1"]
        assert all(t.dataset is None for t in expanded)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        task = _make_task_with_dataset(paths=["missing.jsonl"])
        with pytest.raises(FileNotFoundError):
            expand_dataset(task, tmp_path)


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

        resolved, _ = resolve_all_tasks(
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
        config = BatchRunConfig(run_dir=tmp_path / "runs", row_selection=RowSelection(max_rows=1))

        resolved, _ = resolve_all_tasks(
            task_files=[task_file],
            experiment=experiment,
            default_experiment=default_exp,
            config=config,
        )
        assert len(resolved) == 1
        # --sample is now a random subset (count == max_rows), not a first-N slice.
        assert resolved[0].task.task_id in {"suite/row-a", "suite/row-b"}

    def test_split_applies(self, tmp_path: Path) -> None:
        # BatchRunConfig.split threads CLI -> config -> expand_dataset with no
        # merge-layer participation (dataset expansion precedes variant resolution).
        data = {
            "task_id": "suite",
            "description": "Test",
            "initial_prompt": "Prompt: ${row.prompt}",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "out.txt", "description": "File"}],
            "dataset": {
                "rows": [
                    {"id": "row-a", "prompt": "a", "split": "train"},
                    {"id": "row-b", "prompt": "b", "split": "test"},
                ]
            },
        }
        task_file = tmp_path / "suite.yaml"
        task_file.write_text(yaml.safe_dump(data))
        default_exp, experiment = self._make_experiment(["v1"])
        config = BatchRunConfig(run_dir=tmp_path / "runs", row_selection=RowSelection(split="train"))

        resolved, _ = resolve_all_tasks(
            task_files=[task_file],
            experiment=experiment,
            default_experiment=default_exp,
            config=config,
        )
        assert [rt.task.task_id for rt in resolved] == ["suite/row-a"]

    def _write_split_suite(self, tmp_path: Path, task_id: str, splits: list[str | None]) -> Path:
        rows: list[dict[str, Any]] = []
        for i, split in enumerate(splits):
            row: dict[str, Any] = {"id": f"row-{i}", "prompt": str(i)}
            if split is not None:
                row["split"] = split
            rows.append(row)
        data = {
            "task_id": task_id,
            "description": "Test",
            "initial_prompt": "Prompt: ${row.prompt}",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "out.txt", "description": "File"}],
            "dataset": {"rows": rows},
        }
        p = tmp_path / f"{task_id}.yaml"
        p.write_text(yaml.safe_dump(data))
        return p

    def test_split_leaves_an_unlabelled_sibling_suite_whole(self, tmp_path: Path) -> None:
        # The actual reason unlabelled datasets pass through: --split is global to the
        # invocation, so a run mixing a split-labelled suite with an unlabelled one must
        # filter the first and leave the second entirely alone. Asserting this at the
        # resolver (not on one isolated expand_dataset call) is what proves the claim.
        labelled = self._write_split_suite(tmp_path, "labelled", ["train", "test"])
        unlabelled = self._write_split_suite(tmp_path, "plain", [None, None, None])
        default_exp, experiment = self._make_experiment(["v1"])

        resolved, skipped = resolve_all_tasks(
            task_files=[labelled, unlabelled],
            experiment=experiment,
            default_experiment=default_exp,
            config=BatchRunConfig(run_dir=tmp_path / "runs", row_selection=RowSelection(split="train")),
        )
        assert not skipped
        assert sorted(rt.task.task_id for rt in resolved) == [
            "labelled/row-0",  # the one train row
            "plain/row-0",  # unlabelled suite survives whole
            "plain/row-1",
            "plain/row-2",
        ]

    def test_unmatched_split_aborts_instead_of_demoting_to_a_skipped_task(self, tmp_path: Path) -> None:
        # A labelled suite with no row in the requested split ABORTS the run. It used to
        # demote to a SkippedTask like any load failure, which produced the worst outcome
        # in the whole split workflow: one yellow line, zero evals run, exit 0 — a CI gate
        # reporting success for a one-character typo. The other dataset errors describe a
        # malformed FILE and stay demoted (one bad task must not abort a suite); this one
        # describes a malformed INVOCATION, and the same selector applies to every task in
        # the run, so there is no per-task isolation argument for it.
        #
        # The message is still the only place a user learns what they should have typed,
        # so both halves stay pinned.
        task_file = self._write_split_suite(tmp_path, "labelled", ["train", "test"])
        default_exp, experiment = self._make_experiment(["v1"])

        with pytest.raises(SplitSelectorError) as exc:
            resolve_all_tasks(
                task_files=[task_file],
                experiment=experiment,
                default_experiment=default_exp,
                config=BatchRunConfig(run_dir=tmp_path / "runs", row_selection=RowSelection(split="holdou")),
            )
        assert "no rows in split 'holdou'" in str(exc.value)
        assert "'test', 'train'" in str(exc.value)

    def test_split_selector_error_is_a_value_error(self) -> None:
        # Every existing `except ValueError` caller depends on this, including the CLI
        # seam in run_command.py that turns it into a typer.BadParameter.
        assert issubclass(SplitSelectorError, ValueError)

    def test_unlabelled_task_in_a_multi_task_run_is_unaffected(self, tmp_path: Path) -> None:
        # Guards the `if labelled:` placement: the abort must fire only for a task that
        # CARRIES split labels. A suite with none passes through unfiltered even while a
        # sibling suite is being filtered by the same selector.
        labelled = self._write_split_suite(tmp_path, "labelled", ["train", "test"])
        plain = self._write_split_suite(tmp_path, "plain", [None, None])
        default_exp, experiment = self._make_experiment(["v1"])

        resolved, skipped = resolve_all_tasks(
            task_files=[labelled, plain],
            experiment=experiment,
            default_experiment=default_exp,
            config=BatchRunConfig(run_dir=tmp_path / "runs", row_selection=RowSelection(split="train")),
        )
        assert skipped == []
        assert sorted(rt.task.task_id for rt in resolved) == ["labelled/row-0", "plain/row-0", "plain/row-1"]

    def test_unmatched_split_surfaces_as_a_cli_bad_parameter(self, tmp_path: Path) -> None:
        # The CLI seam, at the cheaper of the two levels: `run_command.py` wraps its
        # resolve_all_tasks call in `except ValueError -> typer.BadParameter`, and a full
        # `coder-eval run` invocation would need credentials and a sandbox. typer.BadParameter
        # exits **2**, not 1 — do not "fix" a later assertion to 1.
        import typer

        task_file = self._write_split_suite(tmp_path, "labelled", ["train", "test"])
        default_exp, experiment = self._make_experiment(["v1"])

        with pytest.raises(typer.BadParameter) as exc:
            try:
                resolve_all_tasks(
                    task_files=[task_file],
                    experiment=experiment,
                    default_experiment=default_exp,
                    config=BatchRunConfig(run_dir=tmp_path / "runs", row_selection=RowSelection(split="holdou")),
                )
            except ValueError as e:  # exactly what cli/run_command.py does
                raise typer.BadParameter(str(e)) from e
        assert "'test', 'train'" in str(exc.value)

    def test_non_dataset_task_unaffected(self, tmp_path: Path) -> None:
        task_file = self._write_task_yaml(tmp_path, "plain", with_dataset=False)
        default_exp, experiment = self._make_experiment(["v1"])
        config = BatchRunConfig(run_dir=tmp_path / "runs", row_selection=RowSelection(max_rows=99))

        resolved, _ = resolve_all_tasks(
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

        resolved, _ = resolve_all_tasks(
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

        resolved, _ = resolve_all_tasks(
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

        resolved, _ = resolve_all_tasks([task_file], experiment, default_exp, config)
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
        resolved, _ = resolve_all_tasks([task_file], experiment, default_exp, config)

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

    def test_config_resolution_failure_isolated_to_skipped(self, tmp_path: Path) -> None:
        """A per-task layer-5 resolution failure skips just that task, not the suite.

        `--type codex` (config.agent_type) rewrites a task whose YAML carries the
        Claude-only `sdk_options` into a CodexAgentConfig, which forbids that field
        (`extra="forbid"`). The incompatibility raises during layer-5 resolution;
        the task must land in `skipped` while a sibling still resolves — rather than
        aborting the whole coder-eval run (as it did before this isolation).
        """
        from coder_eval.orchestration.config import BatchRunConfig
        from coder_eval.orchestration.experiment import resolve_all_tasks

        good = {
            "task_id": "plain-task",
            "description": "d",
            "initial_prompt": "p",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "f.py", "description": "d"}],
        }
        # Valid as claude-code (sdk_options is Claude-only); becomes invalid only
        # once --type codex rewrites the agent kind at layer 5.
        claude_only = {
            **good,
            "task_id": "claude-only-task",
            "agent": {"type": "claude-code", "sdk_options": {"effort": "high"}},
        }
        good_file = tmp_path / "good.yaml"
        good_file.write_text(yaml.dump(good))
        bad_file = tmp_path / "claude_only.yaml"
        bad_file.write_text(yaml.dump(claude_only))

        experiment = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        # --type codex applied to every task (layer 5, highest precedence).
        config = BatchRunConfig(run_dir=tmp_path / "runs", agent_type="codex")

        resolved, skipped = resolve_all_tasks([good_file, bad_file], experiment, experiment, config)

        # Sibling resolved; the incompatible task is isolated (no raise).
        assert [rt.task.task_id for rt in resolved] == ["plain-task"]
        assert len(skipped) == 1
        assert skipped[0].path == str(bad_file)
        assert "sdk_options" in skipped[0].reason

    def test_config_resolution_failure_skips_whole_file_no_partial_fanout(self, tmp_path: Path) -> None:
        """A variant that fails resolution rolls back its whole file's fan-out.

        `bad.yaml` carries Claude-only `sdk_options`: it resolves cleanly under
        the claude-code variant but fails under the codex variant (which forbids
        the field). Because a file's resolved tasks are buffered and committed as
        a unit, the clean claude-code entry is rolled back with the failing codex
        one — the file is skipped whole, never left as a lopsided partial fan-out
        that would skew an A/B comparison. Sibling `good.yaml` (no sdk_options)
        resolves under both variants, so the failure is isolated, not a global
        abort.
        """
        from coder_eval.orchestration.config import BatchRunConfig
        from coder_eval.orchestration.experiment import resolve_all_tasks

        def _task(task_id: str, agent: dict[str, Any] | None = None) -> dict[str, Any]:
            data: dict[str, Any] = {
                "task_id": task_id,
                "description": "d",
                "initial_prompt": "p",
                "sandbox": {"driver": "tempdir"},
                "success_criteria": [{"type": "file_exists", "path": "f.py", "description": "d"}],
            }
            if agent is not None:
                data["agent"] = agent
            return data

        good_file = tmp_path / "good.yaml"
        good_file.write_text(yaml.dump(_task("good-task")))
        bad_file = tmp_path / "bad.yaml"
        # Claude-only sdk_options: valid under the claude-code variant, forbidden
        # once the codex variant rewrites the agent kind.
        bad_file.write_text(
            yaml.dump(_task("bad-task", agent={"type": "claude-code", "sdk_options": {"effort": "high"}}))
        )

        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[
                ExperimentVariant(variant_id="as-claude"),
                ExperimentVariant(variant_id="as-codex", agent={"type": "codex"}),
            ],
        )
        config = BatchRunConfig(run_dir=tmp_path / "runs")

        resolved, skipped = resolve_all_tasks([good_file, bad_file], experiment, experiment, config)

        # good.yaml resolves for BOTH variants; bad.yaml is skipped whole — its
        # clean claude-code entry is rolled back with the failing codex one.
        assert {rt.task.task_id for rt in resolved} == {"good-task"}
        assert sorted(rt.variant_id for rt in resolved) == ["as-claude", "as-codex"]
        assert len(skipped) == 1
        assert skipped[0].path == str(bad_file)
        assert "sdk_options" in skipped[0].reason

    def test_every_task_failing_resolution_aborts_as_value_error(self, tmp_path: Path) -> None:
        """When EVERY attempted task fails resolution, abort rather than return empty.

        Two task files that BOTH carry Claude-only `sdk_options` under `--type
        codex`: with no sibling resolving, `len(resolution_errors) == attempted`,
        so the suite must not silently produce an empty run. It aborts with the
        first task's own error (a Pydantic ValidationError — a ValueError — so it
        surfaces verbatim through the caller's `except ValueError`), never a
        silent empty resolved list.
        """

        def _claude_only(task_id: str) -> dict[str, Any]:
            return {
                "task_id": task_id,
                "description": "d",
                "initial_prompt": "p",
                "sandbox": {"driver": "tempdir"},
                "success_criteria": [{"type": "file_exists", "path": "f.py", "description": "d"}],
                "agent": {"type": "claude-code", "sdk_options": {"effort": "high"}},
            }

        file_a = tmp_path / "a.yaml"
        file_a.write_text(yaml.dump(_claude_only("task-a")))
        file_b = tmp_path / "b.yaml"
        file_b.write_text(yaml.dump(_claude_only("task-b")))

        experiment = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        config = BatchRunConfig(run_dir=tmp_path / "runs", agent_type="codex")

        with pytest.raises(ValueError, match="sdk_options"):
            resolve_all_tasks([file_a, file_b], experiment, experiment, config)

    def test_single_file_non_value_error_surfaces_as_value_error(self, tmp_path: Path) -> None:
        """The all-fail abort normalizes a non-ValueError reason to ValueError.

        A lone task file whose variant injects a missing `system_prompt_file`
        raises FileNotFoundError (an OSError, not a ValueError) during layer-5
        resolution. With a single file, `len(resolution_errors) == attempted == 1`
        fires the all-fail branch. It must re-raise as a ``ValueError`` so the
        caller's `except ValueError` catches it (clean CLI error) instead of a
        FileNotFoundError escaping as a raw traceback — while preserving the
        original message ("system_prompt_file not found").
        """
        task = {
            "task_id": "missing-prompt-task",
            "description": "d",
            "initial_prompt": "p",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "path": "f.py", "description": "d"}],
        }
        task_file = tmp_path / "task.yaml"
        task_file.write_text(yaml.dump(task))

        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="v1", agent={"system_prompt_file": "does-not-exist.txt"})],
        )
        config = BatchRunConfig(run_dir=tmp_path / "runs")

        with pytest.raises(ValueError, match="system_prompt_file not found"):
            resolve_all_tasks([task_file], experiment, experiment, config, experiment_file=tmp_path / "exp.yaml")
