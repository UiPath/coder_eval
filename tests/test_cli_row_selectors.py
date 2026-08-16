"""The three row selectors (``--split`` / ``--sample`` / ``--sample-per-stratum``) must
survive every hop from the command line to the batch config.

Nothing in the tree exercised that path. ``tests/test_dataset_expansion.py`` hand-builds
``BatchRunConfig(row_selection=RowSelection(split="train"))`` and ``tests/test_plan_command.py``
passes ``split=`` as a Python kwarg — both start *downstream* of the CLI, so the two hops
where the wiring can break silently were untested:

1. ``run_command`` → ``_run_all_tasks``, where the parameter is **renamed** (`sample` →
   `max_rows`); and
2. ``_run_all_tasks`` → ``BatchRunConfig``, where a dropped keyword would leave the
   selector at its ``None`` default and run the whole suite instead of the requested
   subset — a full-cost run that reports as the subset the user asked for.

Plus the documented ``SplitSelectorError`` → ``typer.BadParameter`` conversion, which is
what stops a mistyped split name producing a green run over zero rows.

Deliberately no end-to-end invocation that reaches a real batch: that needs credentials
and a sandbox, and the hops above are fully covered without executing an agent.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.cli.run_command import _run_with_experiment
from coder_eval.models import ROW_SELECTOR_FLAGS, Dataset, RowSelection
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.task_loader import (
    STRATIFIED_CAUSE_PREFIXES,
    SplitSelectorError,
    expand_dataset,
    expand_dataset_with_selection,
    row_split_label,
    select_rows,
    stratum_key,
)


runner = CliRunner()

_TASK = "tasks/hello_date.yaml"


def _recording_run_all_tasks(sink: dict[str, Any]):
    """An async stand-in for ``_run_all_tasks`` that records how it was called.

    Must be a coroutine function: ``run_command`` hands the result to ``asyncio.run``,
    and a plain function would raise ``TypeError`` there — the test would then "pass"
    on a swallowed exception rather than on the wiring it means to pin. Every assertion
    below therefore also checks ``exit_code == 0``.
    """

    async def _stand_in(*args: object, **kwargs: object) -> None:
        sink.update(kwargs)

    return _stand_in


class TestRunForwardsRowSelectors:
    """``coder-eval run``'s selector flags reach the async batch entry point."""

    def test_run_forwards_every_row_selector_to_the_batch_entry_point(self) -> None:
        captured: dict[str, Any] = {}
        with patch("coder_eval.cli.run_command._run_all_tasks", _recording_run_all_tasks(captured)):
            result = runner.invoke(
                app,
                ["run", _TASK, "--split", "test", "--sample", "3", "--sample-per-stratum", "2"],
            )
        assert result.exit_code == 0, result.output
        # Asserted by KEYWORD, not positional index: these arrive as kwargs and a
        # positional assertion would break on any harmless signature reshuffle. Note the
        # rename across the hop — the CLI option is `--sample`, the kwarg is `max_rows`.
        assert captured["split"] == "test"
        assert captured["max_rows"] == 3
        assert captured["sample_per_stratum"] == 2

    def test_run_without_selectors_passes_none_for_all_three(self) -> None:
        captured: dict[str, Any] = {}
        with patch("coder_eval.cli.run_command._run_all_tasks", _recording_run_all_tasks(captured)):
            result = runner.invoke(app, ["run", _TASK])
        assert result.exit_code == 0, result.output
        assert captured["split"] is None
        assert captured["max_rows"] is None
        assert captured["sample_per_stratum"] is None


class TestRowSelectorsReachTheBatchConfig:
    """The second hop: ``_run_all_tasks`` → ``BatchRunConfig``.

    This is the one the pre-existing coverage never reached — every other test that
    exercises ``--split`` builds the config by hand, so a dropped keyword here would
    run the whole suite while the report still claimed the requested subset.
    """

    def test_run_split_reaches_batch_run_config(self, tmp_path: Path) -> None:
        captured: list[BatchRunConfig] = []

        async def _stand_in(_files, config: BatchRunConfig, *args: object, **kwargs: object) -> None:
            captured.append(config)
            # Stop here rather than returning a fabricated (summary, gate_count) pair:
            # everything after this call in _run_all_tasks is reporting, not wiring.
            raise typer.Exit(0)

        with patch("coder_eval.cli.run_command._run_with_experiment", _stand_in):
            result = runner.invoke(
                app,
                ["run", _TASK, "--run-dir", str(tmp_path / "run"), "--split", "test"],
            )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        assert captured[0].row_selection.split == "test"

    def test_run_samplers_reach_batch_run_config(self, tmp_path: Path) -> None:
        captured: list[BatchRunConfig] = []

        async def _stand_in(_files, config: BatchRunConfig, *args: object, **kwargs: object) -> None:
            captured.append(config)
            raise typer.Exit(0)

        with patch("coder_eval.cli.run_command._run_with_experiment", _stand_in):
            result = runner.invoke(
                app,
                ["run", _TASK, "--run-dir", str(tmp_path / "run"), "--sample", "3", "--sample-per-stratum", "2"],
            )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        assert captured[0].row_selection.max_rows == 3
        assert captured[0].row_selection.sample_per_stratum == 2


