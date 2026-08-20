"""Unit tests for `coder_eval.optimize.load` — the family's rank-0 reader.

Loading, pairing, run-tree provenance, reconciliation, the row primitives and rule attribution.
It decides nothing, so nothing here asserts a promotion.
"""

import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.models import (
    ActivationGateVerdict,
    CriterionResult,
    EvaluationResult,
    RoundScores,
    copy_with,
)
from coder_eval.optimize.activation import activation_gate, holm_promote, measure_noise_floor, noise_floor_mde
from coder_eval.optimize.fronts import arm_row_scores, cost_quality_points, headroom_ceiling
from coder_eval.optimize.gate import cost_latency_guardrails
from coder_eval.optimize.load import (
    RuleAttribution,
    SplitProvenance,
    _balance_clusters,
    _no_results_note,
    _pair_rows,
    _rules_verdicts,
    _wrong_path_notes,
    balance_pair,
    label_pairs,
    load_and_pair,
    load_arm_rows,
    load_suite_rows,
    read_split_provenance,
    row_cost_levels,
    row_costs,
    row_replicate_scores,
    rule_row_map,
)
from coder_eval.optimize.store import UNRECORDED_SPLIT, grader_changed
from coder_eval.reports_optimize import render_row_replicates
from coder_eval.reports_stats import median_or_none
from tests.optimize_fixtures import (
    FAST_RESAMPLES,
    SUITE,
    WINNER,
    activation_verdict,
    cost_check,
    cost_quality_arm,
    cost_rows,
    eval_result,
    exec_gate,
    exec_run_dir,
    grader_result,
    scored_result,
    set_split,
    shared_dirs,
    write_arm,
    write_row,
)


class TestLoadSuiteRows:
    def test_reads_all_replicate_dirs(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]}, invocations=3)
        assert [len(load_suite_rows(d, "incumbent", SUITE)["r1"]) for d in run_dirs] == [1, 1, 1]
        # Pooled across the three invocations, the row carries three replicates.
        assert len(load_arm_rows(run_dirs, "incumbent", SUITE)["r1"]) == 3

    def test_skips_malformed_task_json(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dir = tmp_path / "run-0"
        write_row(run_dir, "incumbent", "good", eval_result("good", [("yes", "yes")]))
        bad = write_row(run_dir, "incumbent", "bad", eval_result("bad", [("yes", "yes")]))
        bad.write_text('{"task_id": "truncated"', encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            rows = load_suite_rows(run_dir, "incumbent", SUITE)
        assert set(rows) == {"good"}
        assert "Failed to load" in caplog.text

    def test_missing_variant_dir_returns_empty(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-0"
        write_row(run_dir, "incumbent", "r1", eval_result("r1", [("yes", "yes")]))
        assert load_suite_rows(run_dir, "typo-variant", SUITE) == {}
        assert load_suite_rows(run_dir, "incumbent", "typo-suite") == {}

    def test_the_loader_reads_a_differently_padded_replicate_dir(self, tmp_path: Path) -> None:
        """The day `replicate_subdir_name` widens to NNN, this loader must still find the rows.

        The pre-CE042 glob pinned `[0-9][0-9]`, so it would have matched NOTHING — both gates load
        zero rows and the zero-row note blames a wrong variant id, a wrong suite id or a wrong run
        directory, which are the three things that would be correct.
        """
        run_dir = tmp_path / "run-0"
        task_dir = run_dir / "incumbent" / SUITE / "r1" / "000"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(eval_result("r1", [("yes", "yes")]).model_dump_json(), encoding="utf-8")
        assert list(load_suite_rows(run_dir, "incumbent", SUITE)) == ["r1"]


class TestBalancePair:
    """The per-row replicate trim, which was spelled three times in three shapes."""

    def test_equal_lengths_pass_through(self) -> None:
        assert balance_pair([1.0, 2.0], [3.0, 4.0]) == ([1.0, 2.0], [3.0, 4.0])

    def test_a_longer_incumbent_trims_to_the_candidate(self) -> None:
        assert balance_pair([1.0, 2.0, 3.0], [4.0]) == ([1.0], [4.0])

    def test_a_longer_candidate_trims_to_the_incumbent(self) -> None:
        assert balance_pair([1.0], [4.0, 5.0, 6.0]) == ([1.0], [4.0])

    def test_an_empty_side_yields_two_empty_lists(self) -> None:
        # The row is then dropped by whichever caller's own emptiness rule applies — a different
        # question from balancing, and deliberately not this function's to answer.
        assert balance_pair([], [1.0, 2.0]) == ([], [])
        assert balance_pair([1.0, 2.0], []) == ([], [])

    def test_it_is_generic_over_the_element_type(self) -> None:
        # Both real element types: the guardrail trims floats, the F1 and sibling paths trim
        # label pairs. A signature that only served one would have left two of the three sites.
        pairs = [("yes", "yes"), ("no", "no"), ("yes", "no")]
        assert balance_pair(pairs, pairs[:1]) == ([("yes", "yes")], [("yes", "yes")])
        assert balance_pair([0.1, 0.2, 0.3], [0.4, 0.5]) == ([0.1, 0.2], [0.4, 0.5])


class TestLabelPairs:
    def test_selects_by_position(self, tmp_path: Path) -> None:
        # Two stacked skill_triggered criteria whose descriptions BOTH interpolate the row id,
        # mirroring the shipped templates. A description-keyed implementation fails this.
        results = [eval_result("r1", [("yes", "yes"), ("no", "yes")])]
        assert label_pairs(results, 0) == [("yes", "yes")]
        assert label_pairs(results, 1) == [("no", "yes")]

    def test_skips_rows_with_too_few_results(self) -> None:
        results = [eval_result("r1", [("yes", "yes")])]
        assert label_pairs(results, 5) == []

    def test_skips_non_classification_results(self) -> None:
        results = [eval_result("r1", [("yes", "yes")], extra_basic=True)]
        assert label_pairs(results, 1) == []


class TestCriterionIndexIsBoundedBelow:
    """The lower bound. The internal guards bound only ABOVE (``criterion_index >= len(...)``),
    which is right for the overflow case — rows legitimately differ in criteria count, so an
    over-long index degrades to "skip the row" — and blind to a negative one. Python's positional
    indexing then silently grades ``success_criteria_results[-1]``: the LAST criterion on every
    row, reported as a confident number for the criterion the caller named. The skill drives all of
    this from an inline ``python`` snippet, so a wrong index is an authoring error that has to be
    loud rather than coerced into a different measurement.
    """

    def test_activation_gate_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            activation_gate(
                incumbent_run_dirs=run_dirs,
                candidate_run_dirs=run_dirs,
                incumbent_variant="incumbent",
                candidate_variant="candidate",
                suite_id=SUITE,
                criterion_index=-1,
                n_resamples=FAST_RESAMPLES,
            )

    def test_execution_gate_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -2"):
            exec_gate(run_dir, engagement_criterion_index=-2)

    def test_arm_row_scores_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=-1)

    def test_cost_quality_points_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            cost_quality_points(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=-1)

    def test_noise_floor_mde_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=-1)

    def test_measure_noise_floor_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            measure_noise_floor(
                run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=-1, model="m"
            )

    def test_none_stays_legal_on_the_index_optional_entry_points(self, tmp_path: Path) -> None:
        # `None` is the documented "use the row's weighted_score" sentinel, not a missing value.
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        assert arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE) != []
        assert cost_quality_points(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE) is not None

    def test_an_over_long_index_still_degrades_rather_than_raising(self, tmp_path: Path) -> None:
        # The anti-over-fix pin: only the LOWER bound became an error. Rows legitimately differ in
        # criteria count, so an index past the end must keep skipping the row.
        run_dirs = write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        scores = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=9)
        assert scores[0].row_scores == {}

    def test_minus_one_used_to_grade_the_last_criterion_of_each_row(self, tmp_path: Path) -> None:
        """The defect witnessed, not merely asserted — on rows whose criteria lists DIFFER.

        Every other test here uses single-criterion rows, where `-1` and `0` select the same
        result and the bug is invisible. Here row `r1` carries two criteria and `r2` one, so
        `success_criteria_results[-1]` is a DIFFERENT criterion on the two rows — and on `r1` it is
        a `file_check`, not the `skill_triggered` the caller named. `label_pairs` keeps only
        `ClassificationCriterionResult`s, so before the guard this returned a confident F1
        computed over a silently different, silently smaller set of rows.
        """
        run_dir = tmp_path / "run-0"
        write_row(run_dir, "incumbent", "r1", eval_result("r1", [("yes", "no")], extra_basic=True))
        write_row(run_dir, "incumbent", "r2", eval_result("r2", [("yes", "yes")]))

        rows = load_suite_rows(run_dir, "incumbent", SUITE)
        # What `-1` would have selected: `file_check` on r1 (dropped by label_pairs, so the row
        # vanishes from the sample) and the row's only classification result on r2.
        assert [type(rows[rid][0].success_criteria_results[-1]).__name__ for rid in ("r1", "r2")] == [
            "CriterionResult",
            "ClassificationCriterionResult",
        ]
        # Index 0 — what the caller asked for — is a classification result on BOTH rows.
        assert all(len(label_pairs(rows[rid], 0)) == 1 for rid in ("r1", "r2"))
        # And the boundary now refuses to answer the question at all.
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            arm_row_scores(run_dirs=[run_dir], variant_ids=["incumbent"], suite_id=SUITE, criterion_index=-1)

    def test_the_persisted_verdict_cannot_carry_a_negative_index(self) -> None:
        # The mechanical half, on the model rather than at the boundary: a recorded verdict can
        # never claim a negative position even if some future caller bypassed the guard.
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            ActivationGateVerdict(
                incumbent_variant="i",
                candidate_variant="c",
                suite_id=SUITE,
                criterion_index=-1,
                confidence=0.95,
                n_resamples=10,
                rows_paired=0,
                rows_excluded=0,
                mean_diff=None,
                ci_low=None,
                ci_high=None,
                p_value=None,
                p_floor=None,
                n_discordant=None,
            )


