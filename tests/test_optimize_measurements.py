"""The optimize sidecar: a noise-floor cache and a regression corpus, beside the narrative ledger.

The split is the point. `.optimize-skill/<skill>/history.json` stays free-form prose; only the two
things that must be machine-read live in `measurements.json`, with a tight model. The first test
class is what stops a future refactor from quietly reversing that decision.
"""

from __future__ import annotations

import ast
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coder_eval.models import ArmRowScores, NoiseFloor, OptimizeMeasurements, RegressionRow, RoundScores
from coder_eval.optimize.activation import (
    measure_noise_floor,
    noise_floor_mde,
)
from coder_eval.optimize.gate import (
    GATE_RESAMPLES,
)
from coder_eval.optimize.search import (
    regression_check,
)
from coder_eval.optimize.store import (
    MEASUREMENTS_FILENAME,
    UNRECORDED_SPLIT,
    UNRESOLVED_MODEL,
    append_regression_rows,
    load_measurements,
    lookup_noise_floor,
    record_noise_floor,
    record_round_scores,
)
from tests.optimize_fixtures import SUITE, eval_result, module_source, write_row


REPO_ROOT = Path(__file__).parent.parent


def _floor(**overrides) -> NoiseFloor:
    base = {
        "suite_id": "my-skill-activation",
        "variant_id": "incumbent",
        "model": "claude-haiku-4-5-20251001",
        "criterion_index": 0,
        "n_rows": 12,
        "n_invocations": 3,
        "confidence": 0.95,
        "seed": 0,
        "n_resamples": 2000,
        # Set EXPLICITLY, as `measure_noise_floor` sets it, so the helper models a floor written
        # by current code. `lookup_noise_floor` skips an entry whose `split` was never set at all —
        # that is how a pre-upgrade cache file is told apart from a full-suite measurement.
        "split": None,
        "mde": 0.08,
        "computed_at": datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    }
    return NoiseFloor(**{**base, **overrides})


def _path(tmp_path: Path, skill: str = "my-skill") -> Path:
    return tmp_path / ".optimize-skill" / skill / MEASUREMENTS_FILENAME