async def test_an_unmatched_split_becomes_a_typer_bad_parameter(tmp_path: Path) -> None:
    """A mistyped ``--split`` must abort the run, not yield a green zero-row one.

    ``run_command`` never names ``SplitSelectorError`` — it catches the base ``ValueError``
    at the ``resolve_all_tasks`` call — so this pins the conversion rather than the class.
    The patch targets the **defining** module because ``_run_with_experiment`` imports
    ``resolve_all_tasks`` lazily inside its own body; ``coder_eval.cli.run_command.
    resolve_all_tasks`` does not exist as a module attribute and patching it would raise.
    """
    message = (
        "Dataset for task 'x' has no rows in split 'typo' (split_field='split'); "
        "labelled splits present: ['test', 'train']"
    )

    def _raise(**_kwargs: object) -> None:
        raise SplitSelectorError(message)

    with (
        patch("coder_eval.orchestration.experiment.resolve_all_tasks", _raise),
        pytest.raises(typer.BadParameter) as excinfo,
    ):
        await _run_with_experiment([Path(_TASK)], BatchRunConfig(run_dir=tmp_path), None, None, 1)

    # The splits that DO exist must survive the conversion — that list is the whole
    # value of the error to someone who typo'd a selector.
    assert "labelled splits present: ['test', 'train']" in str(excinfo.value)
    # typer.BadParameter exits 2 (click's usage-error code), not 1. Pinned so nobody
    # "fixes" it to 1 and breaks the CI contract for a usage error.
    assert excinfo.value.exit_code == 2


class TestRowSelectionModel:
    """The one declaration of the three selectors."""

    def test_every_selector_defaults_to_none_and_nothing_is_requested(self) -> None:
        selection = RowSelection()
        assert selection.split is None
        assert selection.max_rows is None
        assert selection.sample_per_stratum is None
        assert selection.requested is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [("split", "test"), ("max_rows", 1), ("sample_per_stratum", 1)],
    )
    def test_any_single_selector_makes_it_requested(self, field: str, value: object) -> None:
        assert RowSelection(**{field: value}).requested is True

    @pytest.mark.parametrize("field", ["max_rows", "sample_per_stratum"])
    def test_a_zero_count_is_rejected(self, field: str) -> None:
        # ge=1 mirrors the CLI's min=1, so a value the flag could never produce cannot be
        # recorded by hand either.
        with pytest.raises(ValidationError):
            RowSelection(**{field: 0})

    def test_an_unknown_key_is_ignored_rather_than_rejected(self) -> None:
        """Pinning the deliberate ABSENCE of extra="forbid" so nobody "fixes" it.

        run.json is read back by `reports.py` and `reports_junit.generate_junit_xml` from run
        directories that may have been written by a NEWER coder-eval (runs are pulled from
        blob storage). Forbidding extras here would turn a future fourth selector into a
        hard parse failure of the entire report instead of an ignored key.
        """
        selection = RowSelection.model_validate({"split": "test", "sample_by_vibes": 7})
        assert selection.split == "test"
        assert not hasattr(selection, "sample_by_vibes")

    def test_the_flag_map_covers_exactly_the_model_fields(self) -> None:
        # Asserted at the SOURCE of the mapping: a fourth field on the model without a flag
        # entry would render as nothing on every surface that prints selectors.
        assert set(ROW_SELECTOR_FLAGS) == set(RowSelection.model_fields)

    def test_batch_run_config_round_trips_its_selection(self) -> None:
        config = BatchRunConfig(run_dir=Path("runs/x"), row_selection=RowSelection(split="train"))
        recovered = BatchRunConfig.model_validate(config.model_dump(mode="json"))
        assert recovered.row_selection.split == "train"

    def test_batch_run_config_defaults_to_an_empty_selection_not_none(self) -> None:
        # The config side and the summary side default DIFFERENTLY on purpose: a run always
        # HAS a (possibly empty) selection, while a summary's None means "not recorded".
        assert BatchRunConfig(run_dir=Path("runs/x")).row_selection.requested is False