class TestUnbalancedReplicates:
    """Two arms that did the SAME thing on every row must not separate.

    A row's weight in an arm's pooled f1.yes is its observation count, so an arm that contributed
    three replicates for a row while the other contributed two has silently reweighted the
    comparison. The trigger is mundane — Stage B is three separate invocations, and one interrupted
    run leaves a partial row set. Measured before the fix: byte-identical labels on all 20 rows,
    f1.yes 0.818 vs 0.750, interval excluding zero, p = 0.022, rows_excluded 0, no note.
    """

    def _rows(self) -> dict[str, list[tuple[str, str]]]:
        # A mix the arms agree on exactly: 12 engage, 8 do not.
        return {f"r{i}": [("yes", "yes" if i % 5 else "no")] for i in range(20)}

    def _dirs(self, tmp_path: Path, *, truncate_after: int) -> list[Path]:
        rows = self._rows()
        run_dirs = []
        for i in range(3):
            run_dir = tmp_path / f"run-{i}"
            for n, (row_id, labels) in enumerate(sorted(rows.items())):
                # The incumbent's third invocation stopped part-way through.
                if not (i == 2 and n >= truncate_after):
                    write_row(run_dir, "incumbent", row_id, eval_result(row_id, labels))
                write_row(run_dir, "candidate", row_id, eval_result(row_id, labels))
            run_dirs.append(run_dir)
        return run_dirs

    def test_identical_arms_do_not_separate_when_one_run_was_interrupted(self, tmp_path: Path) -> None:
        verdict = activation_verdict(self._dirs(tmp_path, truncate_after=12))
        assert verdict.incumbent_f1 == verdict.candidate_f1
        assert verdict.mean_diff == 0.0
        assert verdict.ci_low == verdict.ci_high == 0.0
        assert holm_promote([verdict])[0].promoted is False

    def test_the_trim_is_named_so_the_run_is_re_run_not_read(self, tmp_path: Path) -> None:
        verdict = activation_verdict(self._dirs(tmp_path, truncate_after=12))
        note = " ".join(verdict.notes)
        assert "different replicate counts" in note
        assert "trimmed to the smaller count" in note
        assert "Re-run it" in note

    def test_balanced_arms_are_untouched(self, tmp_path: Path) -> None:
        verdict = activation_verdict(self._dirs(tmp_path, truncate_after=20))
        assert not any("replicate counts" in n for n in verdict.notes)
        assert verdict.rows_paired == 20