class TestTheNarrativeLedgerIsLeftAlone:
    def test_existing_history_json_is_left_alone(self) -> None:
        """The real `.optimize-skill/ci/history.json` in this repo must stay unmodelled.

        Its value is exactly the parts no schema would admit — the superseded readings, the
        calibration notes, the record of why a four-way exact tie was total non-engagement rather
        than a ceiling. A model tight enough to validate it would have made it unwritable. This
        test fails the moment someone starts schematizing it.
        """
        history = REPO_ROOT / ".optimize-skill" / "ci" / "history.json"
        # `.optimize-skill/` is gitignored, so this skip fires in every clone. What is left behind
        # it is only the ledger's SHAPE, which needs the real file to assert anything about. The
        # load-bearing half — the whole-package scan proving no code path names history.json —
        # moved to `test_no_code_path_names_the_narrative_ledger`, which never skips.
        if not history.exists():  # noqa: CE045
            pytest.skip("the worked ci ledger is not present in this checkout")

        # It is a free-form ARRAY carrying keys no model declares — proof that validating it
        # would reject the real file.
        entries = json.loads(history.read_text(encoding="utf-8"))
        assert isinstance(entries, list) and entries
        narrative_keys = set(entries[0]) - set(OptimizeMeasurements.model_fields)
        assert narrative_keys, "the ledger no longer carries free-form keys — has it been schematized?"

        with pytest.raises(ValueError):
            OptimizeMeasurements.model_validate_json(history.read_text(encoding="utf-8"))

    def test_no_code_path_names_the_narrative_ledger(self) -> None:
        """No code path names `history.json`. Runs unconditionally — it needs no ledger present.

        It lived behind `test_existing_history_json_is_left_alone`'s skip until CE045 was written,
        which meant this — the half that guards a real architectural decision rather than the shape
        of one checked-in file — had never run in CI.

        Asserted over the whole package rather than one module, and with comments and docstrings
        stripped, so the guard cannot be satisfied by moving the parse somewhere else or evaded by
        a mention that is only prose.
        """
        offenders = []
        for module in sorted((REPO_ROOT / "src" / "coder_eval").rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                # `history.jsonl` is an unrelated shell-history file in the docker runner.
                if not re.search(r"history\.json(?!l)", node.value) or id(node) in docstrings:
                    continue
                offenders.append(f"{module.name}:{node.lineno}")
        assert not offenders, (
            f"{offenders} name history.json in code. The narrative ledger is neither parsed, "
            "validated nor rewritten by any code path — that is the whole decision behind "
            "splitting measurements.json out of it."
        )


class TestLoadMeasurements:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        loaded = load_measurements(_path(tmp_path))
        assert loaded == OptimizeMeasurements(skill="my-skill")

    def test_malformed_raises_with_the_path(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"skill": "my-skill", "noise_floors": [', encoding="utf-8")
        with pytest.raises(ValueError, match=str(path)):
            load_measurements(path)

    def test_rejects_a_mismatched_skill(self, tmp_path: Path) -> None:
        path = _path(tmp_path, skill="my-skill")
        path.parent.mkdir(parents=True)
        path.write_text(OptimizeMeasurements(skill="a-different-skill").model_dump_json(), encoding="utf-8")
        with pytest.raises(ValueError, match="copied rather than written here"):
            load_measurements(path)

    def test_unknown_keys_are_rejected(self, tmp_path: Path) -> None:
        # extra="forbid": every field here is machine-written, so a typo must not become a
        # permanent cache miss nobody notices.
        path = _path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"skill": "my-skill", "noise_flors": []}', encoding="utf-8")
        with pytest.raises(ValueError):
            load_measurements(path)

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        original = OptimizeMeasurements(
            skill="my-skill",
            noise_floors=[_floor()],
            regression_corpus=[RegressionRow(row_id="pos-3", promoted_in_round=1, reason="oblique phrasing")],
        )
        path.parent.mkdir(parents=True)
        path.write_text(original.model_dump_json(), encoding="utf-8")
        assert load_measurements(path) == original


class TestWritesAreAtomic:
    def test_a_rejected_write_leaves_the_existing_file_byte_identical(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(OptimizeMeasurements(skill="somebody-else").model_dump_json(), encoding="utf-8")
        before = path.read_bytes()

        with pytest.raises(ValueError):
            record_noise_floor(path, _floor())
        assert path.read_bytes() == before

    def test_a_failed_replace_leaves_no_temp_sibling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The os.replace path itself: if the rename fails, the temp file must not survive for the
        # next reader to trip over.
        path = _path(tmp_path)

        def _boom(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError, match="no space left"):
            record_noise_floor(path, _floor())

        assert not path.exists()
        assert list(path.parent.iterdir()) == []


class TestNoiseFloorCache:
    def test_lookup_matches_only_an_identical_measurement(self, tmp_path: Path) -> None:
        # Every key field, because each one demonstrably changes the number the cache would serve.
        measurements = record_noise_floor(_path(tmp_path), _floor())
        assert lookup_noise_floor(measurements, _floor()) is not None
        for differing in (
            {"suite_id": "another-suite"},
            {"variant_id": "1-incumbent"},
            {"model": "claude-sonnet-5"},
            # A floor is measured on two different metrics now, and on the same suite they are
            # different numbers — so `metric` has to key or one track is served the other's.
            {"metric": "weighted_score", "criterion_index": None},
            {"criterion_index": 1},
            {"n_rows": 16},
            {"n_invocations": 6},
            {"confidence": 0.9},
            {"seed": 7},
            {"n_resamples": 500},
        ):
            assert lookup_noise_floor(measurements, _floor(**differing)) is None, (
                f"a floor measured at {differing} was served for a different measurement"
            )

    def test_the_probe_ignores_the_measurement_itself(self, tmp_path: Path) -> None:
        # mde/computed_at are what you are looking UP, so they cannot be part of the key.
        measurements = record_noise_floor(_path(tmp_path), _floor(mde=0.08))
        found = lookup_noise_floor(measurements, _floor(mde=0.0, computed_at=datetime(2020, 1, 1, tzinfo=UTC)))
        assert found is not None and found.mde == 0.08

    def test_record_replaces_a_same_key_entry(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        record_noise_floor(path, _floor(mde=0.08))
        updated = record_noise_floor(path, _floor(mde=0.05))
        assert len(updated.noise_floors) == 1
        assert updated.noise_floors[0].mde == 0.05
        assert load_measurements(path) == updated

    def test_record_keeps_entries_with_a_different_key(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        record_noise_floor(path, _floor())
        updated = record_noise_floor(path, _floor(model="claude-sonnet-5"))
        assert len(updated.noise_floors) == 2

    def test_a_hand_edited_duplicate_resolves_to_the_newer_entry(self, tmp_path: Path) -> None:
        measurements = OptimizeMeasurements(skill="my-skill", noise_floors=[_floor(mde=0.9), _floor(mde=0.1)])
        found = lookup_noise_floor(measurements, _floor())
        assert found is not None and found.mde == 0.1

    def test_nested_records_reject_unknown_keys(self) -> None:
        # extra="forbid" does NOT propagate into nested models — and the corpus, which is the part
        # that is not reconstructible, lives in the nested ones.
        with pytest.raises(ValueError):
            OptimizeMeasurements.model_validate(
                {"skill": "s", "noise_floors": [{**_floor().model_dump(mode="json"), "typo_field": 1}]}
            )
        with pytest.raises(ValueError):
            OptimizeMeasurements.model_validate(
                {"skill": "s", "regression_corpus": [{"row_id": "r", "promoted_in_round": 1, "reason": "x", "b": 2}]}
            )

    def test_an_infinite_or_out_of_range_mde_is_rejected(self) -> None:
        for bad in (float("inf"), float("nan"), 1.5, -0.1):
            with pytest.raises(ValueError):
                _floor(mde=bad)


class TestRegressionCorpus:
    def test_append_deduplicates_on_row_id(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        append_regression_rows(path, [RegressionRow(row_id="pos-1", promoted_in_round=1, reason="first")])
        updated = append_regression_rows(
            path,
            [
                RegressionRow(row_id="pos-1", promoted_in_round=2, reason="re-promoted, must not duplicate"),
                RegressionRow(row_id="pos-2", promoted_in_round=2, reason="new"),
            ],
        )
        assert [r.row_id for r in updated.regression_corpus] == ["pos-1", "pos-2"]
        # Append-only: the original reason is never rewritten by a later promotion.
        assert updated.regression_corpus[0].reason == "first"

    def test_append_is_a_no_op_when_everything_is_known(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        rows = [RegressionRow(row_id="pos-1", promoted_in_round=1, reason="first")]
        append_regression_rows(path, rows)
        before = path.read_bytes()
        append_regression_rows(path, rows)
        assert path.read_bytes() == before

    def test_the_corpus_survives_the_sidecar_and_is_readable_by_the_check(self, tmp_path: Path) -> None:
        """Write, reload from disk, then READ — the loop the corpus existed without until now.

        The corpus is the one thing in this file that is not reconstructible, so the round trip is
        asserted against the real JSON rather than against the in-memory return value.
        """
        path = _path(tmp_path)
        append_regression_rows(
            path,
            [
                RegressionRow(row_id="pos-1", promoted_in_round=1, reason="oblique phrasing"),
                RegressionRow(row_id="pos-2", promoted_in_round=1, reason="symptom vocabulary"),
            ],
        )
        corpus = load_measurements(path).regression_corpus
        assert [r.reason for r in corpus] == ["oblique phrasing", "symptom vocabulary"]

        arm = ArmRowScores(variant_id="cand-a", row_scores={"pos-1": 1.0})
        found = regression_check(corpus, arm)
        # pos-2 is a HOLE on this arm, not a pass — it is what the round trip has to preserve.
        assert [(row.row_id, row.reason, score) for row, score in found] == [("pos-2", "symptom vocabulary", None)]


class TestNoiseFloorReuse:
    """The cache as `measure_noise_floor` actually uses it, against real run dirs on disk."""

    def _run_dirs(self, tmp_path: Path, *, invocations: int = 4, rows: int = 10) -> list[Path]:
        dirs = []
        for i in range(invocations):
            run_dir = tmp_path / f"run-{i}"
            for row in range(rows):
                observed = "yes" if (row + i) % 3 else "no"
                write_row(run_dir, "incumbent", f"r{row}", eval_result(f"r{row}", [("yes", observed)]))
            dirs.append(run_dir)
        return dirs

    def _measure(self, run_dirs: list[Path], **kwargs) -> NoiseFloor | None:
        return measure_noise_floor(
            run_dirs=run_dirs,
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            **{"model": "claude-haiku-4-5-20251001", **kwargs},
        )

    def _stored(self, mde: float, **overrides) -> OptimizeMeasurements:
        # `n_resamples` is DERIVED from the gate's own default rather than spelled: it is a key
        # field, so an entry written at any other count is a legitimate miss — which is exactly
        # what happened to every floor cached before GATE_RESAMPLES existed, and is the safe
        # direction (a miss recomputes from data already on disk).
        fields = {
            "suite_id": SUITE,
            "n_rows": 10,
            "n_invocations": 4,
            "n_resamples": GATE_RESAMPLES,
            "mde": mde,
            **overrides,
        }
        return OptimizeMeasurements(skill="my-skill", noise_floors=[_floor(**fields)])

    def test_measure_returns_the_whole_keyed_record(self, tmp_path: Path) -> None:
        measured = self._measure(self._run_dirs(tmp_path))
        assert measured is not None
        # n_rows is what the split actually scored, which is the number the cache keys on and
        # the one a caller cannot otherwise obtain.
        assert (measured.n_rows, measured.n_invocations, measured.confidence) == (10, 4, 0.95)
        assert (measured.suite_id, measured.variant_id, measured.criterion_index) == (SUITE, "incumbent", 0)
        assert 0.0 <= measured.mde <= 1.0

    def test_reused_when_every_key_field_matches(self, tmp_path: Path) -> None:
        run_dirs = self._run_dirs(tmp_path)
        reused = self._measure(run_dirs, measurements=self._stored(0.999))
        assert reused is not None and reused.mde == 0.999

    def test_recomputed_when_the_row_count_changed(self, tmp_path: Path) -> None:
        run_dirs = self._run_dirs(tmp_path)
        fresh = self._measure(run_dirs)
        assert fresh is not None
        recomputed = self._measure(run_dirs, measurements=self._stored(0.999, n_rows=99))
        assert recomputed is not None and recomputed.mde == fresh.mde

    def test_recomputed_when_the_invocation_count_changed(self, tmp_path: Path) -> None:
        """The finding this test exists for: the floor moves a LOT with the invocation count.

        Measured on one suite over the same 10 rows: 0.402 at two invocations, 0.168 at four.
        A key that omitted `n_invocations` would have served the two-invocation floor to a
        four-invocation round — and the floor decides whether the round runs at all.
        """
        two = self._measure(self._run_dirs(tmp_path / "a", invocations=2))
        four = self._measure(self._run_dirs(tmp_path / "b", invocations=4))
        assert two is not None and four is not None
        assert two.mde != four.mde, "the fixture no longer distinguishes invocation counts"

        # The 2-invocation floor is on disk; a 4-invocation round must not be handed it.
        stored = OptimizeMeasurements(skill="my-skill", noise_floors=[two])
        recomputed = measure_noise_floor(
            run_dirs=self._run_dirs(tmp_path / "b", invocations=4),
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            model="claude-haiku-4-5-20251001",
            measurements=stored,
        )
        assert recomputed is not None and recomputed.mde == four.mde

    def test_recomputed_when_the_variant_was_renamed(self, tmp_path: Path) -> None:
        # The likeliest trip in practice: the incumbent arm is renamed round to round while
        # suite, model and row count stay put.
        run_dirs = self._run_dirs(tmp_path)
        fresh = self._measure(run_dirs)
        assert fresh is not None
        recomputed = self._measure(run_dirs, measurements=self._stored(0.999, variant_id="1-incumbent"))
        assert recomputed is not None and recomputed.mde == fresh.mde

    def test_an_unresolvable_model_never_hits_the_cache(self, tmp_path: Path) -> None:
        # noise_floor_mde passes a placeholder when the caller resolved no model; a floor
        # measured under another model must not be borrowed.
        run_dirs = self._run_dirs(tmp_path)
        fresh = noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0)
        borrowed = noise_floor_mde(
            run_dirs=run_dirs,
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            measurements=self._stored(0.999),
            model=None,
        )
        assert borrowed == fresh != 0.999

    def test_noise_floor_mde_agrees_with_measure_noise_floor(self, tmp_path: Path) -> None:
        run_dirs = self._run_dirs(tmp_path)
        measured = self._measure(run_dirs)
        assert measured is not None
        assert (
            noise_floor_mde(
                run_dirs=run_dirs,
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5-20251001",
            )
            == measured.mde
        )


class TestRoundScores:
    def test_round_scores_carry_arm_row_scores_through_json(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        original = OptimizeMeasurements(
            skill="my-skill",
            round_scores=[
                RoundScores(
                    round=1,
                    arm_row_scores=[
                        ArmRowScores(variant_id="incumbent", row_scores={"r1": 0.5, "r2": 1.0}),
                        ArmRowScores(variant_id="cand-a", row_scores={"r1": 1.0, "r2": 0.0}),
                    ],
                    pareto_front=["incumbent", "cand-a"],
                )
            ],
        )
        path.parent.mkdir(parents=True)
        path.write_text(original.model_dump_json(), encoding="utf-8")

        loaded = load_measurements(path)
        assert loaded == original
        # The vectors are what a later round looks back at, so they must survive whole.
        assert loaded.round_scores[0].arm_row_scores[1].row_scores == {"r1": 1.0, "r2": 0.0}

    def test_round_scores_reject_unknown_keys(self) -> None:
        with pytest.raises(ValueError):
            OptimizeMeasurements.model_validate(
                {"skill": "s", "round_scores": [{"round": 1, "arm_row_scores": [], "pareto": []}]}
            )


class TestRecordRoundScores:
    def _scores(self, rnd: int, mde_marker: float) -> RoundScores:
        return RoundScores(
            round=rnd,
            arm_row_scores=[ArmRowScores(variant_id="cand-a", row_scores={"r1": mde_marker})],
            pareto_front=["cand-a"],
        )

    def test_replaces_the_same_round_rather_than_appending(self, tmp_path: Path) -> None:
        # Replace, unlike the regression corpus beside it: re-running a round supersedes its
        # measurement rather than leaving two contradictory records of the same round.
        path = _path(tmp_path)
        record_round_scores(path, self._scores(1, 0.1))
        updated = record_round_scores(path, self._scores(1, 0.9))
        assert len(updated.round_scores) == 1
        assert updated.round_scores[0].arm_row_scores[0].row_scores == {"r1": 0.9}
        assert load_measurements(path) == updated

    def test_keeps_other_rounds(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        record_round_scores(path, self._scores(1, 0.1))
        updated = record_round_scores(path, self._scores(2, 0.2))
        assert [r.round for r in updated.round_scores] == [1, 2]

    def test_coexists_with_the_other_two_writers(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        record_noise_floor(path, _floor())
        append_regression_rows(path, [RegressionRow(row_id="pos-1", promoted_in_round=1, reason="x")])
        updated = record_round_scores(path, self._scores(1, 0.5))
        assert len(updated.noise_floors) == 1
        assert len(updated.regression_corpus) == 1
        assert len(updated.round_scores) == 1


class TestAnUnresolvedModelIsNeverCached:
    def test_record_noise_floor_refuses_the_placeholder(self, tmp_path: Path) -> None:
        # It could never match its own lookup, so writing it only accumulates dead entries.
        path = _path(tmp_path)
        updated = record_noise_floor(path, _floor(model=UNRESOLVED_MODEL))
        assert updated.noise_floors == []
        assert not path.exists(), "nothing should have been written at all"

    def test_a_real_model_still_records(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        assert len(record_noise_floor(path, _floor()).noise_floors) == 1


class TestTheTwoTracksCoexist:
    """`metric` joins the cache key because a floor is now measured on two different quantities."""

    def _activation(self, **overrides) -> NoiseFloor:
        return _floor(**overrides)

    def _execution(self, **overrides) -> NoiseFloor:
        # Same suite, variant, model, row count and invocation count — everything the key held
        # BEFORE `metric` existed. Only the metric (and its criterion_index) differ.
        return _floor(metric="weighted_score", criterion_index=None, **overrides)

    def test_the_two_tracks_floors_do_not_collide_in_the_cache(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        record_noise_floor(path, self._activation(mde=0.11))
        measurements = record_noise_floor(path, self._execution(mde=0.44))

        assert len(measurements.noise_floors) == 2, "the execution floor REPLACED the activation one"
        activation = lookup_noise_floor(measurements, self._activation(mde=0.0))
        execution = lookup_noise_floor(measurements, self._execution(mde=0.0))
        assert activation is not None and activation.mde == 0.11
        assert execution is not None and execution.mde == 0.44

    def test_metric_is_part_of_the_key(self) -> None:
        measurements = OptimizeMeasurements(skill="my-skill", noise_floors=[self._activation()])
        assert lookup_noise_floor(measurements, self._execution()) is None

    def test_a_legacy_floor_still_loads_but_no_longer_matches_once_split_joined_the_key(self, tmp_path: Path) -> None:
        """An existing `measurements.json` must still LOAD; whether it still MATCHES depends.

        Loading is the non-negotiable half: `load_measurements` deliberately RAISES on a malformed
        file rather than rebuilding it, so a non-defaulted field would make every pre-existing
        sidecar unreadable — not a cache miss, a hard failure on a file carrying a regression
        corpus that is not reconstructible.

        Matching is where `split` deliberately DIFFERS from `metric`, and the difference is about
        whether the default is TRUE of legacy data. When `metric` was added, activation was the
        only track, so every pre-existing floor really was `f1.yes` and the default described it
        correctly. `split=None` means "no --split was passed", which a legacy floor measured under
        `--split train` would assert falsely — and being handed that floor is exactly the
        train-against-test failure this field exists to prevent, surviving inside the sidecar. So a
        floor whose `split` key was never set is skipped, and the round recomputes. That costs one
        bootstrap over data already on disk, which is the trade this module's own docstring makes.
        """
        path = _path(tmp_path)
        path.parent.mkdir(parents=True)
        legacy = {
            "skill": "my-skill",
            "noise_floors": [
                {
                    "suite_id": "my-skill-activation",
                    "variant_id": "incumbent",
                    "model": "claude-haiku-4-5-20251001",
                    "criterion_index": 0,
                    "n_rows": 12,
                    "n_invocations": 3,
                    "confidence": 0.95,
                    "seed": 0,
                    "n_resamples": 2000,
                    "mde": 0.08,
                    "computed_at": "2026-08-13T12:00:00Z",
                }
            ],
        }
        path.write_text(json.dumps(legacy), encoding="utf-8")

        loaded = load_measurements(path)
        assert loaded.noise_floors[0].metric == "f1.yes", "a legacy entry IS an activation floor"
        # metric: the default describes legacy data correctly, so it is still a match candidate.
        assert "metric" not in loaded.noise_floors[0].model_fields_set
        # split: the default does NOT, so the entry is skipped and the caller recomputes.
        assert "split" not in loaded.noise_floors[0].model_fields_set
        assert lookup_noise_floor(loaded, _floor(mde=0.0)) is None

    def test_a_floor_written_by_current_code_still_matches(self, tmp_path: Path) -> None:
        """The other side of the skip: a floor whose split WAS set matches normally."""
        path = _path(tmp_path)
        record_noise_floor(path, _floor(mde=0.08))
        found = lookup_noise_floor(load_measurements(path), _floor(mde=0.0))
        assert found is not None and found.mde == 0.08

    def test_criterion_index_none_is_legal_and_negative_is_not(self) -> None:
        assert _floor(criterion_index=None).criterion_index is None
        with pytest.raises(ValueError):
            _floor(criterion_index=-1)


class TestTargetLabelMoved:
    def test_target_label_is_importable_from_models(self) -> None:
        # A module-level constant that is not in `__all__` is a private import in disguise, so the
        # move is only complete when both halves are done.
        import coder_eval.models as models
        from coder_eval.models import TARGET_LABEL

        assert TARGET_LABEL == "yes"
        assert "TARGET_LABEL" in models.__all__

    @pytest.mark.parametrize(
        "module",
        ["optimize.load", "optimize.gate", "optimize.activation", "optimize.execution"],
    )
    def test_the_family_imports_it_rather_than_redeclaring_it(self, module: str) -> None:
        """Every module in the family that reads the label must import it, not respell it.

        Parametrized across the split rather than pinned to one module: the decision layer is six
        modules now, and a re-declaration in any of them is the same defect — two equal string
        constants that agree today and diverge silently the moment either moves, with a gate then
        reading an F1 for a class the criterion never emits.
        """
        import importlib

        from coder_eval.models import TARGET_LABEL

        imported = importlib.import_module(f"coder_eval.{module}")
        source = module_source(module)
        if not hasattr(imported, "TARGET_LABEL"):
            # A module that does not read the label at all is fine; it just must not declare one.
            assert "TARGET_LABEL = " not in source, f"{module} declares TARGET_LABEL without using it"
            return
        assert imported.TARGET_LABEL is TARGET_LABEL
        assert "TARGET_LABEL = " not in source, f"{module} re-declares TARGET_LABEL — it must import it"

    def test_the_criterion_that_produces_the_label_imports_it_too(self) -> None:
        """The gate reads `f1.yes`; `skill_triggered` is what emits that `yes`.

        Two equal string constants is precisely the state being removed: they agree today and
        would diverge silently the moment either moved, with the gate reading an F1 for a class
        the criterion had stopped emitting. Identity, not equality — equality is what two
        independent literals already satisfy.

        The dependency points criterion -> models rather than the other way because
        `models/optimize.py` is a cycle-free leaf the gate imports, and `models` cannot import
        `criteria`.
        """
        from coder_eval.criteria import skill_triggered
        from coder_eval.models import TARGET_LABEL

        assert skill_triggered._YES is TARGET_LABEL
        source = (REPO_ROOT / "src" / "coder_eval" / "criteria" / "skill_triggered.py").read_text(encoding="utf-8")
        assert '_YES = "yes"' not in source, "skill_triggered re-declares the label — it must import it"

    def test_the_noise_floor_default_is_derived_from_it(self) -> None:
        # The model cannot import the gate (that is a cycle), so the literal needs a guard.
        from coder_eval.models import TARGET_LABEL

        assert NoiseFloor.model_fields["metric"].default == f"f1.{TARGET_LABEL}"


class TestInstanceBestFrontIsPersisted:
    """The merge shortlist is what a LATER round is built from, so it has to survive to disk."""

    def test_round_scores_round_trips_the_instance_best_front(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        original = OptimizeMeasurements(
            skill="my-skill",
            round_scores=[
                RoundScores(
                    round=1,
                    arm_row_scores=[ArmRowScores(variant_id="cand-a", row_scores={"r1": 1.0, "r2": 0.3})],
                    pareto_front=["incumbent", "cand-a"],
                    instance_best_front=["cand-a", "cand-b"],
                )
            ],
        )
        path.parent.mkdir(parents=True)
        path.write_text(original.model_dump_json(), encoding="utf-8")

        loaded = load_measurements(path)
        assert loaded == original
        # The two fronts are stored SEPARATELY — the whole point is that they differ.
        assert loaded.round_scores[0].pareto_front != loaded.round_scores[0].instance_best_front

    def test_round_scores_written_before_the_field_still_load(self, tmp_path: Path) -> None:
        # RoundScores is extra="forbid", so the default is what keeps an existing sidecar readable
        # through a load_measurements that RAISES rather than rebuilding.
        path = _path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "skill": "my-skill",
                    "round_scores": [
                        {
                            "round": 1,
                            "arm_row_scores": [{"variant_id": "cand-a", "row_scores": {"r1": 0.5}}],
                            "pareto_front": ["cand-a"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert load_measurements(path).round_scores[0].instance_best_front == []


class TestLineageHeadIsPersisted:
    """The search loop's carry-forward pointer — and everything it deliberately is NOT.

    It names the arm rounds 2+ work from. It is not a promotion, and it stores no score: the
    number to beat is derived from that arm's `row_scores`, so the two cannot disagree.
    """

    def _round(self, **overrides) -> RoundScores:
        base = {
            "round": 1,
            "arm_row_scores": [ArmRowScores(variant_id="cand-a", row_scores={"r1": 1.0, "r2": 0.5})],
            "pareto_front": ["cand-a"],
        }
        return RoundScores(**{**base, **overrides})

    def test_defaults_to_none(self) -> None:
        assert self._round().lineage_head is None

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        original = OptimizeMeasurements(skill="my-skill", round_scores=[self._round(lineage_head="cand-a")])
        path.parent.mkdir(parents=True)
        path.write_text(original.model_dump_json(), encoding="utf-8")

        loaded = load_measurements(path)
        assert loaded == original
        assert loaded.round_scores[0].lineage_head == "cand-a"

    def test_a_sidecar_written_before_the_field_loads_with_none(self, tmp_path: Path) -> None:
        # `extra="forbid"` rejects UNKNOWN keys, not ABSENT optional ones — and the opposite belief
        # is common enough to be worth pinning, since `load_measurements` RAISES on a malformed
        # file rather than rebuilding one that carries an unreconstructible regression corpus.
        path = _path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "skill": "my-skill",
                    "round_scores": [
                        {
                            "round": 1,
                            "arm_row_scores": [{"variant_id": "cand-a", "row_scores": {"r1": 0.5}}],
                            "pareto_front": ["cand-a"],
                            "instance_best_front": ["cand-a"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert load_measurements(path).round_scores[0].lineage_head is None

    def test_record_round_scores_preserves_it_and_replaces_the_round(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        record_round_scores(path, self._round(lineage_head="cand-a"))
        # Preserved through the writer, not only through model_dump_json: a writer that dropped a
        # non-None head would still pass a test whose only assertion is the replacement below.
        assert load_measurements(path).round_scores[0].lineage_head == "cand-a"

        updated = record_round_scores(path, self._round(lineage_head=None))
        assert len(updated.round_scores) == 1, "re-recording a round must replace it, not accumulate"
        assert updated.round_scores[0].lineage_head is None
        assert load_measurements(path) == updated

    def test_a_head_naming_an_absent_arm_is_rejected(self) -> None:
        # Otherwise the next round's `next(a for a in ... if a.variant_id == head)` raises
        # StopIteration in the user's terminal, from a sidecar written a round earlier.
        with pytest.raises(ValueError, match="not one of this round's arms"):
            self._round(lineage_head="cand-b")

    def test_a_head_with_no_row_scores_is_rejected(self) -> None:
        # And this one would be a ZeroDivisionError on the mean, one round later.
        with pytest.raises(ValueError, match="scored no rows"):
            self._round(
                arm_row_scores=[ArmRowScores(variant_id="cand-a", row_scores={})],
                lineage_head="cand-a",
            )

    def test_it_stores_no_score(self) -> None:
        # The number to beat is DERIVED from the head's row_scores. A second declaration is a
        # second thing that can disagree — the drift CE062/CE040 and `_floor_key` exist to prevent.
        assert "lineage_score" not in RoundScores.model_fields
        assert RoundScores.model_fields["lineage_head"].annotation == (str | None)


class TestSplitIsPartOfTheCacheKey:
    """A train floor must never be served to a test lookup.

    On the shipped `outcome.yaml` template `split` is the ONLY key field that differs between the
    two measurements — same suite, same variant, same model, same row count — so without it in the
    key one split's floor answers the other's lookup, on the number that decides whether a round
    runs at all.
    """

    def test_two_floors_differing_only_in_split_do_not_match_each_other(self) -> None:
        train = _floor(split="train", mde=0.08)
        test = _floor(split="test", mde=0.31)
        measurements = OptimizeMeasurements(skill="my-skill", noise_floors=[train, test])

        assert lookup_noise_floor(measurements, _floor(split="train")) is train
        assert lookup_noise_floor(measurements, _floor(split="test")) is test

    def test_a_null_split_is_its_own_key_not_a_wildcard(self) -> None:
        """A full-suite floor answers a full-suite lookup and nothing else."""
        measurements = OptimizeMeasurements(skill="my-skill", noise_floors=[_floor(split=None, mde=0.08)])
        assert lookup_noise_floor(measurements, _floor(split=None)) is not None
        assert lookup_noise_floor(measurements, _floor(split="train")) is None

    def test_split_is_in_the_derived_key_not_a_hand_written_list(self) -> None:
        """`_floor_key` reads `NoiseFloor.model_fields`, which is what makes adding a key field
        a one-line change that cannot be forgotten here."""
        from coder_eval.optimize.store import _FLOOR_MEASUREMENT_FIELDS

        key_fields = [n for n in NoiseFloor.model_fields if n not in _FLOOR_MEASUREMENT_FIELDS]
        assert "split" in key_fields
        # And declared above `mde`, so the file keeps reading the way its docstring says.
        names = list(NoiseFloor.model_fields)
        assert names.index("split") < names.index("mde")

    def test_two_floors_differing_only_in_split_both_survive_a_write(self, tmp_path: Path) -> None:
        """Round-tripped through the REAL cache: replacement is keyed, so these must not collide."""
        path = _path(tmp_path)
        record_noise_floor(path, _floor(split="train", mde=0.08))
        measurements = record_noise_floor(path, _floor(split="test", mde=0.31))
        assert len(measurements.noise_floors) == 2, "the test floor REPLACED the train one"


class TestUnrecordedSplitIsNeverCached:
    """The sentinel's whole contract, mirroring UNRESOLVED_MODEL's."""

    def test_record_refuses_and_says_which_field_made_it_uncacheable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _path(tmp_path)
        with caplog.at_level("INFO"):
            measurements = record_noise_floor(path, _floor(split=UNRECORDED_SPLIT))
        assert measurements.noise_floors == []
        assert not path.exists(), "an uncacheable floor must not even create the sidecar"
        assert "split" in caplog.text and UNRECORDED_SPLIT in caplog.text

    def test_the_unresolved_model_refusal_still_works_and_names_its_own_field(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression guard: the two refusals now share one branch."""
        with caplog.at_level("INFO"):
            measurements = record_noise_floor(_path(tmp_path), _floor(model=UNRESOLVED_MODEL))
        assert measurements.noise_floors == []
        assert "model" in caplog.text and UNRESOLVED_MODEL in caplog.text

    def test_it_can_never_collide_with_a_real_split_name(self) -> None:
        # Parenthesised exactly like UNRESOLVED_MODEL, and a --split value comes from a
        # dataset's split_field, which task authors spell as plain identifiers.
        assert UNRECORDED_SPLIT.startswith("(") and UNRECORDED_SPLIT.endswith(")")
        assert UNRECORDED_SPLIT != UNRESOLVED_MODEL