class TestStratumKey:
    """The sampler's grouping rule, now owned by one function.

    `plan`'s per-stratum preview groups through this, so the counts it prints describe the
    strata `--sample-per-stratum` actually draws from. A preview with its own grouping would
    report numbers for strata the sampler does not use.
    """

    def test_a_missing_field_and_an_empty_string_share_the_empty_key(self) -> None:
        assert stratum_key({}, "expected_skill") == ""
        assert stratum_key({"expected_skill": ""}, "expected_skill") == ""

    def test_an_explicit_null_becomes_the_string_none(self) -> None:
        """The documented cost of folding a missing key to "". Deliberately NOT
        `row_split_label`'s convention, which treats absent / None / "" alike — the split
        filter cannot tolerate an explicit null silently becoming a real label."""
        assert stratum_key({"expected_skill": None}, "expected_skill") == "None"
        assert row_split_label({"expected_skill": None}, "expected_skill") is None

    def test_the_two_conventions_are_deliberately_different(self) -> None:
        row = {"split": None}
        assert stratum_key(row, "split") == "None"
        assert row_split_label(row, "split") is None


class TestSelectRowsAppliedCauses:
    """`applied` names a selector only when it actually removed a row."""

    @staticmethod
    def _rows(n: int, **extra: object) -> list[dict[str, Any]]:
        return [{"id": f"r{i}", **extra} for i in range(n)]

    def test_a_selector_that_removed_nothing_is_absent(self) -> None:
        outcome = select_rows(self._rows(4), Dataset(rows=[{"id": "x"}]), task_id="t", max_rows=99)
        assert outcome.applied == ()
        assert len(outcome.rows) == 4

    def test_a_split_that_kept_every_row_is_absent(self) -> None:
        rows = self._rows(3, split="train")
        outcome = select_rows(rows, Dataset(rows=[{"id": "x"}]), task_id="t", split="train")
        assert outcome.applied == ()

    def test_a_cli_sourced_stratified_count_names_the_flag(self) -> None:
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)]
        dataset = Dataset(rows=[{"id": "x"}], sample_seed=1)
        outcome = select_rows(rows, dataset, task_id="t", sample_per_stratum=1)
        assert outcome.applied == ("--sample-per-stratum 1",)

    def test_a_yaml_sourced_stratified_count_names_the_yaml_key(self) -> None:
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)]
        dataset = Dataset(rows=[{"id": "x"}], sample_per_stratum=1, sample_seed=1)
        outcome = select_rows(rows, dataset, task_id="t")
        assert outcome.applied == ("dataset.sample_per_stratum: 1",)

    def test_sample_beats_sample_per_stratum_and_only_it_is_named(self) -> None:
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(4)]
        dataset = Dataset(rows=[{"id": "x"}], sample_seed=1)
        outcome = select_rows(rows, dataset, task_id="t", max_rows=2, sample_per_stratum=1)
        assert outcome.applied == ("--sample 2",)
        assert len(outcome.rows) == 2

    def test_split_is_named_first_when_both_narrow(self) -> None:
        rows = [{"id": f"a{i}", "expected_skill": "alpha", "split": "train"} for i in range(3)]
        rows += [{"id": "b0", "expected_skill": "alpha", "split": "test"}]
        dataset = Dataset(rows=[{"id": "x"}], sample_seed=1)
        outcome = select_rows(rows, dataset, task_id="t", split="train", sample_per_stratum=1)
        assert outcome.applied == ("--split train", "--sample-per-stratum 1")

    def test_every_stratified_cause_select_rows_emits_matches_the_shared_prefixes(self) -> None:
        """`plan`'s nondeterminism warning tests each cause against STRATIFIED_CAUSE_PREFIXES.

        Asserted against causes `select_rows` ACTUALLY PRODUCES, not against hard-coded
        literals: the point is that the producer's format and the consumer's prefixes cannot
        drift apart, and a test comparing two literals to each other would keep passing while
        the real cause was reworded and the warning silently stopped firing. Both sources are
        exercised because the two spellings differ — `--sample-per-stratum` is hyphenated,
        `dataset.sample_per_stratum` is not.
        """
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)]
        cli = select_rows(rows, Dataset(rows=[{"id": "x"}], sample_seed=1), task_id="t", sample_per_stratum=1)
        yaml_sourced = select_rows(rows, Dataset(rows=[{"id": "x"}], sample_per_stratum=1, sample_seed=1), task_id="t")
        for outcome in (cli, yaml_sourced):
            assert outcome.applied, "expected a stratified narrowing to be reported"
            assert all(cause.startswith(STRATIFIED_CAUSE_PREFIXES) for cause in outcome.applied)