class TestOneRowCostDefinition:
    def test_cost_quality_points_agree_with_the_guardrail_about_a_row_cost(self, tmp_path: Path) -> None:
        """Both surfaces print the same number, because both route through row_cost_levels.

        This is the test that stops a second definition of "what a row cost" from appearing — the
        CE037-class defect this repo already added a lint rule for in the F1 direction.
        """
        per_row = {f"r{i}": (0.8, 0.5 + 0.1 * i) for i in range(8)}
        cost_quality_arm(tmp_path, "incumbent", per_row)
        cost_quality_arm(tmp_path, "candidate", per_row)
        run_dir = tmp_path / "run-0"

        points = cost_quality_points(
            run_dirs=[run_dir], variant_ids=["incumbent", "candidate"], suite_id=SUITE, criterion_index=None
        )
        check = cost_check(
            cost_latency_guardrails(
                incumbent_rows=load_arm_rows([run_dir], "incumbent", SUITE),
                candidate_rows=load_arm_rows([run_dir], "candidate", SUITE),
                n_resamples=200,
            )
        )
        incumbent = next(p for p in points if p.variant_id == "incumbent")
        assert incumbent.cost_per_row == pytest.approx(check.incumbent)

    def test_row_cost_levels_is_the_only_row_cost_reduction(self) -> None:
        # Called directly and compared against the guardrail's reported level on a fixture with
        # UNEVEN replicate counts, so the shared reduction is exercised rather than assumed.
        rows = cost_rows({"r0": [1.0, 3.0], "r1": [2.0], "r2": [4.0, 4.0, 4.0]})
        levels = row_cost_levels([row_costs(rows[rid]) for rid in sorted(rows)])
        assert levels == [2.0, 2.0, 4.0]

        check = cost_check(cost_latency_guardrails(incumbent_rows=rows, candidate_rows=rows, n_resamples=200))
        assert check.incumbent == pytest.approx(median_or_none(levels))

    def test_an_empty_cluster_is_absent_not_zero(self) -> None:
        # `mean([])` is 0.0, so an unfiltered empty cluster would read as "this row cost nothing".
        assert row_cost_levels([[1.0], [], [3.0]]) == [1.0, 3.0]


