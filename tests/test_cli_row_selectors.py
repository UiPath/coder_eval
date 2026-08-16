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

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.cli.run_command import _run_with_experiment
from coder_eval.models import ROW_SELECTOR_FLAGS, RowSelection
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.task_loader import SplitSelectorError


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