class TestExpandDatasetWrapperIsAWrapper:
    def test_both_entry_points_return_the_same_rows_for_seeded_inputs(self, tmp_path: Path) -> None:
        from coder_eval.models import TaskDefinition

        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(4)]
        rows += [{"id": f"b{i}", "expected_skill": "beta"} for i in range(4)]
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="Do ${row.id}",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "description": "c", "path": "o.txt"}],
            dataset=Dataset(rows=rows, sample_seed=3),
        )
        plain = expand_dataset(task, tmp_path, sample_per_stratum=2)
        wrapped, outcome = expand_dataset_with_selection(task, tmp_path, sample_per_stratum=2)
        assert [t.task_id for t in plain] == [t.task_id for t in wrapped]
        assert len(outcome.rows) == len(wrapped)


class TestPlanPreviewsWhatRunExecutes:
    """B5: the row count `plan` prints must equal the row count `run` resolves.

    `plan` is only worth running if it previews the invocation you are about to pay for.
    Both halves are asserted — the STRUCTURAL one (the selected rows vs what
    `resolve_all_tasks` produced) and the PRINTED one (the "N selected" token) — so neither
    the selection nor its rendering can drift alone. `dataset.sample_seed` is pinned
    throughout, since an unseeded stratified draw is re-drawn per call by design.
    """

    @staticmethod
    def _write_suite(tmp_path: Path) -> Path:
        rows = [{"id": f"a{i}", "expected_skill": "alpha", "split": "train"} for i in range(3)]
        rows += [{"id": f"b{i}", "expected_skill": "beta", "split": "train"} for i in range(3)]
        rows += [{"id": f"c{i}", "expected_skill": "alpha", "split": "test"} for i in range(2)]
        data = {
            "task_id": "suite",
            "description": "d",
            "initial_prompt": "Do ${row.id}",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [{"type": "file_exists", "description": "c", "path": "o.txt"}],
            "dataset": {"rows": rows, "sample_seed": 11},
        }
        path = tmp_path / "suite.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    @staticmethod
    def _resolved_ids(task_file: Path, tmp_path: Path, **selectors: object) -> set[str]:
        from coder_eval.models import ExperimentDefaults, ExperimentDefinition, ExperimentVariant
        from coder_eval.orchestration.experiment import resolve_all_tasks

        # The agent type comes from the DEFAULT experiment's defaults, exactly as
        # experiments/default.yaml supplies it in a real run.
        default_exp = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code"}),
            variants=[ExperimentVariant(variant_id="v1")],
        )
        experiment = ExperimentDefinition(
            experiment_id="e",
            description="d",
            variants=[ExperimentVariant(variant_id="v1")],
        )
        resolved, _ = resolve_all_tasks(
            task_files=[task_file],
            experiment=experiment,
            default_experiment=default_exp,
            config=BatchRunConfig(run_dir=tmp_path / "runs", row_selection=RowSelection(**selectors)),  # type: ignore[arg-type]
        )
        return {rt.task.task_id for rt in resolved}

    @pytest.mark.parametrize(
        "selectors",
        [
            {"split": "train"},
            {"split": "train", "max_rows": 2},
            {"split": "train", "sample_per_stratum": 1},
            {"sample_per_stratum": 2},
        ],
        ids=["split", "split+sample", "split+per-stratum", "per-stratum"],
    )
    def test_plan_previews_the_row_count_run_resolves(self, tmp_path: Path, selectors: dict) -> None:
        from coder_eval.cli.plan_command import _preview_dataset
        from coder_eval.orchestration.task_loader import load_task

        task_file = self._write_suite(tmp_path)
        task, _ = load_task(task_file)

        run_ids = self._resolved_ids(task_file, tmp_path, **selectors)

        # `_preview_dataset` now writes through an `emit` sink rather than the console, so the
        # caller can BUFFER a file's whole preview and print its ✓/✗ banner once, after
        # everything that could fail has run.
        emitted: list[str] = []
        previewed = _preview_dataset(
            task,
            task_file,
            split_name=selectors.get("split"),
            max_rows=selectors.get("max_rows"),
            sample_per_stratum=selectors.get("sample_per_stratum"),
            emit=emitted.append,
        )
        printed = " ".join(emitted)

        # Structural half: plan selected exactly what run resolved.
        assert {t.task_id for t in previewed} == run_ids
        # Rendering half: the number a reader actually sees matches it too.
        assert f"{len(run_ids)} selected" in printed

    def test_a_seeded_stratified_preview_selects_the_same_rows_not_just_the_same_count(self, tmp_path: Path) -> None:
        """What pinning `dataset.sample_seed` buys: identity, not merely cardinality."""
        from coder_eval.cli.plan_command import _preview_dataset
        from coder_eval.orchestration.task_loader import load_task

        task_file = self._write_suite(tmp_path)
        task, _ = load_task(task_file)
        run_ids = self._resolved_ids(task_file, tmp_path, sample_per_stratum=1)

        previewed = _preview_dataset(task, task_file, sample_per_stratum=1, emit=lambda _line: None)

        assert {t.task_id for t in previewed} == run_ids