class TestGraderFingerprint:
    """The instrument, recorded per round — because a grader fix moves every score at once.

    Measured: a mid-round fix moved a suite mean 0.8679 -> 0.9158 on IDENTICAL artifacts, and
    nothing in any run directory recorded that the instrument had moved.
    """

    GRADER = (
        Path(__file__).parent.parent
        / "plugins"
        / "coder-eval"
        / "reference"
        / "templates"
        / "outcome-grader"
        / "verify.py"
    )

    def _copy(self, tmp_path: Path) -> Path:
        target = tmp_path / "outcome-grader"
        shutil.copytree(self.GRADER.parent, target, ignore=shutil.ignore_patterns("__pycache__"))
        return target / "verify.py"

    @staticmethod
    def _fingerprint(script: Path) -> str:
        completed = subprocess.run(
            [sys.executable, str(script), "--fingerprint"], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        lines = completed.stdout.split()
        assert len(lines) == 1, f"--fingerprint printed more than the hash: {completed.stdout!r}"
        return lines[0]

    def test_grader_fingerprint_is_stable_across_invocations(self, tmp_path: Path) -> None:
        script = self._copy(tmp_path)
        assert self._fingerprint(script) == self._fingerprint(script)

    def test_grader_fingerprint_bypasses_the_score_protocol(self, tmp_path: Path) -> None:
        # No line-1 float and no RULES line: nothing is being scored, and a float here would be
        # read as a score by anything that pipes the two modes through one reader.
        script = self._copy(tmp_path)
        printed = self._fingerprint(script)
        assert "RULES" not in printed
        try:
            float(printed)
        except ValueError:
            pass
        else:  # pragma: no cover - a hash that parses as a float is a contract violation
            raise AssertionError("--fingerprint printed something a score reader would accept")

    def test_grader_fingerprint_changes_when_an_expectation_changes(self, tmp_path: Path) -> None:
        # The answer key is PART of the instrument. Editing one row's expected values changes what
        # the suite measures just as surely as editing a check does.
        script = self._copy(tmp_path)
        before = self._fingerprint(script)
        spec = script.parent / "expectations" / "core-1.json"
        spec.write_text(spec.read_text(encoding="utf-8").replace("REPLACE/output/report.md", "out/x.md"), "utf-8")
        assert self._fingerprint(script) != before

    def test_grader_fingerprint_changes_when_the_script_changes(self, tmp_path: Path) -> None:
        script = self._copy(tmp_path)
        before = self._fingerprint(script)
        script.write_text(script.read_text(encoding="utf-8") + "\n# a new check would go here\n", "utf-8")
        assert self._fingerprint(script) != before

    @pytest.mark.parametrize(
        "stray",
        ["__pycache__/verify.cpython-313.pyc", "expectations/.DS_Store", "expectations/archive/retired.json"],
        ids=["pycache", "editor-junk", "retired-keys"],
    )
    def test_a_file_the_grader_never_reads_does_not_move_the_fingerprint(self, tmp_path: Path, stray: str) -> None:
        """The instrument is the script plus the keys it LOADS — not everything in the directory.

        A first version filtered `__pycache__` by name, which was DEAD CODE: the cache sits beside
        the script and the hash only ever walked `expectations/`, so the guard could not fire and
        its test passed with the guard deleted. Hashing what the grader actually reads makes all
        three of these true by construction, and none of them is a special case.
        """
        script = self._copy(tmp_path)
        before = self._fingerprint(script)
        target = script.parent / stray
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00 not part of the instrument \x01")
        assert self._fingerprint(script) == before

    def test_a_new_row_key_does_move_the_fingerprint(self) -> None:
        # The other half, so the test above cannot be satisfied by a fingerprint that ignores
        # everything: an expectations file the grader WOULD load is part of the instrument.
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            script = self._copy(Path(raw))
            before = self._fingerprint(script)
            (script.parent / "expectations" / "core-9.json").write_text('{"path": "x", "checks": {}}', "utf-8")
            assert self._fingerprint(script) != before

    def test_a_nul_in_an_expectation_cannot_forge_the_delimiter(self, tmp_path: Path) -> None:
        # Two DIFFERENT instruments must not hash alike. Without a length prefix, one file whose
        # content embeds `\0<path>\0` produces the same byte stream as two files with that path
        # and content — a rename-versus-edit collision on the one number the field rests on.
        one, two = self._copy(tmp_path / "one"), self._copy(tmp_path / "two")
        for grader in (one, two):
            for existing in (grader.parent / "expectations").glob("*.json"):
                existing.unlink()
        (one.parent / "expectations" / "a.json").write_bytes(b"X\0expectations/b.json\0Y")
        (two.parent / "expectations" / "a.json").write_bytes(b"X")
        (two.parent / "expectations" / "b.json").write_bytes(b"Y")
        assert self._fingerprint(one) != self._fingerprint(two)

    def test_a_fingerprint_that_cannot_be_computed_exits_non_zero(self, tmp_path: Path) -> None:
        """`--fingerprint` has the OPPOSITE failure protocol from every other path, deliberately.

        The grader exits 0 on every failure to protect a score it already computed. There is no
        score here — so falling through that guard printed `0.0000\ngrader failed: ...`, which
        `check=True` cannot catch and a caller doing `.stdout.strip()` records AS THE FINGERPRINT.
        Every later round then reports a changed instrument, forever, from a permissions error.
        """
        script = self._copy(tmp_path)
        target = next((script.parent / "expectations").glob("*.json"))
        target.chmod(0o000)
        try:
            completed = subprocess.run(
                [sys.executable, str(script), "--fingerprint"], capture_output=True, text=True, timeout=60
            )
        finally:
            target.chmod(0o644)
        assert completed.returncode != 0, completed.stdout
        assert completed.stdout.strip() == "", "a score-shaped line here is recorded as the fingerprint"
        assert "fingerprint failed" in completed.stderr

    def test_the_scoring_path_still_exits_zero_on_failure(self, tmp_path: Path) -> None:
        # The other side of the same coin: the non-zero exit must NOT have leaked into scoring,
        # where coder-eval checks the exit code before parsing the score and discards it.
        script = self._copy(tmp_path)
        completed = subprocess.run(
            [sys.executable, str(script), "no-such-row"], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0
        assert float(completed.stdout.splitlines()[0]) == 0.0

    def test_the_fingerprint_does_not_depend_on_where_the_grader_lives(self, tmp_path: Path) -> None:
        # Relative paths and content only — never an absolute path or an mtime, or the number
        # would differ on a colleague's machine and in CI while nothing had changed.
        first = self._copy(tmp_path / "a")
        second = self._copy(tmp_path / "b")
        assert self._fingerprint(first) == self._fingerprint(second)

    def test_round_scores_parses_without_a_fingerprint(self) -> None:
        # An existing measurements.json predating the field. `extra="forbid"` governs UNKNOWN keys;
        # a MISSING key is handled by the field default — different mechanisms, both needed here.
        scores = RoundScores.model_validate({"round": 1, "arm_row_scores": [], "pareto_front": []})
        assert scores.grader_fingerprint is None

    def test_store_round_trips_the_fingerprint(self, tmp_path: Path) -> None:
        # `record_round_scores` model_dump_json's the whole record, so the field needs no writer
        # change — asserted rather than assumed.
        from coder_eval.optimize.store import load_measurements, record_round_scores

        sidecar = tmp_path / "my-skill" / "measurements.json"
        record_round_scores(sidecar, RoundScores(round=1, grader_fingerprint="abc123"))
        assert load_measurements(sidecar).round_scores[0].grader_fingerprint == "abc123"

    @pytest.mark.parametrize(
        ("previous", "current", "expected"),
        [
            ("abc", "abc", False),
            ("abc", "def", True),
            (None, "abc", None),
            ("abc", None, None),
            (None, None, None),
        ],
        ids=["same", "changed", "previous-missing", "current-missing", "both-missing"],
    )
    def test_grader_changed_is_three_valued(
        self, previous: str | None, current: str | None, expected: bool | None
    ) -> None:
        # `None` means UNKNOWN and must never collapse to False: a round that recorded no
        # fingerprint would otherwise masquerade as an instrument that provably did not move.
        before = RoundScores(round=1, grader_fingerprint=previous)
        after = RoundScores(round=2, grader_fingerprint=current)
        assert grader_changed(before, after) is expected

    def test_grader_changed_without_a_previous_round_is_unknown(self) -> None:
        assert grader_changed(None, RoundScores(round=1, grader_fingerprint="abc")) is None


class TestRuleRowMap:
    def test_rule_row_map_inverts_the_rules_line(self) -> None:
        rows = {
            "r1": [grader_result("r1", 0.5, {"R1": "fail", "R2": "pass"})],
            "r2": [grader_result("r2", 1.0, {"R1": "pass", "R2": "pass"})],
            "r3": [grader_result("r3", 0.0, {"R2": "fail", "R3": "na"})],
        }
        assert rule_row_map(rows, 0).failed == {"R1": {"r1"}, "R2": {"r3"}, "R3": set()}

    def test_rule_row_map_any_replicate_failure_marks_the_row(self) -> None:
        # Matches the grader's own any-check rule, and for the same reason: both point at counting
        # the MOST rows as failing, which is what makes the ceiling an upper bound.
        rows = {
            "r1": [
                grader_result("r1", 1.0, {"R1": "pass"}),
                grader_result("r1", 0.5, {"R1": "fail"}),
                grader_result("r1", 1.0, {"R1": "pass"}),
            ]
        }
        assert rule_row_map(rows, 0).failed == {"R1": {"r1"}}

    def test_a_rule_that_failed_nowhere_is_a_key_with_an_empty_set(self) -> None:
        """The rule everything already passes must be SIZEABLE, not missing from the table.

        Keying only failures made "can a candidate for R5 promote?" unanswerable for exactly the
        rule whose answer matters most — no, its ceiling is zero — because the consumer iterates
        the map and R5 was not in it. It also removes the `rows=None` vs `rows=set()` trap: the
        key is always present, so a caller cannot reach for `.get()` and get the whole suite.
        """
        rows = {"r1": [grader_result("r1", 1.0, {"R5": "pass", "R6": "na"})]}
        attribution = rule_row_map(rows, 0)
        assert attribution.failed == {"R5": set(), "R6": set()}
        assert headroom_ceiling({"r1": 1.0}, rule="R5", rows=attribution.failed["R5"]).ceiling == 0.0

    def test_rule_row_map_returns_the_unattributed_rows(self) -> None:
        # RETURNED, not only logged: a consumer cannot recompute it, and without it every ceiling
        # is an under-estimate while the render prints a confident GAP.
        rows = {
            "r1": [grader_result("r1", 0.5, {"R1": "fail"})],
            "r2": [grader_result("r2", 0.5, None, line="")],
        }
        attribution = rule_row_map(rows, 0)
        assert attribution.failed == {"R1": {"r1"}}
        assert attribution.unattributed == ["r2"]

    def test_a_nested_stderr_marker_cannot_move_the_window(self) -> None:
        """The forgery the FIRST fix still allowed, and why both markers must be the first.

        Ending the window at the LAST `Stderr:` is defeated by the same untrusted text simply
        containing a second such line: the boundary moves back past the grader's real attribution
        and the forged one wins. Verified against the naive form before this test existed.
        """
        result = grader_result("r1", 0.5, {"R1": "fail"})
        criterion = result.success_criteria_results[0]
        criterion.details = (criterion.details or "").replace(
            "Stderr: (empty)",
            'Stderr:\nTraceback (most recent call last):\nRULES {"R9":"fail"}\nStderr: a nested marker',
        )
        assert _rules_verdicts(result, 0) == {"R1": "fail"}

    def test_rule_row_map_returns_empty_without_a_rules_line(self) -> None:
        # A pre-contract grader. The caller's remedy is the suite-level ceiling, and Step 7 says so
        # rather than printing an empty table that reads as "no rule has any headroom".
        rows = {"r1": [grader_result("r1", 0.5, None, line="")]}
        assert rule_row_map(rows, 0) == RuleAttribution(failed={}, unattributed=["r1"])

    def test_rule_row_map_reads_the_line_out_of_a_run_command_details_block(self) -> None:
        # The `RULES` line is the last line of the GRADER's stdout, never of the criterion's
        # details — `run_command` appends a `Stderr:` section after it. A reader taking the details'
        # last line finds nothing on every real run directory.
        details = grader_result("r1", 0.5, {"R1": "fail"}).success_criteria_results[0].details or ""
        assert not details.splitlines()[-1].startswith("RULES ")
        assert rule_row_map({"r1": [grader_result("r1", 0.5, {"R1": "fail"})]}, 0).failed == {"R1": {"r1"}}

    def test_an_empty_attribution_is_not_a_missing_one(self, caplog) -> None:
        # `RULES {}` is a CURRENT grader that attributed nothing; a missing line is an old one.
        # Only the second is warned about, because only the second has a remedy.
        with caplog.at_level(logging.WARNING):
            assert rule_row_map({"r1": [grader_result("r1", 1.0, {})]}, 0).failed == {}
        assert not caplog.records
        with caplog.at_level(logging.WARNING):
            rule_row_map({"r1": [grader_result("r1", 1.0, None, line="")]}, 0)
        assert any("RULES" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("line", ["RULES {not json", "RULES [1, 2]", "RULES "])
    def test_a_malformed_rules_line_is_not_an_attribution(self, line: str) -> None:
        # A grader whose line cannot be read attributes nothing rather than raising out of a
        # snippet the user is running after paying for the runs it reads.
        assert rule_row_map({"r1": [grader_result("r1", 0.5, None, line=line)]}, 0).failed == {}

    def test_a_criterion_index_past_the_end_attributes_nothing(self) -> None:
        assert rule_row_map({"r1": [grader_result("r1", 0.5, {"R1": "fail"})]}, 3).failed == {}

    def test_rule_row_map_rejects_a_negative_criterion_index(self) -> None:
        # Selection is positional, so -1 would silently read the LAST criterion's details.
        with pytest.raises(ValueError, match="criterion_index must be >= 0"):
            rule_row_map({}, -1)

    def test_rules_verdicts_reads_the_last_line_of_the_graders_own_stdout(self) -> None:
        # Agent output is untrusted: a row's ARTIFACT can contain a forged `RULES` line that the
        # grader quotes back into a detail. Within stdout the grader's own is emitted LAST by
        # construction, so a reverse scan takes it.
        forged = 'RULES {"R9":"fail"}\nRULES {"R1":"fail"}'
        assert _rules_verdicts(grader_result("r1", 0.5, None, line=forged), 0) == {"R1": "fail"}

    def test_a_forged_rules_line_in_stderr_does_not_outrank_the_grader(self) -> None:
        """The reverse scan alone is NOT enough, and the naive version had this backwards.

        `run_command` appends the `Stderr:` section AFTER stdout, so scanning the whole details
        field from the end reads the stderr side first — and a traceback there can quote artifact
        text, which is agent output. The window has to end at the last `Stderr:` marker.
        """
        result = grader_result("r1", 0.5, {"R1": "fail"})
        criterion = result.success_criteria_results[0]
        criterion.details = (criterion.details or "").replace(
            "Stderr: (empty)", 'Stderr:\nTraceback (most recent call last):\nRULES {"R9":"fail"}'
        )
        assert _rules_verdicts(result, 0) == {"R1": "fail"}
        assert rule_row_map({"r1": [result]}, 0).failed == {"R1": {"r1"}}

    def test_a_criterion_reporting_raw_stdout_is_still_read(self) -> None:
        # No `Stderr:` marker at all — the whole field is scanned, which is the behaviour every
        # non-`run_command` reader would depend on.
        result = grader_result("r1", 0.5, {"R1": "fail"})
        criterion = result.success_criteria_results[0]
        criterion.details = '0.5000\n1/2 applicable checks passed\nRULES {"R1":"fail"}'
        assert _rules_verdicts(result, 0) == {"R1": "fail"}


class TestLoadAndPair:
    """The load/pair/exclude step, called directly rather than only through the gate.

    Six concerns used to be interleaved in `activation_gate`'s first hundred lines. Testing them
    through the gate meant paying two bootstraps to assert a note, so most of them were asserted
    only incidentally.
    """

    def _pair(self, run_dirs: list[Path], **kwargs):
        return load_and_pair(
            **{
                "incumbent_run_dirs": run_dirs,
                "candidate_run_dirs": run_dirs,
                "incumbent_variant": "incumbent",
                "candidate_variant": "candidate",
                "suite_id": SUITE,
                "criterion_index": 0,
                **kwargs,
            }
        )

    def test_a_clean_pair_carries_every_row_and_no_notes(self, tmp_path: Path) -> None:
        rows = {f"r{i}": [("yes", "yes" if i else "no")] for i in range(4)}
        paired = self._pair(shared_dirs(tmp_path, rows, rows))
        assert paired.scored_row_ids == ["r0", "r1", "r2", "r3"]
        assert paired.rows_excluded == 0
        assert paired.notes == []
        # Four rows x three invocations, flattened from the same clusters.
        assert len(paired.incumbent_clusters) == 4
        assert len(paired.incumbent_pairs) == 12
        assert paired.incumbent_pairs == paired.candidate_pairs
        assert paired.n_discordant == 0

    def test_a_zero_row_incumbent_says_which_arm_and_what_did_not_match(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "candidate", {"r1": [("yes", "yes")]})
        paired = self._pair(run_dirs)
        assert paired.scored_row_ids == []
        assert any("the incumbent arm loaded ZERO rows" in n for n in paired.notes)
        assert not any("the candidate arm loaded ZERO rows" in n for n in paired.notes)

    def test_an_unpaired_row_on_each_side_is_excluded_and_counted(self, tmp_path: Path) -> None:
        incumbent = {"shared": [("yes", "yes")], "only-inc": [("yes", "yes")]}
        candidate = {"shared": [("yes", "yes")], "only-cand": [("yes", "yes")]}
        paired = self._pair(shared_dirs(tmp_path, incumbent, candidate))
        assert paired.scored_row_ids == ["shared"]
        assert paired.rows_excluded == 2
        assert any("only-cand, only-inc" in n for n in paired.notes)

    def test_a_hollow_row_is_dropped_from_both_vectors(self, tmp_path: Path) -> None:
        # The row directory exists on both arms, so it PAIRS — but only one arm scored it.
        run_dirs = shared_dirs(tmp_path, {"r1": [("yes", "yes")]}, {"r1": [("yes", "yes")]})
        for run_dir in run_dirs:
            write_row(run_dir, "candidate", "hollow", eval_result("hollow", []))
            write_row(run_dir, "incumbent", "hollow", eval_result("hollow", [("yes", "yes")]))
        paired = self._pair(run_dirs)
        assert paired.scored_row_ids == ["r1"]
        assert paired.rows_excluded == 1
        assert any("scored on only one arm" in n and "hollow" in n for n in paired.notes)

    def test_unbalanced_replicates_are_trimmed_and_the_drop_is_counted(self, tmp_path: Path) -> None:
        run_dirs = shared_dirs(tmp_path, {"r1": [("yes", "yes")]}, {"r1": [("yes", "yes")]})
        # A fourth candidate replicate for r1 only, which would otherwise weigh 4:3.
        write_row(run_dirs[0], "candidate", "r1", eval_result("r1", [("yes", "yes")]), replicate=1)
        paired = self._pair(run_dirs)
        assert len(paired.incumbent_pairs) == len(paired.candidate_pairs) == 3
        assert any("dropping 1 observation(s)" in n for n in paired.notes)

    def test_a_criterion_index_past_the_end_is_named_as_a_wiring_mistake(self, tmp_path: Path) -> None:
        rows = {"r1": [("yes", "yes")]}
        paired = self._pair(shared_dirs(tmp_path, rows, rows), criterion_index=7)
        assert paired.incumbent_pairs == [] and paired.candidate_pairs == []
        assert any("criterion_index=7 selected NO classification results" in n for n in paired.notes)
        assert any("the index is past the end" in n for n in paired.notes)

    def test_the_returned_notes_list_is_the_one_the_gate_keeps_appending_to(self, tmp_path: Path) -> None:
        # Pydantic COPIES the list at construction, so a note appended after the model is built is
        # silently discarded. The gate must hold THIS list, not a copy of it.
        rows = {"r1": [("yes", "yes")]}
        paired = self._pair(shared_dirs(tmp_path, rows, rows))
        before = len(paired.notes)
        paired.notes.append("added by the caller")
        assert len(paired.notes) == before + 1


class TestReadSplitProvenance:
    """`None` (no --split was passed) and "unrecorded" (we cannot tell) are different answers."""

    def test_a_recorded_split_is_read(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "r"
        write_row(run_dir, "v", "r1", eval_result("r1", [("yes", "yes")]))
        set_split(run_dir, "train")
        assert read_split_provenance([run_dir]) == SplitProvenance(recorded=frozenset({"train"}), unrecorded=0)

    def test_a_recorded_null_split_is_recorded_not_unrecorded(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "r"
        write_row(run_dir, "v", "r1", eval_result("r1", [("yes", "yes")]))
        provenance = read_split_provenance([run_dir])
        assert provenance == SplitProvenance(recorded=frozenset({None}), unrecorded=0)
        assert provenance.value is None

    @pytest.mark.parametrize(
        ("name", "write"),
        [
            ("no run.json", lambda p: None),
            ("unparseable JSON", lambda p: (p / "run.json").write_text("{not json", encoding="utf-8")),
            ("run.json is a list", lambda p: (p / "run.json").write_text("[]", encoding="utf-8")),
            ("no row_selection key", lambda p: (p / "run.json").write_text('{"run_id": "x"}', encoding="utf-8")),
            (
                "row_selection is null",
                lambda p: (p / "run.json").write_text('{"row_selection": null}', encoding="utf-8"),
            ),
        ],
    )
    def test_every_unreadable_shape_counts_as_unrecorded(self, tmp_path: Path, name: str, write) -> None:
        run_dir = tmp_path / "r"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").unlink(missing_ok=True)
        write(run_dir)
        provenance = read_split_provenance([run_dir])
        assert provenance == SplitProvenance(recorded=frozenset(), unrecorded=1), name
        assert provenance.value == UNRECORDED_SPLIT

    def test_mixed_dirs_report_both_halves(self, tmp_path: Path) -> None:
        recorded = tmp_path / "a"
        write_row(recorded, "v", "r1", eval_result("r1", [("yes", "yes")]))
        set_split(recorded, "test")
        missing = tmp_path / "b"
        missing.mkdir()
        provenance = read_split_provenance([recorded, missing])
        assert provenance == SplitProvenance(recorded=frozenset({"test"}), unrecorded=1)
        # Any unrecorded dir makes the whole measurement uncacheable, whatever the others said.
        assert provenance.value == UNRECORDED_SPLIT

    def test_different_recorded_splits_are_mismatched(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        for run_dir, split in ((a, "train"), (b, "test")):
            write_row(run_dir, "v", "r1", eval_result("r1", [("yes", "yes")]))
            set_split(run_dir, split)
        assert read_split_provenance([a, b]).mismatched is True

    def test_the_same_split_everywhere_is_not_mismatched(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        for run_dir in (a, b):
            write_row(run_dir, "v", "r1", eval_result("r1", [("yes", "yes")]))
            set_split(run_dir, "train")
        provenance = read_split_provenance([a, b])
        assert provenance.mismatched is False and provenance.value == "train"


class TestLoadAndPairStages:
    """The stages `load_and_pair` composes, unit-tested apart from the loading and the notes.

    `load_and_pair` was radon cc 30 and interleaved five concerns; every one of its notes already
    had an end-to-end test through the real run tree, which is what pins the RENDERED behaviour.
    These are the arithmetic, driven directly, so a boundary can be moved without paying a run
    directory to find out what it did.
    """

    @staticmethod
    def _rows(spec: dict[str, list[list[tuple[str, str]]]]) -> dict[str, list[EvaluationResult]]:
        """One arm as `{row id: [labels per replicate]}` — no disk, no run directory."""
        return {rid: [eval_result(rid, labels) for labels in replicates] for rid, replicates in spec.items()}

    def test_pairing_splits_the_three_row_sets(self) -> None:
        incumbent = self._rows(
            {
                "shared": [[("yes", "yes")]],
                "only-inc": [[("yes", "yes")]],
                # Present on both arms but scored on neither side here — an errored row.
                "hollow": [[]],
            }
        )
        candidate = self._rows(
            {"shared": [[("yes", "no")]], "only-cand": [[("yes", "yes")]], "hollow": [[("yes", "yes")]]}
        )
        pairing = _pair_rows(incumbent_rows=incumbent, candidate_rows=candidate, criterion_index=0)
        assert pairing.paired_row_ids == ["hollow", "shared"]
        assert pairing.unpaired == ["only-cand", "only-inc"]
        # `hollow` PAIRS — its directory exists on both arms — and is then dropped for scoring on one.
        assert pairing.hollow == ["hollow"]
        assert pairing.scored_row_ids == ["shared"]

    def test_a_row_scored_on_neither_arm_is_not_hollow(self) -> None:
        """`hollow` is an ASYMMETRY, not an absence: `bool(inc) != bool(cand)`.

        A row both arms failed to score is excluded from `scored_row_ids` all the same, but it
        introduces no bias between the arms — so naming it in the one-arm-only note would send the
        reader looking for a difference that is not there.
        """
        rows = self._rows({"r1": [[]]})
        pairing = _pair_rows(incumbent_rows=rows, candidate_rows=rows, criterion_index=0)
        assert pairing.paired_row_ids == ["r1"]
        assert pairing.hollow == []
        assert pairing.scored_row_ids == []

    def test_balancing_trims_to_the_smaller_count_and_counts_the_drop(self) -> None:
        per_row = {
            "even": ([("yes", "yes")], [("yes", "yes")]),
            "uneven": ([("yes", "yes")] * 3, [("yes", "yes")]),
        }
        clusters = _balance_clusters(per_row=per_row, scored_row_ids=["even", "uneven"])
        assert clusters.unbalanced_rows == ["uneven"]
        # 3 + 1 observations become 1 + 1, so two are dropped.
        assert clusters.dropped == 2
        assert len(clusters.incumbent_pairs) == len(clusters.candidate_pairs) == 2

    def test_discordance_is_counted_on_the_balanced_clusters(self) -> None:
        """The count that bounds the discreteness floor, and it must describe what was compared.

        Computed on the raw clusters instead, a row trimmed from 3:1 could read as discordant on
        observations the comparison never saw — which would report a floor for a sample that does
        not exist.
        """
        per_row = {"trimmed": ([("yes", "yes"), ("yes", "no"), ("yes", "no")], [("yes", "yes")])}
        clusters = _balance_clusters(per_row=per_row, scored_row_ids=["trimmed"])
        # After the trim both arms hold exactly `("yes", "yes")`, so the row is CONCORDANT.
        assert clusters.n_discordant == 0

    def test_replicate_order_does_not_make_a_row_discordant(self) -> None:
        # `sorted`, never `==`: the same pairs in a different replicate order is the same row.
        per_row = {
            "r1": (
                [("yes", "yes"), ("yes", "no")],
                [("yes", "no"), ("yes", "yes")],
            )
        }
        assert _balance_clusters(per_row=per_row, scored_row_ids=["r1"]).n_discordant == 0

    def test_a_genuinely_discordant_row_is_counted(self) -> None:
        per_row = {"r1": ([("yes", "yes")], [("yes", "no")])}
        assert _balance_clusters(per_row=per_row, scored_row_ids=["r1"]).n_discordant == 1

    def test_the_wrong_path_note_names_only_the_empty_arm(self) -> None:
        notes = _wrong_path_notes(
            (
                ("incumbent", "incumbent", {}, [Path("/runs/a")]),
                ("candidate", "candidate", self._rows({"r1": [[("yes", "yes")]]}), [Path("/runs/a")]),
            ),
            SUITE,
        )
        assert len(notes) == 1
        assert "the incumbent arm loaded ZERO rows" in notes[0]
        assert "/runs/a" in notes[0] and SUITE in notes[0]

    def test_the_wrong_path_note_says_so_when_there_were_no_run_dirs_at_all(self) -> None:
        notes = _wrong_path_notes((("incumbent", "incumbent", {}, []),), SUITE)
        assert len(notes) == 1 and "no run dirs were given" in notes[0]

    def test_the_no_results_note_names_the_types_it_actually_found(self) -> None:
        rows = self._rows({"r1": [[("yes", "yes")]]})
        note = _no_results_note(incumbent_rows=rows, candidate_rows=rows, criterion_index=7)
        assert "criterion_index=7" in note
        assert "the index is past the end" in note
        # And it says what it is NOT, because the two look identical in the numbers.
        assert "NOT the same as the skill never firing" in note


class TestRowsExcludedComposesBothCauses:
    """`rows_excluded` is ONE number over TWO causes, and it is declared in exactly one place.

    The existing tests cover each cause alone (2 unpaired, 1 hollow). Neither would notice a stage
    boundary that recomputed the number from only the half it can see, which is why the plan's own
    edge case says to compute it once, at the end.
    """

    def test_unpaired_and_hollow_rows_are_summed(self, tmp_path: Path) -> None:
        incumbent = {"shared": [("yes", "yes")], "only-inc": [("yes", "yes")]}
        candidate = {"shared": [("yes", "yes")], "only-cand": [("yes", "yes")]}
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        for run_dir in run_dirs:
            write_row(run_dir, "incumbent", "hollow", eval_result("hollow", [("yes", "yes")]))
            write_row(run_dir, "candidate", "hollow", eval_result("hollow", []))
        paired = load_and_pair(
            incumbent_run_dirs=run_dirs,
            candidate_run_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
        )
        assert paired.scored_row_ids == ["shared"]
        # two present in one arm only, plus one that paired and scored on one arm only
        assert paired.rows_excluded == 3
        assert any("only-cand, only-inc" in n for n in paired.notes)
        assert any("scored on only one arm" in n and "hollow" in n for n in paired.notes)


def _two_criteria(row_id: str, first: float, second: float) -> EvaluationResult:
    """A row carrying TWO criteria, so an index past the first is a real selection rather than a hole."""
    base = grader_result(row_id, first, None)
    return copy_with(
        base,
        success_criteria_results=[
            *base.success_criteria_results,
            CriterionResult(criterion_type="file_check", description=f"second for {row_id}", score=second),
        ],
    )


class TestRowReplicateScores:
    """`row_score` reduced over a whole arm without averaging — the reproducibility reading."""

    def _rows(self, per_row: dict[str, list[float]]) -> dict[str, list[EvaluationResult]]:
        return {row_id: [scored_result(row_id, s) for s in scores] for row_id, scores in per_row.items()}

    def test_an_index_reads_that_criterion_across_replicates_sorted_by_row(self) -> None:
        rows = {
            "r2": [grader_result("r2", 0.5, None), grader_result("r2", 0.75, None)],
            "r1": [grader_result("r1", 1.0, None)],
        }
        result = row_replicate_scores(rows, 0)
        assert result == {"r1": [1.0], "r2": [0.5, 0.75]}
        # Dict equality ignores order, so the "sorted by row id" half of the contract needs its own
        # assertion — the input is deliberately given out of order.
        assert list(result) == ["r1", "r2"]

    def test_none_reads_the_rows_weighted_score(self) -> None:
        assert row_replicate_scores(self._rows({"r1": [0.25, 0.75]})) == {"r1": [0.25, 0.75]}

    def test_an_index_past_every_row_raises_naming_the_real_count(self) -> None:
        # An empty map would read as "no rows scored" — a measurement result for an authoring error,
        # and one discovered only after the runs are paid for.
        rows = {"r1": [grader_result("r1", 1.0, None)]}
        with pytest.raises(ValueError, match="past every row's criteria list") as excinfo:
            row_replicate_scores(rows, 7)

        assert "carries 1" in str(excinfo.value)
        assert not isinstance(excinfo.value, IndexError)

    def test_a_replicate_missing_the_criterion_is_dropped_and_the_row_survives(self) -> None:
        # A crashed replicate is a hole, not a reason to refuse the other fourteen rows. `r2`'s
        # second replicate carries only one criterion, so index 1 is absent for it alone.
        rows = {
            "r1": [_two_criteria("r1", 1.0, 0.5), _two_criteria("r1", 1.0, 0.5)],
            "r2": [_two_criteria("r2", 1.0, 0.25), grader_result("r2", 1.0, None)],
        }
        assert row_replicate_scores(rows, 1) == {"r1": [0.5, 0.5], "r2": [0.25]}

    def test_a_row_with_no_usable_replicate_is_absent_rather_than_empty(self) -> None:
        rows = {"r1": [_two_criteria("r1", 1.0, 0.5)], "r2": [grader_result("r2", 1.0, None)]}
        assert row_replicate_scores(rows, 1) == {"r1": [0.5]}

    def test_an_arm_whose_every_replicate_scored_nothing_does_not_raise(self) -> None:
        """The documented exemption: the raise needs a width to measure against.

        With no scored criterion anywhere, a bad index and a wholly-crashed arm are the same input.
        Naming the arm is the CALLER's job, and `replicates_report` is what does it.
        """
        rows = {"r1": [copy_with(grader_result("r1", 1.0, None), success_criteria_results=[])]}
        assert row_replicate_scores(rows, 7) == {}

    def test_an_empty_rows_map_is_an_empty_dict_rather_than_a_raise(self) -> None:
        # Round 1 with a mistyped variant reaches this, and the renderer handles an empty map.
        assert row_replicate_scores({}, 0) == {}
        assert render_row_replicates({}, {}) == "_No rows to compare._"

    def test_a_negative_index_is_rejected_at_the_boundary(self) -> None:
        with pytest.raises(ValueError, match="criterion_index must be >= 0"):
            row_replicate_scores({"r1": [grader_result("r1", 1.0, None)]}, -1)