class TestBatchRunConfigDeprecatedRowSelectors:
    """`max_rows` / `sample_per_stratum` were FIELDS on this model before `row_selection`.

    Collapsing them into one nested model is right — it is the same declaration `run.json`
    records — but `BatchRunConfig` declares `extra="forbid"`, so the old spelling went straight
    from working to a hard `ValidationError` with no deprecation step. `run_batch` is a public API
    and the nightly/dashboard consumer lives in a separate repo, so that break lands out of tree.
    """

    def _cfg(self, tmp_path: Path, **kwargs) -> BatchRunConfig:
        return BatchRunConfig(run_dir=tmp_path, **kwargs)

    def test_max_rows_folds_and_warns(self, tmp_path: Path) -> None:
        with pytest.warns(DeprecationWarning, match=r"removed in 0\.11\.0"):
            config = self._cfg(tmp_path, max_rows=5)
        assert config.row_selection.max_rows == 5

    def test_sample_per_stratum_folds_and_warns(self, tmp_path: Path) -> None:
        with pytest.warns(DeprecationWarning, match=r"removed in 0\.11\.0"):
            config = self._cfg(tmp_path, sample_per_stratum=3)
        assert config.row_selection.sample_per_stratum == 3

    def test_both_aliases_fold_into_one_selection(self, tmp_path: Path) -> None:
        with pytest.warns(DeprecationWarning):
            config = self._cfg(tmp_path, max_rows=5, sample_per_stratum=3)
        assert (config.row_selection.max_rows, config.row_selection.sample_per_stratum) == (5, 3)

    def test_an_alias_together_with_row_selection_raises(self, tmp_path: Path) -> None:
        # A caller mid-migration passing both has a bug; choosing one for them hides it.
        with pytest.raises(ValidationError, match="pass one or the other"):
            self._cfg(tmp_path, max_rows=5, row_selection=RowSelection(max_rows=9))

    def test_no_alias_means_no_warning(self, tmp_path: Path) -> None:
        # The common path, and it must stay silent — a deprecation that fires on every ordinary
        # construction is one every caller learns to filter out.
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            config = self._cfg(tmp_path)
        assert config.row_selection == RowSelection()

    def test_an_unknown_key_still_raises(self, tmp_path: Path) -> None:
        """THE anti-over-fix test: `extra="forbid"` must not be weakened.

        The `mode="before"` validator sees every key, so a too-eager implementation could swallow
        anything it did not recognise.
        """
        with pytest.raises(ValidationError):
            self._cfg(tmp_path, max_rowz=5)

    def test_split_is_not_an_alias(self, tmp_path: Path) -> None:
        # `split` WAS briefly a flat field here (f1b6abb -> d5301a6) but only on this unreleased
        # branch, so no shipped caller can be using it; v0.9.6 carries only max_rows and
        # sample_per_stratum. Advertising an alias for a spelling that never shipped is churn.
        with pytest.raises(ValidationError):
            self._cfg(tmp_path, split="train")

    def test_the_dump_is_identical_either_way(self, tmp_path: Path) -> None:
        """Fingerprint stability. `compute_run_fingerprint` dumps the whole config, so if the
        alias survived into the dump every existing `--resume` would see a changed fingerprint.
        The fold happens pre-validation, so it cannot.
        """
        with pytest.warns(DeprecationWarning):
            aliased = self._cfg(tmp_path, max_rows=5, sample_per_stratum=3)
        explicit = self._cfg(tmp_path, row_selection=RowSelection(max_rows=5, sample_per_stratum=3))
        assert aliased.model_dump() == explicit.model_dump()
        assert "max_rows" not in aliased.model_dump()

    def test_the_alias_list_is_frozen_and_still_names_real_fields(self) -> None:
        """The list is a statement about a HISTORICAL API, so it is frozen rather than derived.

        Deriving it from `RowSelection.model_fields` would make a fourth selector added later
        silently become an accepted flat alias, warning about a 0.11.0 removal for a spelling that
        never shipped. Both halves are asserted: the exact frozen set, and that every entry is
        still a real `RowSelection` field — a rename there must not leave a dead alias behind.
        """
        from coder_eval.orchestration.config import _DEPRECATED_ROW_SELECTORS

        assert _DEPRECATED_ROW_SELECTORS == ("max_rows", "sample_per_stratum")
        assert set(_DEPRECATED_ROW_SELECTORS) <= set(RowSelection.model_fields)

    def test_it_does_not_mutate_the_callers_dict(self, tmp_path: Path) -> None:
        """`model_validate` hands the validator the CALLER'S dict; popping rewrites it in place.

        Measured before the copy: a caller who built kwargs from JSON got their `max_rows`
        replaced by a `RowSelection` object, and a later `json.dumps(payload)` raised TypeError —
        and that caller is exactly the out-of-tree consumer this deprecation exists to protect.
        """
        payload: dict[str, Any] = {"run_dir": str(tmp_path), "max_rows": 5}
        with pytest.warns(DeprecationWarning):
            BatchRunConfig.model_validate(payload)
        assert payload == {"run_dir": str(tmp_path), "max_rows": 5}
        json.dumps(payload)  # would raise if a RowSelection had been spliced in

    def test_the_raise_path_does_not_mutate_either(self, tmp_path: Path) -> None:
        # A caller catching the error and retrying must not find a silently altered payload.
        payload: dict[str, Any] = {
            "run_dir": str(tmp_path),
            "max_rows": 5,
            "row_selection": {"max_rows": 9},
        }
        before = dict(payload)
        with pytest.raises(ValidationError):
            BatchRunConfig.model_validate(payload)
        assert payload == before

    @pytest.mark.parametrize("via", ["keyword", "model_validate"])
    def test_the_warning_is_attributed_to_the_caller_not_to_pydantic(self, tmp_path: Path, via: str) -> None:
        """A deprecation nobody sees is not a deprecation.

        A `mode="before"` validator sits under a VARIABLE number of pydantic frames, so a fixed
        `stacklevel` cannot point at the caller. With `stacklevel=2` the warning was attributed to
        `pydantic/main.py` — and Python's default filter only surfaces a `DeprecationWarning`
        raised from `__main__`, so an ordinary script saw NOTHING while `pytest.warns` still
        passed. 0.11.0 would then have been a hard break nobody was warned about.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            if via == "keyword":
                BatchRunConfig(run_dir=tmp_path, max_rows=5)
            else:
                BatchRunConfig.model_validate({"run_dir": str(tmp_path), "max_rows": 5})
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations, "no DeprecationWarning was emitted"
        for warning in deprecations:
            assert Path(warning.filename).name == Path(__file__).name, (
                f"attributed to {warning.filename}, not the caller — Python's default filter "
                "would hide it from an out-of-tree caller entirely"
            )

    def test_the_real_fingerprint_is_identical_either_way(self, tmp_path: Path) -> None:
        """The invariant named by `test_the_dump_is_identical_either_way`, through the real path.

        `compute_run_fingerprint` dumps with `mode="json"`, not the plain `model_dump()` the
        sibling test compares — so this calls the actual function rather than a spelling of it.
        A changed fingerprint invalidates every existing `--resume`.
        """
        from coder_eval.orchestration.batch import compute_run_fingerprint

        with pytest.warns(DeprecationWarning):
            aliased = BatchRunConfig(run_dir=tmp_path, max_rows=5, sample_per_stratum=3)
        explicit = BatchRunConfig(run_dir=tmp_path, row_selection=RowSelection(max_rows=5, sample_per_stratum=3))
        args = ("exp", "anthropic", None)
        assert compute_run_fingerprint(aliased, *args) == compute_run_fingerprint(explicit, *args)
