"""Unit tests for `coder_eval.optimize.activation` — does a candidate DESCRIPTION get engaged?

`f1.yes` over `skill_triggered`, paired cluster bootstrap over rows. Track-specific: the discreteness
floor and its refusal, the sibling-annexation checks, the cross-split refusal, and seed stability.
"""

import inspect
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest

from coder_eval.models import (
    ActivationGateVerdict,
    ClassificationCriterionResult,
    ConfirmVerdict,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    GuardrailCheck,
    ResolvedTask,
    TaskDefinition,
    copy_with,
)
from coder_eval.optimize.activation import (
    SeedStability,
    _activation_preflight,
    _discreteness_floor,
    _refusal_message,
    _sibling_checks,
    activation_gate,
    confirm_gate,
    derive_sibling_indices,
    gate_seed_stability,
    holm_promote,
    measure_noise_floor,
    min_discordant_rows,
    noise_floor_mde,
)
from coder_eval.optimize.gate import (
    FLOOR_RESOLUTION,
    GATE_RESAMPLES,
    MATERIALITY_FLOOR,
    NOTE_OUTSIDE_FAMILY,
    cost_latency_guardrails,
    note_holm_family,
)
from coder_eval.optimize.load import load_suite_rows
from coder_eval.optimize.store import UNRECORDED_SPLIT
from coder_eval.reports_optimize import _render_checks, render_markdown, render_seed_stability
from coder_eval.reports_stats import BOOTSTRAP_RESAMPLES, DEFAULT_ALPHA, bootstrap_p_floor
from tests.optimize_fixtures import (
    FAST_RESAMPLES,
    REFUSAL_RESAMPLES,
    RUN_SELECTION_KEY,
    SUITE,
    activation_verdict,
    activation_verdict_over_arms,
    confirm_activation_arms,
    cost_check,
    cost_rows,
    eval_result,
    execution_floor,
    full_activation_verdict,
    module_source,
    set_split,
    shared_dirs,
    split_labelled_arms,
    tiny_suite,
    weighted_arm,
    write_arm,
    write_row,
)


class TestActivationGate:
    def _clear_win(self) -> tuple[dict, dict]:
        # 12 positive rows: the incumbent engages on 3, the candidate on all 12.
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}
        return incumbent, candidate

    def test_promotes_a_clearly_better_candidate(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.rows_paired == 12
        assert verdict.ci_low is not None and verdict.ci_low > 0.0
        assert holm_promote([verdict])[0].promoted is True

    def test_leaves_promoted_none_before_holm(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        assert activation_verdict(shared_dirs(tmp_path, incumbent, candidate)).promoted is None

    def test_refuses_a_tied_candidate(self, tmp_path: Path) -> None:
        rows = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(12)}
        verdict = activation_verdict(shared_dirs(tmp_path, rows, dict(rows)))
        assert verdict.mean_diff == 0.0
        assert verdict.ci_low is not None and verdict.ci_high is not None
        assert verdict.ci_low <= 0.0 <= verdict.ci_high
        assert holm_promote([verdict])[0].promoted is False

    def test_reports_range_non_overlap_as_diagnostic_only(self, tmp_path: Path) -> None:
        # 4 rows, candidate strictly ahead on every invocation (ranges do not overlap) but too
        # few clusters for the interval to exclude zero.
        incumbent = {f"r{i}": [("yes", "no")] for i in range(4)}
        candidate = {"r0": [("yes", "yes")], "r1": [("yes", "no")], "r2": [("yes", "no")], "r3": [("yes", "no")]}
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.range_non_overlap is True
        assert verdict.ci_low is not None and verdict.ci_low <= 0.0
        assert holm_promote([verdict])[0].promoted is False

    def test_excludes_unpaired_rows_and_counts_them(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "yes")] for i in range(5)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(4)}  # r4 missing on the candidate
        candidate["extra"] = [("yes", "yes")]
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.rows_paired == 4
        assert verdict.rows_excluded == 2
        assert any("only one arm" in note for note in verdict.notes)

    def test_a_row_scored_on_only_one_arm_is_excluded_from_both(self, tmp_path: Path) -> None:
        """The asymmetry that promotes a candidate for CRASHING on the rows it would have missed.

        A row that times out or errors is written with an empty ``success_criteria_results`` —
        the row directory exists, so it pairs, but it contributes a pair to only one arm. Left
        in, the two arms' F1s are computed over different row sets, and the bias runs toward
        the candidate: here the candidate "wins" 1.000 vs 0.667 purely by producing nothing on
        the six rows it was failing.
        """
        incumbent = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(12)}
        candidate = {f"r{i}": ([("yes", "yes")] if i % 2 else []) for i in range(12)}
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate))

        assert verdict.rows_paired == 6
        assert verdict.rows_excluded == 6
        assert verdict.incumbent_f1 == verdict.candidate_f1 == 1.0
        assert verdict.mean_diff == 0.0
        assert holm_promote([verdict])[0].promoted is False
        assert any("scored on only one arm" in note for note in verdict.notes)

    def test_with_fewer_than_two_paired_rows_returns_none_stats(self, tmp_path: Path) -> None:
        verdict = activation_verdict(shared_dirs(tmp_path, {"r0": [("yes", "yes")]}, {"r0": [("yes", "yes")]}))
        assert verdict.rows_paired == 1
        assert (verdict.mean_diff, verdict.ci_low, verdict.ci_high, verdict.p_value) == (None, None, None, None)
        assert verdict.promoted is False
        assert any("fewer than the 2" in note for note in verdict.notes)

    def test_a_wrong_path_yields_zero_rows_and_never_raises(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        verdict = activation_gate(
            incumbent_run_dirs=run_dirs,
            candidate_run_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id="a-suite-that-does-not-exist",
            criterion_index=0,
        )
        assert verdict.rows_paired == 0
        assert verdict.promoted is False
        # A mistyped path must be LOUD in the verdict: "0 rows" alone reads identically to a
        # genuinely tiny suite, which is the silent-zero conflation this note exists to break.
        note = " ".join(verdict.notes)
        assert "loaded ZERO rows" in note
        assert "a-suite-that-does-not-exist" in note
        assert "not a result" in note

    def test_a_wrong_variant_id_names_the_arm_that_found_nothing(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        verdict = activation_gate(
            incumbent_run_dirs=run_dirs,
            candidate_run_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variant="typo-variant",
            suite_id=SUITE,
            criterion_index=0,
        )
        note = " ".join(verdict.notes)
        assert "the candidate arm loaded ZERO rows" in note
        assert "the incumbent arm loaded ZERO rows" not in note

    def test_bad_criterion_index_is_noted_not_raised(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate), criterion_index=7)
        # Every row exists but none is scored at that position, so nothing is comparable.
        assert (verdict.rows_paired, verdict.rows_excluded) == (0, 12)
        note = " ".join(verdict.notes)
        assert "criterion_index=7" in note
        assert "POSITION" in note
        # Distinguishable from "the skill never fired", which yields pairs with observed='no'.
        assert "never firing" in note
        assert holm_promote([verdict])[0].promoted is False

    def test_negative_criterion_index_raises_rather_than_grading_the_last_criterion(self, tmp_path: Path) -> None:
        # The lower bound the internal `>= len(...)` guards cannot see. Selection is POSITIONAL,
        # so -1 does not fail — it grades the LAST criterion on every row and returns a confident
        # number for a criterion nobody asked about.
        incumbent, candidate = self._clear_win()
        with pytest.raises(ValueError, match="criterion_index must be >= 0, got -1"):
            activation_verdict(shared_dirs(tmp_path, incumbent, candidate), criterion_index=-1)

    def test_criterion_index_zero_is_legal(self, tmp_path: Path) -> None:
        # The boundary the guard must NOT reject.
        incumbent, candidate = self._clear_win()
        assert activation_verdict(shared_dirs(tmp_path, incumbent, candidate), criterion_index=0).rows_paired > 0

    def test_sibling_recall_drop_blocks_promotion(self, tmp_path: Path) -> None:
        # Criterion 0 is the target (candidate wins outright); criterion 1 is a sibling the
        # candidate annexes on half the rows — a false negative there, so recall.yes drops.
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no"), ("yes", "yes")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes"), ("yes", "yes" if i % 2 else "no")] for i in range(12)}
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate), sibling_indices=[1])
        assert verdict.ci_low is not None and verdict.ci_low > 0.0
        assert [c.passed for c in verdict.sibling_checks] == [False]

        decided = holm_promote([verdict])[0]
        assert decided.promoted is False
        assert any("moved the failure" in note for note in decided.notes)

    def test_sibling_with_no_true_instances_is_not_a_regression(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no"), ("no", "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes"), ("no", "no")] for i in range(12)}
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate), sibling_indices=[1])
        assert [c.passed for c in verdict.sibling_checks] == [True]
        assert verdict.sibling_checks[0].note is not None
        assert holm_promote([verdict])[0].promoted is True

    def test_a_sibling_index_that_selects_nothing_is_noted_not_scored(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate), sibling_indices=[4])
        check = verdict.sibling_checks[0]
        assert check.passed is True
        assert check.incumbent is None and check.candidate is None
        assert check.note is not None and "no classification results on either arm" in check.note

    def test_a_sibling_present_on_one_arm_only_is_not_blamed_on_the_candidate(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no"), ("yes", "yes")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}  # no sibling criterion at all
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate), sibling_indices=[1])
        check = verdict.sibling_checks[0]
        assert check.passed is True
        assert check.note is not None and "one arm only" in check.note

    def test_the_verdict_records_the_interval_width_it_used(self, tmp_path: Path) -> None:
        # A 90% interval labelled 95% is the failure this pins: the renderer reads the width off
        # the verdict rather than assuming one.
        incumbent, candidate = self._clear_win()
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate), confidence=0.90)
        assert verdict.confidence == 0.90
        assert verdict.n_resamples == FAST_RESAMPLES
        assert "90% CI" in render_markdown(verdict)

    def test_is_deterministic_for_a_seed(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        assert activation_verdict(run_dirs, seed=3) == activation_verdict(run_dirs, seed=3)


class TestHolmPromote:
    def _verdict(self, name: str, p: float | None, diff: float | None = 0.2) -> ActivationGateVerdict:
        return ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant=name,
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=BOOTSTRAP_RESAMPLES,
            rows_paired=12,
            rows_excluded=0,
            incumbent_f1=0.4,
            candidate_f1=0.6,
            mean_diff=diff,
            ci_low=0.05,
            ci_high=0.35,
            p_value=p,
        )

    def test_step_down_across_a_family_is_not_bonferroni(self) -> None:
        # Bonferroni (p <= alpha/3) would promote only the first; Holm's step-down promotes two.
        verdicts = [self._verdict("a", 0.001), self._verdict("b", 0.02), self._verdict("c", 0.9)]
        assert [v.promoted for v in holm_promote(verdicts)] == [True, True, False]

    def test_single_verdict_reduces_to_plain_alpha(self) -> None:
        assert holm_promote([self._verdict("a", 0.049)])[0].promoted is True
        assert holm_promote([self._verdict("a", 0.051)])[0].promoted is False

    def test_empty_list_returns_empty(self) -> None:
        assert holm_promote([]) == []

    def test_a_family_of_only_undecidable_arms_returns_them_all_unpromoted(self) -> None:
        decided = holm_promote([self._verdict("a", None), self._verdict("b", None)])
        assert [v.promoted for v in decided] == [False, False]

    def test_excludes_none_p_values_from_the_family(self) -> None:
        # The undecidable arm must not tighten the correction for its siblings: with it counted,
        # the family size would be 3 and `b` (p=0.03 > 0.05/2) would fail the second step.
        verdicts = [self._verdict("a", 0.001), self._verdict("b", 0.03), self._verdict("c", None)]
        decided = holm_promote(verdicts)
        assert [v.promoted for v in decided] == [True, True, False]
        assert any("outside the family" in note for note in decided[2].notes)

    def test_records_the_alpha_it_applied(self) -> None:
        assert holm_promote([self._verdict("a", 0.001)], alpha=0.01)[0].holm_alpha == 0.01

    def test_a_difference_favouring_the_incumbent_never_promotes(self) -> None:
        decided = holm_promote([self._verdict("a", 0.001, diff=-0.3)])[0]
        assert decided.promoted is False
        assert any("incumbent's favour" in note for note in decided.notes)


class TestNoiseFloorMde:
    def test_returns_a_positive_half_width(self, tmp_path: Path) -> None:
        # A noisy incumbent: the same rows flip between invocations, so the null comparison has
        # a real spread and the MDE is above zero.
        run_dirs = []
        for i in range(4):
            run_dir = tmp_path / f"run-{i}"
            for row in range(10):
                observed = "yes" if (row + i) % 3 else "no"
                write_row(run_dir, "incumbent", f"r{row}", eval_result(f"r{row}", [("yes", observed)]))
            run_dirs.append(run_dir)

        mde = noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0)
        assert mde is not None and mde > 0.0

    def test_none_with_a_single_invocation(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {f"r{i}": [("yes", "yes")] for i in range(6)}, invocations=1)
        assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) is None

    def test_none_with_fewer_than_two_rows(self, tmp_path: Path) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"only": [("yes", "yes")]}, invocations=3)
        assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) is None

    def test_an_odd_invocation_count_splits_two_one(self, tmp_path: Path) -> None:
        # 3 invocations must still produce a floor (a 2/1 split), not None.
        run_dirs = write_arm(tmp_path, "incumbent", {f"r{i}": [("yes", "yes")] for i in range(6)}, invocations=3)
        assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) == 0.0


class TestMdeAndGuardrailsInTheVerdict:
    def test_gate_notes_a_difference_below_the_mde(self, tmp_path: Path) -> None:
        # Both arms noisy in the same way: the difference is tiny, and the incumbent's own
        # invocation-to-invocation spread is larger than it.
        run_dirs = []
        for i in range(4):
            run_dir = tmp_path / f"run-{i}"
            for row in range(10):
                incumbent_observed = "yes" if (row + i) % 3 else "no"
                candidate_observed = "yes" if (row + i + 1) % 3 else "no"
                write_row(run_dir, "incumbent", f"r{row}", eval_result(f"r{row}", [("yes", incumbent_observed)]))
                write_row(run_dir, "candidate", f"r{row}", eval_result(f"r{row}", [("yes", candidate_observed)]))
            run_dirs.append(run_dir)

        verdict = activation_verdict(run_dirs)
        assert verdict.mde is not None and verdict.mde > 0.0
        assert verdict.mean_diff is not None and abs(verdict.mean_diff) < verdict.mde
        assert any("minimum detectable effect" in note for note in verdict.notes)

    def test_gate_notes_when_the_mde_cannot_be_computed(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate, invocations=1))
        assert verdict.mde is None
        assert any("could not be computed" in note for note in verdict.notes)

    def test_gate_fills_guardrails_from_the_scored_rows(self, tmp_path: Path) -> None:
        incumbent, candidate = ({f"r{i}": [("yes", "yes")] for i in range(12)} for _ in range(2))
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        verdict = activation_verdict(run_dirs)
        assert [c.name for c in verdict.guardrails] == ["cost (USD/row)", "latency (seconds/row)"]
        # These fixtures record no cost, so the cost guardrail must pass WITH A NOTE, never bare.
        assert all(c.passed for c in verdict.guardrails)
        assert cost_check(verdict.guardrails).note is not None

    def test_render_markdown_prints_the_mde_and_every_guardrail_note(self, tmp_path: Path) -> None:
        incumbent, candidate = ({f"r{i}": [("yes", "yes")] for i in range(12)} for _ in range(2))
        text = render_markdown(holm_promote([activation_verdict(shared_dirs(tmp_path, incumbent, candidate))])[0])
        assert "Minimum detectable effect: 0.000" in text
        assert "cost (USD/row)" in text
        assert "not evaluated" in text
        assert "latency (seconds/row)" in text

    def test_render_markdown_shows_the_guardrail_interval(self) -> None:
        incumbent = cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = cost_rows({f"r{i}": [2.0] for i in range(12)})
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=BOOTSTRAP_RESAMPLES,
            rows_paired=12,
            rows_excluded=0,
            incumbent_f1=0.4,
            candidate_f1=0.9,
            mean_diff=0.5,
            ci_low=0.2,
            ci_high=0.75,
            p_value=0.002,
            guardrails=cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate),
        )
        text = render_markdown(verdict)
        assert "FAIL · cost (USD/row)" in text
        assert "diff CI [" in text
        assert "x incumbent" in text


class TestDiscretenessFloor:
    def test_matches_the_closed_form(self) -> None:
        # 2*(1-R/M)^M: the probability a resample draws NO discordant row, doubled for two tails.
        assert _discreteness_floor(6, 3, 20_000) == pytest.approx(2 * 0.5**6)
        assert _discreteness_floor(8, 4, 20_000) == pytest.approx(2 * 0.5**8)
        assert _discreteness_floor(12, 6, 20_000) == pytest.approx(2 * 0.5**12)

    def test_is_bounded_by_the_estimator_floor(self) -> None:
        # Every row discordant -> the analytic term is 0, so the arithmetic's own floor wins.
        assert _discreteness_floor(40, 40, 2000) == pytest.approx(bootstrap_p_floor(2000))

    def test_of_identical_arms_is_one(self) -> None:
        # Zero discordant rows: (1-0)^M = 1, doubled and clamped. Two identical arms cannot be
        # separated at any alpha, and that is the honest reading rather than a bug.
        assert _discreteness_floor(6, 0, 20_000) == 1.0

    def test_no_rows_is_one(self) -> None:
        assert _discreteness_floor(0, 0, 20_000) == 1.0

    def test_more_discordant_than_rows_does_not_go_negative(self) -> None:
        assert _discreteness_floor(4, 9, 2000) == pytest.approx(bootstrap_p_floor(2000))


class TestMinDiscordantRows:
    """The lever a refusal names: how many rows the arms must DISAGREE on, not how many rows.

    Every expectation is a literal verified against the shipped module, and `alpha` is bound from
    `DEFAULT_ALPHA` rather than spelled `0.05` — the same rule the prose sensors enforce on the
    surfaces, applied to the tests that pin them.
    """

    def test_reproduces_the_sizing_figures(self) -> None:
        assert min_discordant_rows(8, DEFAULT_ALPHA) == 3
        assert min_discordant_rows(10, DEFAULT_ALPHA) == 4
        assert min_discordant_rows(20, DEFAULT_ALPHA) == 4
        assert min_discordant_rows(20, DEFAULT_ALPHA / 5) == 5
        assert min_discordant_rows(6, 0.001) == 5

    def test_returns_the_smallest_count_that_clears(self) -> None:
        # The contract is "smallest", so the answer must clear and its predecessor must not.
        for n_rows, threshold in ((8, DEFAULT_ALPHA), (10, DEFAULT_ALPHA), (20, DEFAULT_ALPHA / 5)):
            required = min_discordant_rows(n_rows, threshold)
            assert required is not None
            assert _discreteness_floor(n_rows, required, GATE_RESAMPLES) <= threshold
            assert _discreteness_floor(n_rows, required - 1, GATE_RESAMPLES) > threshold

    def test_the_row_count_is_not_the_lever(self) -> None:
        # Holding R fixed and ADDING rows makes the floor rise, which is why "add rows" is the
        # wrong remedy and this function exists. Same shape as the docstring's worked figures.
        floors = [_discreteness_floor(m, 3, GATE_RESAMPLES) for m in (8, 10, 20)]
        assert floors == sorted(floors) and floors[0] < floors[-1]

    def test_no_rows_clears_nothing(self) -> None:
        assert min_discordant_rows(0, DEFAULT_ALPHA) is None
        assert min_discordant_rows(-3, DEFAULT_ALPHA) is None

    def test_a_threshold_above_one_is_cleared_by_a_single_row(self) -> None:
        # The floor is clamped at 1.0, so any threshold at or above it is met by R = 1. Not
        # special-cased into None, which would report "impossible" for the trivially possible.
        assert min_discordant_rows(10, 1.0) == 1
        assert min_discordant_rows(10, 1.5) == 1

    def test_an_unclearable_bar_returns_none_rather_than_n_rows(self) -> None:
        # Every row discordant leaves the estimator's own floor, so a threshold below THAT cannot
        # be met at any R — at any suite size, since that floor is a function of the draw count
        # alone. The caller must send the reader to n_resamples, not to rows.
        assert min_discordant_rows(6, bootstrap_p_floor(2_000) / 2, 2_000) is None


class TestReusedRunDirIsRefused:
    """`run.json` is per-INVOCATION; the tree under it is APPEND-ONLY.

    A second `coder-eval run --run-dir <same dir> --split test` leaves the first split's rows on
    disk while rewriting `row_selection` to say `test`. The cross-split refusal cannot see it —
    provenance reads clean, single-valued — and because both arms are subdirectories of the SAME
    run dir the contamination is symmetric: the stale rows pair on both sides, so there is no
    `rows_excluded` bump and no unpaired-rows note. The only trace is a `rows_paired` larger than
    the split, which nothing else flags.
    """

    def _clean(self, tmp_path: Path) -> Path:
        run_dir = tmp_path / "run-0"
        for row in ("t1", "t2", "t3"):
            write_row(run_dir, "incumbent", row, eval_result(row, [("yes", "no")]))
            write_row(run_dir, "candidate", row, eval_result(row, [("yes", "yes")]))
        return run_dir

    def _gate_one(self, run_dir: Path, **kwargs):
        return activation_gate(
            incumbent_run_dirs=[run_dir],
            candidate_run_dirs=[run_dir],
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=FAST_RESAMPLES,
            **kwargs,
        )

    def test_the_defect_end_to_end_a_tree_holding_two_splits_is_refused(self, tmp_path: Path) -> None:
        """THE test that pins the finding. Before this preflight it returned a confident interval."""
        run_dir = self._clean(tmp_path)
        # The earlier invocation's train rows, still on disk, described by no run.json.
        for row in ("r1", "r2"):
            write_row(run_dir, "incumbent", row, eval_result(row, [("yes", "no")]), record=False)
            write_row(run_dir, "candidate", row, eval_result(row, [("yes", "yes")]), record=False)

        # Symmetric contamination: both arms pair the stale rows, so nothing else notices.
        assert set(load_suite_rows(run_dir, "incumbent", SUITE)) == {"t1", "t2", "t3", "r1", "r2"}

        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is not None
        assert (verdict.mean_diff, verdict.ci_low, verdict.ci_high, verdict.p_value) == (None, None, None, None)
        assert (verdict.incumbent_f1, verdict.candidate_f1) == (None, None)
        # Per LOCATION, and actionable: how many stale results, across how many rows, WHERE.
        # A tree-wide arm x dir total would be unreconcilable with the `Rows paired` line the
        # same block prints four lines below it.
        assert "2 result(s) across 2 row(s)" in verdict.gate_refusal
        assert "incumbent" in verdict.gate_refusal and "candidate" in verdict.gate_refusal
        assert "r1/00" in verdict.gate_refusal and "fresh --run-dir" in verdict.gate_refusal

    def test_it_renders_as_not_a_result_and_carries_no_p(self, tmp_path: Path) -> None:
        # A wiring refusal, so it joins the `NOT A RESULT` family — distinguishable in a ledger
        # from the discreteness refusal, which is the only one that ever carries a p.
        run_dir = self._clean(tmp_path)
        write_row(run_dir, "candidate", "stale", eval_result("stale", [("yes", "yes")]), record=False)
        decided = holm_promote([self._gate_one(run_dir)])[0]
        assert decided.p_value is None and decided.promoted is False
        text = render_markdown(decided)
        assert "NOT A RESULT" in text
        assert "CANNOT SEPARATE AT THIS SIZE" not in text

    def test_a_stale_replicate_inside_a_recorded_row_is_refused(self, tmp_path: Path) -> None:
        """Row ids alone are blind one level down, and the trigger is mundane.

        Re-using a run dir with a smaller `--repeats` leaves the earlier call's `<NN>` dirs inside
        rows the new run.json DOES record. `load_suite_rows` pools every replicate it finds and
        `balance_pair` trims symmetrically — so, again, nothing else flags it and the gate returns
        a confident interval over contaminated clusters.
        """
        run_dir = self._clean(tmp_path)  # every row recorded at replicate 00
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            write_row(run_dir, variant, "t1", eval_result("t1", [("yes", observed)]), 1, record=False)
        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is not None
        assert "t1/01" in verdict.gate_refusal

    def test_a_recorded_replicate_is_not_flagged(self, tmp_path: Path) -> None:
        # The anti-over-fire half of the replicate key: a legitimate --repeats 2 run records both.
        run_dir = self._clean(tmp_path)
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            for row in ("t1", "t2", "t3"):
                write_row(run_dir, variant, row, eval_result(row, [("yes", observed)]), 1)
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_entry_with_no_replicate_index_covers_every_replicate_of_its_row(self, tmp_path: Path) -> None:
        # Permissive on ambiguity, exactly as for a missing variant_id: an unattributable entry
        # means "cannot rule this one in", not "this one is stale".
        run_dir = self._clean(tmp_path)
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            write_row(run_dir, variant, "t1", eval_result("t1", [("yes", observed)]), 1, record=False)
        payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        for entry in payload["task_results"]:
            entry.pop("replicate_index", None)
        (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_unreadable_suite_directory_degrades_to_a_note(self, tmp_path: Path) -> None:
        # `iterdir` can raise; the function's contract is to degrade to "cannot tell", exactly as
        # `read_split_provenance` does for an unreadable run.json.
        run_dir = self._clean(tmp_path)
        suite_dir = run_dir / "candidate" / SUITE
        suite_dir.chmod(0o000)
        try:
            verdict = self._gate_one(run_dir)
        finally:
            suite_dir.chmod(0o755)
        assert verdict.gate_refusal is None
        assert any("cannot be reconciled" in note for note in verdict.notes)

    def test_the_arms_in_different_run_dirs_name_both_locations(self, tmp_path: Path) -> None:
        inc_dir, cand_dir = tmp_path / "inc", tmp_path / "cand"
        for run_dir, variant, observed in ((inc_dir, "incumbent", "no"), (cand_dir, "candidate", "yes")):
            for row in ("t1", "t2", "t3"):
                write_row(run_dir, variant, row, eval_result(row, [("yes", observed)]))
            write_row(run_dir, variant, "stale", eval_result("stale", [("yes", observed)]), record=False)
        verdict = activation_gate(
            incumbent_run_dirs=[inc_dir],
            candidate_run_dirs=[cand_dir],
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        assert str(inc_dir) in verdict.gate_refusal and str(cand_dir) in verdict.gate_refusal

    def test_more_than_three_stale_results_are_truncated_with_an_ellipsis(self, tmp_path: Path) -> None:
        run_dir = self._clean(tmp_path)
        for row in ("s1", "s2", "s3", "s4"):
            write_row(run_dir, "candidate", row, eval_result(row, [("yes", "yes")]), record=False)
        refusal = self._gate_one(run_dir).gate_refusal
        assert refusal is not None
        assert "4 result(s) across 4 row(s)" in refusal and "…" in refusal

    def test_an_unreconcilable_sibling_dir_is_named_in_the_refusal(self, tmp_path: Path) -> None:
        # The note that would say so is unreachable past the refusal's return, so the refusal
        # itself has to carry it — otherwise its totals silently exclude a whole directory.
        dirty, opaque = tmp_path / "dirty", tmp_path / "opaque"
        for run_dir in (dirty, opaque):
            for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
                for row in ("t1", "t2", "t3"):
                    write_row(run_dir, variant, row, eval_result(row, [("yes", observed)]))
        write_row(dirty, "candidate", "stale", eval_result("stale", [("yes", "yes")]), record=False)
        (opaque / "run.json").unlink()
        verdict = activation_gate(
            incumbent_run_dirs=[dirty, opaque],
            candidate_run_dirs=[dirty, opaque],
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        assert "could not be reconciled either way" in verdict.gate_refusal

    def test_a_clean_run_dir_is_neither_refused_nor_noted(self, tmp_path: Path) -> None:
        # The anti-over-fire test, and the overwhelmingly common path: the guard must not fire by
        # default, and must not add a note to every block either.
        verdict = self._gate_one(self._clean(tmp_path))
        assert verdict.gate_refusal is None
        assert not any("task_results" in note or "re-used --run-dir" in note for note in verdict.notes)

    def test_a_missing_run_json_degrades_to_a_note(self, tmp_path: Path) -> None:
        run_dir = self._clean(tmp_path)
        (run_dir / "run.json").unlink()
        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert any("cannot be reconciled" in note for note in verdict.notes)

    def test_a_malformed_run_json_degrades_to_a_note(self, tmp_path: Path) -> None:
        run_dir = self._clean(tmp_path)
        (run_dir / "run.json").write_text('{"task_results": [', encoding="utf-8")
        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert any("cannot be reconciled" in note for note in verdict.notes)

    def test_a_run_json_predating_task_results_degrades_to_a_note(self, tmp_path: Path) -> None:
        # Old run dirs must stay gatable: the one state where contamination is undetectable must
        # not also be the one state that refuses everything.
        run_dir = self._clean(tmp_path)
        (run_dir / "run.json").write_text(json.dumps({RUN_SELECTION_KEY: {"split": None}}), encoding="utf-8")
        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert any("cannot be reconciled" in note for note in verdict.notes)

    def test_a_tree_holding_fewer_rows_than_recorded_does_not_refuse(self, tmp_path: Path) -> None:
        # An interrupted write or a deleted row is not this defect; only rows the run.json never
        # wrote are. Nothing is unrecorded here, so there is nothing to refuse.
        run_dir = self._clean(tmp_path)
        shutil.rmtree(run_dir / "incumbent" / SUITE / "t3")
        shutil.rmtree(run_dir / "candidate" / SUITE / "t3")
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_entry_with_no_variant_id_counts_for_every_variant(self, tmp_path: Path) -> None:
        # Permissive on ambiguity: the harm is a FALSE refusal blocking a real promotion, and an
        # unattributable entry means "cannot rule this row in", not "this row is stale".
        run_dir = self._clean(tmp_path)
        write_row(run_dir, "incumbent", "extra", eval_result("extra", [("yes", "no")]), record=False)
        write_row(run_dir, "candidate", "extra", eval_result("extra", [("yes", "yes")]), record=False)
        payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        payload["task_results"].append({"task_id": f"{SUITE}/extra"})  # no variant_id
        (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        assert self._gate_one(run_dir).gate_refusal is None

    def test_another_suite_in_the_same_run_dir_is_not_mistaken_for_a_stale_row(self, tmp_path: Path) -> None:
        # A run dir legitimately holds several suites and variants. The reconciliation is scoped to
        # the arms' own suite, so a sibling suite's rows are neither counted nor blamed.
        run_dir = self._clean(tmp_path)
        other = run_dir / "incumbent" / "some-other-suite" / "x" / "00"
        other.mkdir(parents=True)
        (other / "task.json").write_text(eval_result("x", [("yes", "yes")]).model_dump_json(), encoding="utf-8")
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_empty_row_directory_is_not_a_row(self, tmp_path: Path) -> None:
        # A directory holding no task.json is not a scored row — the reconciliation reads names but
        # still requires the replicate glob to match, so a stray mkdir cannot fabricate a refusal.
        run_dir = self._clean(tmp_path)
        (run_dir / "candidate" / SUITE / "leftover-dir").mkdir()
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_aggregate_rebuilt_run_dir_does_not_false_positive(self, tmp_path: Path) -> None:
        """`coder-eval aggregate` rebuilds run.json FROM the tree, so it must never be refused.

        Built through the REAL rebuild path (`recover_task_results` -> `build_run_summary` ->
        `write_run_summary`), not by hand-writing a run.json that assumes the answer. It is also
        this check's documented blind spot from the other side: because the record is derived from
        the tree, an already-contaminated dir is LAUNDERED into a clean reading by an aggregate.
        That is stated on `reconcile_tree_against_run_json` and accepted.
        """
        from coder_eval.orchestration.batch import build_run_summary, recover_task_results, write_run_summary

        run_dir = tmp_path / "resumed"
        # Rows on disk carrying their own variant_id, as a real run writes them. `record=False`
        # because the rebuild below is what writes run.json here — that IS the resume path.
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            for row in ("t1", "t2", "t3"):
                result = copy_with(eval_result(row, [("yes", observed)]), variant_id=variant)
                write_row(run_dir, variant, row, result, record=False)
        (run_dir / "run.json").unlink()  # the interrupted run left none

        recovered = recover_task_results(run_dir)
        assert len(recovered) == 6, "fixture: the rebuild must see every row on disk"
        summary = build_run_summary("resumed", recovered, datetime(2026, 8, 16), datetime(2026, 8, 16))
        write_run_summary(summary, run_dir)

        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert not any("cannot be reconciled" in note for note in verdict.notes)
        assert verdict.rows_paired == 3

    def test_a_resumed_run_dir_does_not_false_positive(self, tmp_path: Path) -> None:
        """The one assumption that would have invalidated this preflight, checked on the real path.

        `--resume` does NOT go through `recover_task_results` — that is `aggregate`'s. It calls
        `partition_for_resume` over the RESOLVED task set, reloads the already-finished ones into
        `prior_results`, and `run_batch` folds `[*prior_results, *processed]` into the summary. So
        run.json describes the whole resolved set, including rows this invocation did not execute.
        Modelled exactly: three rows already on disk from the interrupted run, one more executed
        now, and the summary built from the union — as `batch.py` does.
        """
        from coder_eval.orchestration.batch import build_run_summary, partition_for_resume, write_run_summary
        from coder_eval.path_utils import build_task_run_dir

        run_dir = tmp_path / "resumed"
        rows = ("t1", "t2", "t3")
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            for row in rows:
                result = copy_with(eval_result(row, [("yes", observed)]), variant_id=variant)
                write_row(run_dir, variant, row, result, record=False)
        (run_dir / "run.json").unlink()  # the interrupted run left none

        # `partition_for_resume` reads the RESOLVED set — what this invocation would run — and
        # finds every one of them already finished on disk.
        resolved = [
            ResolvedTask(
                task=TaskDefinition(
                    task_id=f"{SUITE}/{row}",
                    description="row",
                    initial_prompt="p",
                    success_criteria=[FileExistsCriterion(path="x", description="x")],
                ),
                task_file=Path("t.yaml"),
                # The PER-TASK dir: `_load_completed_result` reads `rt.run_dir / "task.json"`.
                run_dir=build_task_run_dir(run_dir, variant, f"{SUITE}/{row}"),
                variant_id=variant,
            )
            for variant in ("incumbent", "candidate")
            for row in rows
        ]
        to_run, prior_results, _prior_resolved = partition_for_resume(resolved)
        assert (len(to_run), len(prior_results)) == (0, 6), "fixture: every row must read as finished"

        # `run_batch` folds `[*prior_results, *processed]` into the summary; nothing new ran here.
        summary = build_run_summary("resumed", list(prior_results), datetime(2026, 8, 16), datetime(2026, 8, 16))
        write_run_summary(summary, run_dir)

        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert not any("cannot be reconciled" in note for note in verdict.notes)

    def test_an_empty_run_dir_is_not_refused(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "empty"
        run_dir.mkdir()
        (run_dir / "run.json").write_text(json.dumps({"task_results": []}), encoding="utf-8")
        assert self._gate_one(run_dir).gate_refusal is None


class TestGateRefusal:
    """A suite that structurally cannot separate is REFUSED, not reported as a negative result."""

    # Resample-sensitive, so an explicit count rather than the helper's — but 2,000 rather than the
    # real GATE_RESAMPLES, because what these assert is the ANALYTIC term. At 6 rows / 3 discordant
    # the floor is 2*(1-3/6)^6 = 0.03125, and it dominates the estimator's 2/(m+1) for any m above
    # 63; 20,000 draws would compute the identical floor and take ten times as long.

    def _gated(self, tmp_path: Path, positives: int, distractors: int, family: int, **kwargs):
        incumbent, candidate = tiny_suite(positives, distractors)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        verdicts = [activation_verdict(run_dirs, n_resamples=REFUSAL_RESAMPLES, **kwargs) for _ in range(family)]
        return holm_promote(verdicts)

    def test_six_row_suite_refuses_rather_than_reporting_a_negative(self, tmp_path: Path) -> None:
        # 6 rows, 3 discordant -> floor 0.031, against a family-of-2 Holm threshold of 0.025.
        decided = self._gated(tmp_path, positives=3, distractors=3, family=2)
        for verdict in decided:
            assert verdict.gate_refusal is not None
            assert verdict.promoted is False
            assert "1 survivor(s)" in verdict.gate_refusal
            text = render_markdown(verdict)
            assert "CANNOT SEPARATE AT THIS SIZE" in text
            assert "NOT PROMOTED" not in text

    def test_a_healthy_suite_does_not_refuse(self, tmp_path: Path) -> None:
        decided = self._gated(tmp_path, positives=6, distractors=6, family=2)
        for verdict in decided:
            assert verdict.gate_refusal is None
            assert verdict.promoted is True

    def test_a_refused_suite_stays_refused_across_seeds(self, tmp_path: Path) -> None:
        """Without `and refusal is None` this fails on roughly half the seeds.

        `p_floor` bounds the p's EXPECTATION, so a realized p dips below it about half the time.
        The guard, not the seed, is what decides.
        """
        for seed in range(8):
            decided = self._gated(tmp_path / f"s{seed}", positives=3, distractors=3, family=2, seed=seed)
            assert [v.promoted for v in decided] == [False, False], f"seed {seed} promoted an unpromotable suite"

    def test_refusal_does_not_outrank_undecided(self) -> None:
        # Holm never ran, so there is no threshold for the floor to be refused against.
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=GATE_RESAMPLES,
            rows_paired=6,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.03,
            p_floor=0.9,
        )
        text = render_markdown(verdict)
        assert "UNDECIDED" in text
        assert "CANNOT SEPARATE" not in text

    def test_refusal_ranks_above_a_failing_guardrail(self, tmp_path: Path) -> None:
        # Reading a guardrail presupposes a statistic that separated, which a refused suite's
        # did not — so the refusal is the headline.
        failing = GuardrailCheck(
            name="cost (USD/row)",
            incumbent=1.0,
            candidate=2.0,
            relative_change=1.0,
            tolerance=MATERIALITY_FLOOR,
            ci_low=0.6,
            ci_high=1.4,
            passed=False,
        )
        incumbent, candidate = tiny_suite(3, 3)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        base = activation_verdict(run_dirs, n_resamples=REFUSAL_RESAMPLES)
        decided = holm_promote([base.model_copy(update={"guardrails": [failing]})] * 2)
        text = render_markdown(decided[0])
        assert "CANNOT SEPARATE AT THIS SIZE" in text
        assert "BLOCKED BY A GUARDRAIL" not in text

    def test_a_refused_verdict_is_never_promoted(self) -> None:
        # The Monte-Carlo undershoot, constructed directly: p BELOW p_floor, floor above the bar.
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=GATE_RESAMPLES,
            rows_paired=6,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.0001,
            p_floor=0.03125,
        )
        decided = holm_promote([verdict, verdict])[0]
        assert decided.promoted is False
        assert decided.gate_refusal is not None
        text = render_markdown(decided)
        assert "CANNOT SEPARATE AT THIS SIZE" in text
        assert "**PROMOTED**" not in text

    def test_the_refusal_is_not_duplicated_into_notes(self, tmp_path: Path) -> None:
        decided = self._gated(tmp_path, positives=3, distractors=3, family=2)[0]
        assert decided.gate_refusal is not None
        assert not any("cannot express a p below" in note for note in decided.notes)
        # And the ordinary negative-result note is suppressed too — it would contradict the headline.
        assert not any("ordinary negative result" in note for note in decided.notes)

    def test_identical_arms_are_diagnosed_as_the_candidate_not_the_suite(self, tmp_path: Path) -> None:
        """Zero discordant rows is a DIFFERENT finding, and the remedy is not "add rows".

        `2*(1-R/M)**M` shrinks with M only when the discordance RATE is non-zero, so at R=0 the
        floor is 1.0 at every suite size — no number of extra rows changes it. Telling an operator
        to buy more rows for a candidate that behaved identically to the incumbent is the
        misdiagnosis this branch exists to prevent, and it is the most common degenerate outcome
        in this workflow (a wrong `plugins:` path gives exactly this shape).
        """
        rows = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(8)}
        verdict = activation_verdict(shared_dirs(tmp_path, rows, dict(rows)))
        assert verdict.p_floor == 1.0
        decided = holm_promote([verdict, verdict])[0]
        assert decided.promoted is False
        assert decided.gate_refusal is not None
        assert "identical labels on every one of the 8 scored rows" in decided.gate_refusal
        assert "adding more rows LIKE THESE cannot change it" in decided.gate_refusal
        # And NOT the suite-size remedy, which would be false here.
        assert "survivor(s) at alpha" not in decided.gate_refusal
        assert "the answer is more rows" not in decided.gate_refusal
        assert "CANNOT SEPARATE AT THIS SIZE" in render_markdown(decided)

    def test_the_refusal_is_rank_scoped_not_a_claim_about_every_candidate(self, tmp_path: Path) -> None:
        # `p_floor` is a property of the SUITE and identical across the family, but `threshold`
        # depends on rank — so a worse-ranked sibling can escape the refusal and promote. A
        # message claiming "no candidate can promote here" would contradict its own block.
        decided = self._gated(tmp_path, positives=3, distractors=3, family=2)[0]
        assert decided.gate_refusal is not None
        assert "This candidate could not have promoted" in decided.gate_refusal
        assert "No candidate can promote here" not in decided.gate_refusal

    def test_the_remedy_names_the_discordant_count_that_would_clear_the_bar(self, tmp_path: Path) -> None:
        """10 rows, 3 discordant: the floor (0.056) exceeds alpha, and "add rows" is FALSE here.

        At R = 3 fixed, buying rows raises the floor. The honest remedy names the discordant count
        — 4 at this row count — beside the 3 the suite actually has.
        """
        decided = self._gated(tmp_path, positives=3, distractors=7, family=1)[0]
        assert decided.n_discordant == 3
        refusal = decided.gate_refusal
        assert refusal is not None
        required = min_discordant_rows(10, DEFAULT_ALPHA, REFUSAL_RESAMPLES)
        assert required == 4
        assert f"from {decided.n_discordant} to {required}" in refusal
        assert "makes this floor worse" in refusal
        # The sentence this replaces: unconditionally true only when the added rows are discordant.
        assert "the answer is more rows, not fewer candidates" not in refusal

    def test_the_identical_arms_message_carries_none_of_the_row_remedy(self, tmp_path: Path) -> None:
        # The R = 0 branch owns its own diagnosis; the discordance remedy must not leak into it.
        rows = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(8)}
        decided = holm_promote([activation_verdict(shared_dirs(tmp_path, rows, dict(rows)))] * 2)[0]
        assert decided.n_discordant == 0
        assert decided.gate_refusal is not None
        assert "DISAGREE on" not in decided.gate_refusal
        assert "identical labels on every one of the 8 scored rows" in decided.gate_refusal

    def test_a_verdict_without_a_discordant_count_says_nothing_about_one(self) -> None:
        # `n_discordant` is None on the no-interval path, and a remedy must never invent it.
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=GATE_RESAMPLES,
            rows_paired=6,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.01,
            p_floor=0.03125,
        )
        # A family of two: the floor (0.031) exceeds alpha/2, so the refusal fires.
        refusal = holm_promote([verdict, verdict])[0].gate_refusal
        assert refusal is not None
        assert "survivor(s) at alpha" in refusal
        assert "DISAGREE on" not in refusal

    def test_an_unclearable_bar_does_not_prescribe_rows_that_cannot_work(self) -> None:
        """When no discordant count clears the bar, rows are not the lever — and saying so is wrong.

        `min_discordant_rows` returns None exactly when the floor at `R == M` — which collapses to
        the estimator's own `2/(m+1)`, a function of the DRAW COUNT and of nothing about the suite
        — is still above the threshold. So a message telling that reader to add rows sends them to
        spend on something provably incapable of helping.
        """
        resamples = 200  # a coarse estimator floor, so the bar sits under it
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=resamples,
            rows_paired=6,
            rows_excluded=0,
            n_discordant=2,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.001,
            p_floor=0.4,
        )
        assert min_discordant_rows(6, DEFAULT_ALPHA / 8, resamples) is None
        refusal = holm_promote([verdict] * 8)[0].gate_refusal
        assert refusal is not None
        assert f"{resamples} bootstrap draws" in refusal
        assert "larger n_resamples" in refusal
        assert "more rows AND more disagreement" not in refusal

    def test_max_family_zero_says_no_family_size_works(self) -> None:
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=GATE_RESAMPLES,
            rows_paired=4,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.01,
            p_floor=0.5,  # alpha/0.5 < 1
        )
        decided = holm_promote([verdict])[0]
        assert decided.gate_refusal is not None
        assert "No family size works" in decided.gate_refusal


class TestPFloorOnTheVerdict:
    def test_the_gate_sets_it_from_the_discordant_rows(self, tmp_path: Path) -> None:
        incumbent, candidate = tiny_suite(3, 3)
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.p_floor == pytest.approx(_discreteness_floor(6, 3, FAST_RESAMPLES))

    def test_a_row_whose_replicates_are_reordered_is_concordant(self, tmp_path: Path) -> None:
        # Multiset comparison, not sequence: the same pairs in a different replicate order is the
        # same row on both arms, and counting it discordant would understate the floor.
        run_dirs = []
        for i in range(2):
            run_dir = tmp_path / f"run-{i}"
            for arm, order in (("incumbent", 0), ("candidate", 1)):
                for row in range(4):
                    labels = [("yes", "yes")] if (i == order) else [("yes", "no")]
                    write_row(run_dir, arm, f"r{row}", eval_result(f"r{row}", labels))
            run_dirs.append(run_dir)
        verdict = activation_verdict(run_dirs)
        assert verdict.p_floor == pytest.approx(_discreteness_floor(4, 0, FAST_RESAMPLES))

    def test_no_interval_means_no_floor(self, tmp_path: Path) -> None:
        verdict = activation_verdict(shared_dirs(tmp_path, {"r0": [("yes", "yes")]}, {"r0": [("yes", "yes")]}))
        assert verdict.p_value is None
        assert verdict.p_floor is None
        # `None`, not 0: "the arms agreed everywhere" is a finding, "there was no comparison" is not.
        assert verdict.n_discordant is None
        assert holm_promote([verdict])[0].gate_refusal is None

    def test_the_gate_records_the_count_the_floor_came_from(self, tmp_path: Path) -> None:
        incumbent, candidate = tiny_suite(3, 7)
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate))
        assert (verdict.rows_paired, verdict.n_discordant) == (10, 3)
        assert verdict.p_floor == pytest.approx(_discreteness_floor(10, 3, FAST_RESAMPLES))

    def test_all_rows_discordant_records_the_row_count(self, tmp_path: Path) -> None:
        incumbent, candidate = tiny_suite(4, 0)
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.n_discordant == verdict.rows_paired == 4

    def test_render_prints_the_discordant_count_beside_the_paired_one(self, tmp_path: Path) -> None:
        # The quantity `p_floor` is computed from, visible without having to trigger a refusal.
        incumbent, candidate = tiny_suite(3, 7)
        verdict = activation_verdict(shared_dirs(tmp_path, incumbent, candidate))
        assert "Rows paired: 10 · discordant: 3 · excluded: 0" in render_markdown(verdict)

    def test_render_shows_a_dash_when_there_was_no_comparison(self, tmp_path: Path) -> None:
        verdict = activation_verdict(shared_dirs(tmp_path, {"r0": [("yes", "yes")]}, {"r0": [("yes", "yes")]}))
        assert "discordant: —" in render_markdown(verdict)

    def test_render_reports_both_floors(self, tmp_path: Path) -> None:
        incumbent, candidate = tiny_suite(6, 6)
        verdict = holm_promote([activation_verdict(shared_dirs(tmp_path, incumbent, candidate))])[0]
        text = render_markdown(verdict)
        assert f"estimator {bootstrap_p_floor(FAST_RESAMPLES):.4f}" in text
        assert verdict.p_floor is not None
        assert f"this suite {verdict.p_floor:.4f}" in text


class TestEveryMissingFloorSaysWhy:
    """A silent None on a spend-gating function was the shipped defect; each one now names why."""

    def test_activation_floor_names_too_few_invocations(self, tmp_path: Path, caplog) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {f"r{i}": [("yes", "yes")] for i in range(6)}, invocations=1)
        with caplog.at_level(logging.WARNING):
            assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) is None
        assert "at least 2 invocations" in caplog.text

    def test_activation_floor_names_too_few_scored_rows(self, tmp_path: Path, caplog) -> None:
        run_dirs = write_arm(tmp_path, "incumbent", {"only": [("yes", "yes")]}, invocations=3)
        with caplog.at_level(logging.WARNING):
            assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) is None
        assert "in BOTH halves of the invocation split" in caplog.text

    def test_a_mistyped_run_directory_is_not_silent(self, tmp_path: Path, caplog) -> None:
        # The measured defect: a wrong path returned a bare None and printed nothing, on the one
        # function whose job is to stop a user spending.
        with caplog.at_level(logging.WARNING):
            assert (
                noise_floor_mde(
                    run_dirs=[tmp_path / "typo-a", tmp_path / "typo-b"],
                    variant_id="incumbent",
                    suite_id=SUITE,
                    criterion_index=0,
                )
                is None
            )
        assert "No noise floor could be computed" in caplog.text

    def test_the_activation_floor_names_the_path_not_the_criterion_index(self, tmp_path: Path, caplog) -> None:
        # The parity gap with the execution twin: without its own wrong-path guard this reported
        # "only 0 row(s) ... scored a classification result at criterion 0", sending the reader to
        # check the criterion index when the real fault is the variant / suite / run directory.
        with caplog.at_level(logging.WARNING):
            assert (
                measure_noise_floor(
                    run_dirs=[tmp_path / "typo-a", tmp_path / "typo-b"],
                    variant_id="incumbent",
                    suite_id=SUITE,
                    criterion_index=0,
                    model="m",
                )
                is None
            )
        assert "wrong variant id, a wrong suite id or a wrong run directory" in caplog.text
        assert "at criterion" not in caplog.text, "the criterion index is the wrong thing to blame here"

    def test_execution_floor_names_too_few_replicated_rows(self, tmp_path: Path, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert execution_floor(weighted_arm(tmp_path, "incumbent", {"r0": [0.1, 0.2]})) is None
        assert "carry 2+ replicates" in caplog.text


class TestActivationPreflight:
    """The two row-selection preflights as a unit, rather than through two bootstraps.

    Extracted from `activation_gate` verbatim — every pre-existing preflight test passes unmodified,
    which is the extraction's own acceptance criterion. These add what testing it through the gate
    could not: the precedence between the two causes, and the notes-list identity contract.
    """

    ROWS: ClassVar[dict[str, list[tuple[str, str]]]] = {f"r{i}": [("yes", "yes" if i else "no")] for i in range(3)}

    def _preflight(self, incumbent: list[Path], candidate: list[Path]) -> tuple[str | None, list[str]]:
        return _activation_preflight(
            incumbent_run_dirs=incumbent,
            candidate_run_dirs=candidate,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
        )

    def _split(self, run_dirs: list[Path], split: str | None) -> None:
        for run_dir in run_dirs:
            payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            payload["row_selection"] = {"split": split}
            (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_a_clean_pair_refuses_nothing_and_notes_nothing(self, tmp_path: Path) -> None:
        dirs = shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        assert self._preflight(dirs, dirs) == (None, [])

    def test_a_cross_split_pair_refuses(self, tmp_path: Path) -> None:
        inc = write_arm(tmp_path / "i", "incumbent", self.ROWS, invocations=1)
        cand = write_arm(tmp_path / "c", "candidate", self.ROWS, invocations=1)
        self._split(inc, "train")
        self._split(cand, "test")
        refusal, _notes = self._preflight(inc, cand)
        assert refusal is not None and "DIFFERENT --split values" in refusal

    def test_a_contaminated_tree_refuses(self, tmp_path: Path) -> None:
        dirs = shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        write_row(dirs[0], "candidate", "stale", eval_result("stale", [("yes", "yes")]), record=False)
        refusal, _notes = self._preflight(dirs, dirs)
        assert refusal is not None and "no recorded invocation wrote" in refusal

    def test_the_cross_split_cause_wins_when_both_hold(self, tmp_path: Path) -> None:
        """PRECEDENCE, and it is program order rather than an accident.

        A pair that is both cross-split and contaminated must report the cross-split cause: it is
        the more specific one, and its remedy ("re-run both arms under one --split") is actionable
        without first understanding the other. Reversing the two checks would silently swap which
        message a user acts on.
        """
        inc = write_arm(tmp_path / "i", "incumbent", self.ROWS, invocations=1)
        cand = write_arm(tmp_path / "c", "candidate", self.ROWS, invocations=1)
        self._split(inc, "train")
        self._split(cand, "test")
        write_row(cand[0], "candidate", "stale", eval_result("stale", [("yes", "yes")]), record=False)
        refusal, _notes = self._preflight(inc, cand)
        assert refusal is not None
        assert "DIFFERENT --split values" in refusal
        assert "no recorded invocation wrote" not in refusal

    def test_missing_provenance_is_a_note_not_a_refusal(self, tmp_path: Path) -> None:
        dirs = shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        (dirs[0] / "run.json").unlink()
        refusal, notes = self._preflight(dirs, dirs)
        assert refusal is None, "old run dirs stay gatable"
        assert any("row-selection provenance is missing" in note for note in notes)

    def test_an_unreconcilable_dir_is_a_note_not_a_refusal(self, tmp_path: Path) -> None:
        dirs = shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        payload = json.loads((dirs[0] / "run.json").read_text(encoding="utf-8"))
        payload.pop("task_results", None)
        (dirs[0] / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        refusal, notes = self._preflight(dirs, dirs)
        assert refusal is None
        assert any("record no `task_results`" in note for note in notes)

    def test_the_unknown_count_survives_inside_a_refusal(self, tmp_path: Path) -> None:
        # Both halves must reach the reader: a refusal whose totals silently exclude a directory
        # that could not be checked is a refusal a user cannot reconcile with the tree.
        dirs = shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=2)
        write_row(dirs[0], "candidate", "stale", eval_result("stale", [("yes", "yes")]), record=False)
        payload = json.loads((dirs[1] / "run.json").read_text(encoding="utf-8"))
        payload.pop("task_results", None)
        (dirs[1] / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        refusal, _notes = self._preflight(dirs, dirs)
        assert refusal is not None
        assert "no recorded invocation wrote" in refusal
        assert "could not be reconciled either way" in refusal

    def test_the_notes_list_the_caller_holds_is_the_one_that_reaches_the_verdict(self, tmp_path: Path) -> None:
        """The identity contract the extraction had to preserve.

        `activation_gate` holds the SAME list object `load_and_pair` returned, because pydantic
        COPIES it at construction — so a note appended after the model is built is silently
        discarded. Returning a fresh list and `extend`-ing preserves that; re-binding `notes` to a
        concatenation would not, and every later note would land in a list no verdict ever sees.
        """
        dirs = shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        (dirs[0] / "run.json").unlink()
        verdict = activation_verdict(dirs)
        # The preflight's note AND a note `load_and_pair` wrote before it are both on the verdict,
        # which can only happen if one list carried both.
        assert any("row-selection provenance is missing" in note for note in verdict.notes)
        # And a note the gate appends AFTER the preflight also survives.
        assert any("minimum detectable effect" in note for note in verdict.notes)


class TestTheMdeNoteNamesTheRealCause:
    """`activation_gate`'s MDE note used to name ONE cause unconditionally, and there are five.

    It said "(a null comparison needs at least two invocations of the incumbent)" whatever had
    actually happened — reproduced against shipped code on an incumbent with TWO invocations where
    one row scored in both halves, which rendered that sentence beside `len(run_dirs) == 2`. The
    reason is threaded out through `noise_floor_mde(reasons=...)`, an additive keyword-only sink,
    so the public `float | None` return the skill's snippets import is unchanged.

    Five REACHABLE causes, each witnessed below. `floor_from_clusters` records a sixth — the
    bootstrap declining on fewer than 2 clusters — which both floors' own `< 2` guards make
    unreachable from them, so it is deliberately not tested through this surface.
    """

    ROWS: ClassVar[dict[str, list[tuple[str, str]]]] = {f"r{i}": [("yes", "yes" if i else "no")] for i in range(4)}

    def _reasons(self, **kwargs) -> list[str]:
        reasons: list[str] = []
        noise_floor_mde(**{"criterion_index": 0, "n_resamples": FAST_RESAMPLES, "reasons": reasons, **kwargs})
        return reasons

    def test_too_few_invocations(self, tmp_path: Path) -> None:
        dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=1)
        assert "at least 2 invocations" in " ".join(
            self._reasons(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE)
        )

    def test_a_contaminated_tree(self, tmp_path: Path) -> None:
        dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        write_row(dirs[-1], "incumbent", "stale", eval_result("stale", [("yes", "yes")]), record=False)
        reasons = self._reasons(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE)
        assert "no recorded invocation wrote" in " ".join(reasons)

    def test_a_wrong_path(self, tmp_path: Path) -> None:
        dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        reasons = self._reasons(run_dirs=dirs, variant_id="typo", suite_id=SUITE)
        assert "wrong variant id" in " ".join(reasons)

    def test_a_cross_split_pair(self, tmp_path: Path) -> None:
        dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        for run_dir, split in zip(dirs, ("train", "test"), strict=True):
            payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            payload["row_selection"] = {"split": split}
            (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        assert "DIFFERENT row selections" in " ".join(
            self._reasons(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE)
        )

    def test_too_few_rows_scored_in_both_halves(self, tmp_path: Path) -> None:
        dirs = write_arm(tmp_path, "incumbent", {"only": [("yes", "yes")]}, invocations=2)
        assert "in BOTH halves of the invocation split" in " ".join(
            self._reasons(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE)
        )

    def test_the_gate_renders_the_threaded_cause_not_the_hardcoded_one(self, tmp_path: Path) -> None:
        """The reproduction: TWO invocations, so the old sentence was simply false.

        One row scores in both halves, so the floor declines on the row count — and the block used
        to blame the invocation count in front of a reader who could see there were two.
        """
        incumbent = {"only": [("yes", "yes")], "other": [("yes", "no")]}
        candidate = {"only": [("yes", "yes")], "other": [("yes", "yes")]}
        run_dirs = shared_dirs(tmp_path, incumbent, candidate, invocations=2)
        # Strip one row from the incumbent's second invocation so only one row scores in BOTH.
        shutil.rmtree(run_dirs[1] / "incumbent" / SUITE / "other")
        verdict = activation_verdict(run_dirs)
        assert verdict.mde is None
        note = next(n for n in verdict.notes if "minimum detectable effect could not be computed" in n)
        assert "in BOTH halves of the invocation split" in note
        assert "at least two invocations" not in note, note
        assert len(run_dirs) == 2, "the old sentence was false in front of a reader who could count"

    def test_the_reasons_keyword_is_additive(self, tmp_path: Path) -> None:
        """Every existing caller keeps working untouched — the whole point of a sink."""
        dirs = write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        without = noise_floor_mde(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0)
        collected: list[str] = []
        with_sink = noise_floor_mde(
            run_dirs=dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0, reasons=collected
        )
        assert without == with_sink
        assert collected == [], "a floor that WAS measured records no reason"


class TestRefusalMessage:
    """`_refusal_message` called directly, one test per branch of a ~60-line message builder."""

    def _verdict(
        self,
        *,
        p_floor: float | None,
        n_discordant: int | None = 3,
        rows_paired: int = 6,
        n_resamples: int = GATE_RESAMPLES,
    ) -> ActivationGateVerdict:
        # Literal keywords, not a splat: `extra="forbid"` plus CE041's static half is the two-sided
        # rule this repo settled on, and a helper that splatted would be the shape it forbids.
        return ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=n_resamples,
            rows_paired=rows_paired,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.01,
            p_floor=p_floor,
            n_discordant=n_discordant,
        )

    def test_none_when_the_floor_is_below_the_threshold(self) -> None:
        assert _refusal_message(self._verdict(p_floor=0.001), threshold=0.025, family_size=2, alpha=0.05) is None

    def test_none_when_there_is_no_floor_at_all(self) -> None:
        # `None`, never `""` — `gate_refusal` is `str | None` and the render branches on `is not None`.
        assert _refusal_message(self._verdict(p_floor=None), threshold=0.025, family_size=2, alpha=0.05) is None

    def test_a_floor_of_one_is_the_zero_discordant_case_with_its_own_message(self) -> None:
        message = _refusal_message(self._verdict(p_floor=1.0), threshold=0.025, family_size=2, alpha=0.05)
        assert message is not None
        assert "identical labels on every one of the 6" in message
        assert "adding more rows LIKE THESE cannot change it" in message
        assert "survivor(s)" not in message, "the family lever is meaningless when nothing differs"

    def test_a_finite_floor_names_both_levers_and_the_required_discordant_count(self) -> None:
        message = _refusal_message(
            self._verdict(p_floor=0.03125, n_discordant=3, n_resamples=2000), threshold=0.025, family_size=2, alpha=0.05
        )
        assert message is not None
        assert "Gate at most 1 survivor(s)" in message
        assert "DISAGREE on from 3 to 4" in message
        assert "adding rows they agree on makes this floor worse" in message

    def test_a_verdict_with_no_discordant_count_gets_the_family_lever_alone(self) -> None:
        # Never a sentence about a count the verdict does not carry.
        message = _refusal_message(
            self._verdict(p_floor=0.03125, n_discordant=None), threshold=0.025, family_size=2, alpha=0.05
        )
        assert message is not None
        assert "Gate at most 1 survivor(s) at alpha=0.05." in message
        assert "DISAGREE on" not in message

    def test_an_unreachable_bar_says_rows_are_irrelevant_rather_than_insufficient(self) -> None:
        # `min_discordant_rows` returns None when even every row discordant leaves the estimator's
        # own floor above the bar — a draw-count fact, so "more rows" would be actively wrong.
        message = _refusal_message(
            self._verdict(p_floor=0.5, n_discordant=1, rows_paired=4, n_resamples=10),
            threshold=0.0001,
            family_size=5,
            alpha=0.05,
        )
        assert message is not None
        assert "no discordant count clears this bar" in message
        assert "a larger n_resamples or a smaller family — not rows" in message

    def test_a_non_finite_floor_takes_the_no_refusal_path_rather_than_raising(self) -> None:
        """The one place the early return is not a plain De Morgan of the guard it replaced.

        Every comparison against NaN is False, so a NaN floor refused nothing before; under a
        `p_floor <= threshold` spelling it falls THROUGH to `math.floor(alpha / nan)` and raises
        out of the skill's inline snippet.

        Built with `model_construct`, which is the honest framing: pydantic's validator rejects a
        non-finite float, so this cannot arrive through a validated verdict and the guard is
        defence in depth rather than a live path. It is kept because it is the ORIGINAL spelling's
        semantics — preserving them costs one `not` — and because `_refusal_message` is a
        standalone function now, reachable by a caller that builds a verdict without validating it.
        """
        verdict = ActivationGateVerdict.model_construct(rows_paired=6, p_floor=float("nan"), n_discordant=3)
        assert _refusal_message(verdict, threshold=0.025, family_size=2, alpha=0.05) is None

    def test_no_workable_family_size_says_so(self) -> None:
        message = _refusal_message(
            self._verdict(p_floor=0.9, n_discordant=1), threshold=0.025, family_size=2, alpha=0.05
        )
        assert message is not None
        assert "No family size works at alpha=0.05, not even a family of one" in message


class TestDeriveSiblingIndices:
    """The guardrail stops being opt-in: `None` derives, `()` is now how you opt OUT."""

    def test_skips_a_non_classification_criterion_between_two_classification_ones(self, tmp_path: Path) -> None:
        # Positions are ABSOLUTE. A "count the classification criteria" implementation returns [1]
        # here, which is the file_check — the exact case this test exists for.
        result = eval_result("r1", [("yes", "yes")])
        basic = CriterionResult(criterion_type="file_check", description="f", score=1.0)
        sibling = ClassificationCriterionResult(
            criterion_type="skill_triggered",
            description="sibling",
            score=1.0,
            expected_label="yes",
            observed_label="yes",
        )
        results = [*result.success_criteria_results, basic, sibling]
        stacked = result.model_copy(update={"success_criteria_results": results})
        rows = {"r1": [stacked]}
        assert derive_sibling_indices(rows, primary_index=0) == [2]
        assert derive_sibling_indices(rows, primary_index=2) == [0]

    def test_a_single_criterion_suite_derives_nothing(self) -> None:
        rows = {"r1": [eval_result("r1", [("yes", "yes")])]}
        assert derive_sibling_indices(rows, primary_index=0) == []

    def test_unions_both_arms_rather_than_letting_one_shadow_the_other(self) -> None:
        # `{**incumbent, **candidate}` would drop the incumbent's list for every shared row id —
        # which is every row in the common case — and derive from the candidate alone.
        incumbent = {"r1": [eval_result("r1", [("yes", "yes"), ("no", "no")])]}
        candidate = {"r1": [eval_result("r1", [("yes", "yes")])]}
        assert derive_sibling_indices(incumbent, candidate, primary_index=0) == [1]
        assert derive_sibling_indices(candidate, incumbent, primary_index=0) == [1]

    def test_a_row_with_no_results_contributes_nothing_rather_than_truncating(self) -> None:
        errored = eval_result("r2", []).model_copy(update={"success_criteria_results": []})
        rows = {"r1": [eval_result("r1", [("yes", "yes"), ("no", "no")])], "r2": [errored]}
        assert derive_sibling_indices(rows, primary_index=0) == [1]

    def test_a_primary_past_the_end_does_not_raise(self) -> None:
        rows = {"r1": [eval_result("r1", [("yes", "yes"), ("no", "no")])]}
        assert derive_sibling_indices(rows, primary_index=9) == [0, 1]


class TestSiblingIndicesDefault:
    """`None` derives, `()` checks nothing, an explicit sequence checks exactly those."""

    @staticmethod
    def _stacked(tmp_path: Path) -> list[Path]:
        # Two classification criteria per row: the primary at 0, a sibling at 1 the candidate
        # annexes on half of the sibling's true rows.
        incumbent = {f"r{i}": [("yes", "yes"), ("yes", "yes")] for i in range(4)}
        candidate = {f"r{i}": [("yes", "yes"), ("yes", "no" if i < 2 else "yes")] for i in range(4)}
        return shared_dirs(tmp_path, incumbent, candidate)

    def test_the_default_derives_the_same_list_as_passing_it_explicitly(self, tmp_path: Path) -> None:
        run_dirs = self._stacked(tmp_path)
        derived = activation_verdict(run_dirs)
        explicit = activation_verdict(run_dirs, sibling_indices=[1])
        assert [c.name for c in derived.sibling_checks] == [c.name for c in explicit.sibling_checks]
        assert derived.sibling_checks and "criterion 1" in derived.sibling_checks[0].name

    def test_an_empty_tuple_still_checks_nothing(self, tmp_path: Path) -> None:
        assert activation_verdict(self._stacked(tmp_path), sibling_indices=()).sibling_checks == []

    def test_a_single_criterion_suite_is_silent(self, tmp_path: Path) -> None:
        incumbent, candidate = tiny_suite(4, 4)
        assert activation_verdict(shared_dirs(tmp_path, incumbent, candidate)).sibling_checks == []


class TestAnnexationRate:
    def test_reports_the_fraction_the_candidate_alone_lost(self, tmp_path: Path) -> None:
        run_dirs = TestSiblingIndicesDefault._stacked(tmp_path)
        check = activation_verdict(run_dirs).sibling_checks[0]
        # 4 true-yes sibling rows; the candidate turned 2 of them to "no" and the incumbent none.
        assert check.rate == pytest.approx(0.5)
        assert not check.passed  # the RECALL drop is what fails it, not the rate

    def test_an_equal_arm_reports_zero_and_passes(self, tmp_path: Path) -> None:
        rows = {f"r{i}": [("yes", "yes"), ("yes", "yes")] for i in range(4)}
        check = activation_verdict(shared_dirs(tmp_path, rows, dict(rows))).sibling_checks[0]
        assert check.rate == 0.0
        assert check.passed

    def test_a_sibling_with_no_true_instances_reports_none(self, tmp_path: Path) -> None:
        rows = {f"r{i}": [("yes", "yes"), ("no", "no")] for i in range(4)}
        check = activation_verdict(shared_dirs(tmp_path, rows, dict(rows))).sibling_checks[0]
        assert check.rate is None
        assert check.passed
        assert check.note is not None and "nothing to regress" in check.note

    def test_the_rate_changes_no_pass_fail_outcome(self, tmp_path: Path) -> None:
        # A non-zero annexation the INCUMBENT more than offsets: recall does not drop, so the
        # check passes while the rate is non-zero. The rate is a reading, never a second gate.
        incumbent = {f"r{i}": [("yes", "yes"), ("yes", "yes" if i == 0 else "no")] for i in range(4)}
        candidate = {f"r{i}": [("yes", "yes"), ("yes", "no" if i == 0 else "yes")] for i in range(4)}
        check = activation_verdict(shared_dirs(tmp_path, incumbent, candidate)).sibling_checks[0]
        assert check.rate == pytest.approx(0.25)
        assert check.passed

    def test_render_prints_the_rate_only_when_there_is_one(self, tmp_path: Path) -> None:
        with_rate = activation_verdict(TestSiblingIndicesDefault._stacked(tmp_path)).sibling_checks[0]
        assert f"rate {with_rate.rate:.3f}" in "\n".join(_render_checks("Sibling checks", [with_rate]))

        no_rate = GuardrailCheck(
            name="cost (USD/row)", incumbent=1.0, candidate=1.0, relative_change=0.0, tolerance=0.25, passed=True
        )
        assert "rate" not in "\n".join(_render_checks("Guardrails", [no_rate]))


class TestTheSiblingCheckIsBalancedLikeThePrimaryOne:
    """A replicate imbalance must not move a sibling's recall — the check gates `promoted`."""

    @staticmethod
    def _rows(replicates: int, *, annexed: bool = False) -> dict[str, list[EvaluationResult]]:
        # Criterion 0 is the primary; criterion 1 is the sibling, true-yes on both rows.
        sibling = ("yes", "no") if annexed else ("yes", "yes")
        return {
            "r1": [eval_result("r1", [("yes", "yes"), sibling])] * replicates,
            "r2": [eval_result("r2", [("yes", "yes"), ("yes", "yes")])] * replicates,
        }

    def test_identical_labels_read_identically_despite_an_extra_replicate(self) -> None:
        # Before balancing: recall 0.5 vs 0.6 on byte-identical labels, purely from one row's
        # extra replicate — and the sibling check is folded into `promoted`.
        check = _sibling_checks(
            incumbent_rows=self._rows(2),
            candidate_rows={**self._rows(2), "r1": self._rows(3)["r1"]},
            paired_row_ids=["r1", "r2"],
            sibling_indices=[1],
        )[0]
        assert check.incumbent == check.candidate
        assert check.passed

    def test_the_annexation_rate_is_aligned_within_a_row_not_across_the_flattened_list(self) -> None:
        # An unbalanced row used to shift every later row's alignment, so a candidate that annexed
        # half the sibling's true rows rendered `rate` 0.000 — "took nothing".
        incumbent = {
            "r1": [eval_result("r1", [("yes", "yes"), ("yes", "yes")])] * 3,
            "r2": [eval_result("r2", [("yes", "yes"), ("yes", "yes")])] * 2,
        }
        candidate = {
            "r1": [eval_result("r1", [("yes", "yes"), ("yes", "yes")])] * 2,
            "r2": [eval_result("r2", [("yes", "yes"), ("yes", "no")])] * 2,
        }
        check = _sibling_checks(
            incumbent_rows=incumbent, candidate_rows=candidate, paired_row_ids=["r1", "r2"], sibling_indices=[1]
        )[0]
        # 4 balanced true-yes observations; the candidate turned r2's 2 into "no".
        assert check.rate == pytest.approx(0.5)
        assert not check.passed

    def test_a_one_sided_sibling_is_still_detected_after_balancing(self) -> None:
        # Balancing trims a one-sided row to nothing, so PRESENCE must be read from the untrimmed
        # pools or the "results on one arm only" note disappears.
        incumbent = {"r1": [eval_result("r1", [("yes", "yes"), ("yes", "yes")])]}
        candidate = {"r1": [eval_result("r1", [("yes", "yes")])]}
        check = _sibling_checks(
            incumbent_rows=incumbent, candidate_rows=candidate, paired_row_ids=["r1"], sibling_indices=[1]
        )[0]
        assert check.note is not None and "one arm only" in check.note
        assert check.passed and check.rate is None


class TestConfirmGateActivation:
    """The activation twin. Same four outcomes on `f1.yes`, one shared classifier, no cross-track import."""

    def _confirm(self, tmp_path: Path, *, split: str | None = "test", train: ActivationGateVerdict | None = None):
        inc, cand = confirm_activation_arms(tmp_path / "confirm", split=split, incumbent_hits=2, candidate_hits=5)
        if train is None:
            t_inc, t_cand = confirm_activation_arms(
                tmp_path / "train", split="train", incumbent_hits=2, candidate_hits=5
            )
            train = holm_promote([activation_verdict_over_arms(t_inc, t_cand)])[0]
        return confirm_gate(
            train_verdict=train,
            incumbent_run_dirs=inc,
            candidate_run_dirs=cand,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=FAST_RESAMPLES,
        )

    def test_it_produces_a_confirm_verdict_on_f1(self, tmp_path: Path) -> None:
        confirm = self._confirm(tmp_path)
        assert isinstance(confirm, ConfirmVerdict)
        assert confirm.outcome in {"reproduced", "shrank", "reversed", "undecided"}
        # The effect is the F1 difference this track gates on, read off each verdict rather than
        # recomputed — the same rule the execution twin follows.
        assert confirm.train_effect is not None
        assert confirm.test_effect == confirm.test_verdict.mean_diff

    def test_a_confirm_run_recorded_under_split_train_is_refused(self, tmp_path: Path) -> None:
        confirm = self._confirm(tmp_path, split="train")
        assert confirm.confirm_refusal is not None
        assert "--split 'train'" in confirm.confirm_refusal and confirm.outcome == "undecided"

    def test_a_cross_split_confirm_pair_is_refused_naming_both_splits(self, tmp_path: Path) -> None:
        """This track pools several dirs per arm, so its confirm can be internally inconsistent.

        `activation_gate` refuses it underneath too, but a Stage C block reporting UNDECIDED with no
        reason would send the reader to the gate's notes to find out why.
        """
        inc, cand = confirm_activation_arms(tmp_path / "c", split="test", incumbent_hits=2, candidate_hits=5)
        for d in cand:
            set_split(d, "train")
        t_inc, t_cand = confirm_activation_arms(tmp_path / "t", split="train", incumbent_hits=2, candidate_hits=5)
        train = holm_promote([activation_verdict_over_arms(t_inc, t_cand)])[0]
        confirm = confirm_gate(
            train_verdict=train,
            incumbent_run_dirs=inc,
            candidate_run_dirs=cand,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=FAST_RESAMPLES,
        )
        assert confirm.confirm_refusal is not None
        assert "'train'" in confirm.confirm_refusal and "'test'" in confirm.confirm_refusal

    def test_naming_more_than_one_candidate_raises(self, tmp_path: Path) -> None:
        inc, cand = confirm_activation_arms(tmp_path, split="test", incumbent_hits=2, candidate_hits=5)
        with pytest.raises(TypeError, match="ONE variant id"):
            confirm_gate(
                train_verdict=full_activation_verdict(),
                incumbent_run_dirs=inc,
                candidate_run_dirs=cand,
                incumbent_variant="incumbent",
                candidate_variant=["candidate", "cand-b"],  # type: ignore[arg-type]
                suite_id=SUITE,
                criterion_index=0,
            )

    def test_a_refused_train_verdict_is_undecided(self, tmp_path: Path) -> None:
        refused = full_activation_verdict()  # carries `gate_refusal="refused"`
        assert refused.gate_refusal is not None
        confirm = self._confirm(tmp_path, train=refused)
        assert confirm.outcome == "undecided"
        assert confirm.confirm_refusal is not None and "TRAIN verdict is not a result" in confirm.confirm_refusal

    def test_the_carried_block_is_decided(self, tmp_path: Path) -> None:
        assert self._confirm(tmp_path).test_verdict.promoted is not None

    def test_it_measures_no_floor_of_its_own(self, tmp_path: Path) -> None:
        source = module_source("optimize.activation")
        body = source[source.index("def confirm_gate(") : source.index("def holm_promote(")]
        assert "measure_noise_floor" not in body
        # ONE verdict's two fields, not one field from each of two independently gated runs.
        confirm = self._confirm(tmp_path)
        assert confirm.test_mde == confirm.test_verdict.mde


class TestSeedStability:
    """Does the gate's decision survive a change of bootstrap seed? A READING, never a verdict."""

    @staticmethod
    def _kwargs(run_dirs) -> dict:
        return {
            "incumbent_run_dirs": run_dirs,
            "candidate_run_dirs": run_dirs,
            "incumbent_variant": "incumbent",
            "candidate_variant": "candidate",
            "suite_id": SUITE,
            "criterion_index": 0,
            "n_resamples": FAST_RESAMPLES,
        }

    def test_agreeing_seeds_read_unanimous(self, tmp_path: Path) -> None:
        # A wide, unambiguous win: no seed can move it.
        incumbent = {f"r{i}": [("yes", "no")] for i in range(8)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(8)}
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        stability = gate_seed_stability(**self._kwargs(run_dirs))

        assert stability.seeds == (0, 1, 2)
        assert stability.promote_agreement == 3 and stability.unanimous is True
        assert "STABLE" in render_seed_stability(stability)
        assert "coin flip" not in render_seed_stability(stability)

    def test_seeds_that_all_decline_are_also_unanimous(self, tmp_path: Path) -> None:
        # Unanimity is about AGREEMENT, not about promotion — 0/3 is as stable an answer as 3/3. The
        # candidate LOSES here, which no seed can turn into a promotion.
        incumbent = {f"r{i}": [("yes", "yes")] for i in range(8)}
        candidate = {f"r{i}": [("yes", "no" if i < 4 else "yes")] for i in range(8)}
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        stability = gate_seed_stability(**self._kwargs(run_dirs))
        assert stability.promote_agreement == 0 and stability.unanimous is True
        assert "would promote at none of 3 seeds" in render_seed_stability(stability)

    def test_a_split_decision_renders_as_a_coin_flip_and_returns_no_single_verdict(self) -> None:
        """The whole reason this exists, asserted on the model rather than through a flaky fixture.

        Constructing the disagreement directly is deliberate: a fixture that happens to straddle the
        Holm threshold at these three seeds is exactly the kind of thing that drifts, and the
        behaviour under test is what the READING says when seeds disagree — not the arithmetic that
        produced the disagreement.
        """
        split = SeedStability(seeds=(0, 1, 2), promote_agreement=1, p_values=(0.02, 0.06, 0.07), p_spread=0.05)
        assert split.unanimous is False
        assert not hasattr(split, "promoted"), (
            "SeedStability must carry NO single `promoted` field — collapsing disagreeing seeds into "
            "one verdict is the thing it exists to prevent"
        )
        block = render_seed_stability(split)
        assert "UNSTABLE" in block and "coin flip, not a result" in block
        assert "Do not report the majority's verdict" in block
        # And the count's FAMILY SIZE is named: each seed decides alone, so "would promote at 2/3" is
        # not the round's answer when the round gated a shortfall — measured 3/3 for a candidate a
        # family of three rejects.
        assert "family of ONE" in block and "would promote" in block

    def test_the_block_states_that_it_costs_no_runs(self, tmp_path: Path) -> None:
        # Three bootstraps over rows already loaded. Unsaid, a reader assumes it triples the round.
        incumbent, candidate = tiny_suite(4, 4)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        block = render_seed_stability(gate_seed_stability(**self._kwargs(run_dirs)))
        assert "zero** extra agent runs" in block and "CPU only" in block

    def test_the_p_spread_is_none_below_two_measured_values(self, tmp_path: Path) -> None:
        """Driven through the real computation, because the obvious version cannot fail.

        Constructing a `SeedStability(p_spread=None)` and asserting `p_spread is None` asserts the value
        just passed in — measured: mutating the production guard from `>= 2` to `>= 1` left the whole
        class green. A single seed is the reachable input that exercises it: one measured p, and a
        spread over one value is `0.0`, which reads as "the seeds agreed" when only one of them
        answered.
        """
        incumbent, candidate = tiny_suite(4, 4)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        one = gate_seed_stability(seeds=(0,), **self._kwargs(run_dirs))

        assert one.seeds == (0,)
        assert one.p_values[0] is not None, "fixture drifted — the single seed must produce a p"
        assert one.p_spread is None, "a spread over ONE measured p is not a spread"
        assert "spread (max - min over the measured ones): —" in render_seed_stability(one)
        # Two seeds over the same rows DO produce a spread (0.0 here, since the p is stable) — so the
        # `None` above is about the COUNT of measured values, not about them happening to agree.
        two = gate_seed_stability(seeds=(0, 1), **self._kwargs(run_dirs))
        assert two.p_spread is not None

    def test_the_gate_itself_is_unchanged_by_the_reading(self, tmp_path: Path) -> None:
        """`activation_gate` gains no parameter and no cost, which is why this is a separate function.

        A `seeds=` argument on the gate would have changed the rendered output of every existing call
        site and every pinned fixture. Asserted on the SIGNATURE, so adding one fails here.
        """

        assert "seeds" not in inspect.signature(activation_gate).parameters
        # And the reading's first seed reproduces the gate's own default exactly.
        incumbent, candidate = tiny_suite(4, 4)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        alone = holm_promote([activation_verdict(run_dirs)])[0]
        stability = gate_seed_stability(**self._kwargs(run_dirs))
        assert stability.p_values[0] == alone.p_value

    def test_passing_seed_instead_of_seeds_raises(self, tmp_path: Path) -> None:
        # The seed is the axis being varied, so a caller pinning it has misread the function.
        incumbent, candidate = tiny_suite(4, 4)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        with pytest.raises(TypeError, match="pass `seeds=`"):
            gate_seed_stability(seed=7, **self._kwargs(run_dirs))

    def test_an_empty_seed_list_raises(self, tmp_path: Path) -> None:
        incumbent, candidate = tiny_suite(4, 4)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        with pytest.raises(ValueError, match="at least one seed"):
            gate_seed_stability(seeds=(), **self._kwargs(run_dirs))

    def test_it_is_not_exported_from_the_models_package(self) -> None:
        # Computed and rendered, never persisted — `RuleCeiling`'s category, not the verdicts'.
        import coder_eval.models as models

        assert not hasattr(models, "SeedStability")
        assert SeedStability.__module__ == "coder_eval.optimize.activation"


class TestConfirmGateActivationOutcomes:
    """All three DECIDED outcomes reached through `optimize.activation.confirm_gate` itself.

    Asserting `outcome in {the four values}` proves nothing — the field is a `Literal` of exactly
    those — and the refusal tests next door only ever reach `undecided`. So REPRODUCED, SHRANK and
    REVERSED are pinned on this track too, not only on the execution one and in the pure classifier.

    **The arms have to be FLAKY across invocations**, and that is the whole difficulty: this track's
    floor is a null split over INVOCATIONS, so two identical invocations give an `mde` of exactly
    0.000 and every confirm over them is UNDECIDED — correctly, and it is what the simpler fixtures
    next door produce. One row flipping between the two invocations is what prices the floor.
    """

    @staticmethod
    def _flaky(hits: int) -> list[dict[str, tuple[str, str]]]:
        """Two invocations over 6 positive rows; `hits` of them score, and `r0` flips between them."""
        first = {f"r{i}": ("yes", "yes" if i < hits else "no") for i in range(6)}
        second = {row: (("yes", "no") if row == "r0" else pair) for row, pair in first.items()}
        return [first, second]

    @classmethod
    def _arm(cls, base: Path, variant: str, hits: int, prefix: str) -> list[Path]:
        dirs: list[Path] = []
        for index, labels in enumerate(cls._flaky(hits)):
            run_dir = base / f"{prefix}run-{index}"
            for row, pair in labels.items():
                write_row(run_dir, variant, row, eval_result(row, [pair]))
            dirs.append(run_dir)
        return dirs

    @classmethod
    def _pair(cls, base: Path, *, incumbent_hits: int, candidate_hits: int, split: str) -> tuple:
        inc = cls._arm(base, "incumbent", incumbent_hits, "inc-")
        cand = cls._arm(base, "candidate", candidate_hits, "cand-")
        for run_dir in (*inc, *cand):
            set_split(run_dir, split)
        return inc, cand

    @classmethod
    def _gate(cls, base: Path, *, incumbent_hits: int, candidate_hits: int, split: str):
        inc, cand = cls._pair(base, incumbent_hits=incumbent_hits, candidate_hits=candidate_hits, split=split)
        return holm_promote([activation_verdict_over_arms(inc, cand)])[0], inc, cand

    @classmethod
    def _confirm(cls, tmp_path: Path, *, train: tuple[int, int], test: tuple[int, int]) -> ConfirmVerdict:
        train_verdict, _i, _c = cls._gate(
            tmp_path / "train", incumbent_hits=train[0], candidate_hits=train[1], split="train"
        )
        _v, inc, cand = cls._gate(tmp_path / "test", incumbent_hits=test[0], candidate_hits=test[1], split="test")
        return confirm_gate(
            train_verdict=train_verdict,
            incumbent_run_dirs=inc,
            candidate_run_dirs=cand,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=FAST_RESAMPLES,
        )

    def test_the_same_effect_reproduces(self, tmp_path: Path) -> None:
        confirm = self._confirm(tmp_path, train=(2, 6), test=(2, 6))
        assert confirm.confirm_refusal is None
        assert confirm.outcome == "reproduced"

    def test_a_much_smaller_effect_shrank(self, tmp_path: Path) -> None:
        # Train +0.803 against test +0.368 with a confirm floor of 0.257: the shortfall exceeds it.
        confirm = self._confirm(tmp_path, train=(1, 6), test=(3, 6))
        assert confirm.confirm_refusal is None
        assert confirm.outcome == "shrank"

    def test_a_sign_flip_is_reversed(self, tmp_path: Path) -> None:
        confirm = self._confirm(tmp_path, train=(2, 6), test=(6, 2))
        assert confirm.confirm_refusal is None
        assert confirm.outcome == "reversed"
        assert confirm.test_effect is not None and confirm.test_effect < 0.0

    def test_the_confirm_floor_is_priced_on_these_fixtures(self, tmp_path: Path) -> None:
        # The precondition all three depend on: with identical invocations the floor is 0.000 and
        # every outcome above collapses to UNDECIDED. Asserted so a drifted fixture says so.
        confirm = self._confirm(tmp_path, train=(2, 6), test=(2, 6))
        assert confirm.test_mde is not None and confirm.test_mde > FLOOR_RESOLUTION

    def test_the_effect_is_f1_not_weighted_score(self, tmp_path: Path) -> None:
        # This track gates on `f1.yes`, so the delta it classifies is an F1 difference — read off the
        # verdicts rather than recomputed, exactly as the execution twin reads `weighted_score`.
        confirm = self._confirm(tmp_path, train=(2, 6), test=(2, 6))
        assert confirm.test_effect == confirm.test_verdict.mean_diff
        assert confirm.test_verdict.incumbent_f1 is not None and confirm.test_verdict.candidate_f1 is not None


class TestCrossSplitRefusal:
    """A train arm against a test arm is not a weak comparison — it is not a comparison."""

    def test_a_train_vs_test_pair_is_refused_with_both_splits_named(self, tmp_path: Path) -> None:
        verdict = activation_verdict_over_arms(*split_labelled_arms(tmp_path, "train", "test"))
        assert verdict.gate_refusal is not None
        assert "'train'" in verdict.gate_refusal and "'test'" in verdict.gate_refusal
        # No statistic is reported: there was nothing to compute one over.
        assert verdict.p_value is None
        assert verdict.mean_diff is None and verdict.ci_low is None and verdict.ci_high is None
        assert verdict.incumbent_f1 is None and verdict.candidate_f1 is None
        # Left for holm_promote, exactly like every other activation verdict.
        assert verdict.promoted is None
        # It still says what it LOADED, so the reader can see the arms were otherwise fine.
        assert verdict.rows_paired > 0

    def test_a_recorded_null_split_against_a_named_one_is_also_refused(self, tmp_path: Path) -> None:
        """A full-suite run and a --split run scored different row sets just as surely."""
        verdict = activation_verdict_over_arms(*split_labelled_arms(tmp_path, None, "test"))
        assert verdict.gate_refusal is not None and verdict.p_value is None

    def test_matching_splits_produce_no_refusal_and_no_note(self, tmp_path: Path) -> None:
        """Silence is the correct output for a correctly wired gate."""
        verdict = activation_verdict_over_arms(*split_labelled_arms(tmp_path, "train", "train"))
        assert verdict.gate_refusal is None
        assert not any("provenance" in note for note in verdict.notes)

    def test_one_unrecorded_arm_notes_but_does_not_refuse(self, tmp_path: Path) -> None:
        """The recorded arm proves nothing about the other, so a note — not a refusal."""
        inc, cand = split_labelled_arms(tmp_path, "train", "train")
        (cand[0] / "run.json").unlink()
        verdict = activation_verdict_over_arms(inc, cand)
        assert verdict.gate_refusal is None
        assert verdict.p_value is not None, "an unrecorded arm must stay gatable"
        assert any("provenance is missing from 1 of 4" in note for note in verdict.notes)

    def test_a_within_arm_mismatch_is_caught_too(self, tmp_path: Path) -> None:
        """Stage B runs one arm three times; those three can disagree with each other."""
        inc, cand = split_labelled_arms(tmp_path, "train", "train")
        set_split(inc[0], "test")
        verdict = activation_verdict_over_arms(inc, cand)
        assert verdict.gate_refusal is not None and verdict.p_value is None

    def test_the_refusal_blocks_a_promotion_that_would_otherwise_happen(self, tmp_path: Path) -> None:
        """The assertion that proves the preflight does something.

        The other tests here use a zero-discordant fixture that could never promote, so they would
        pass with the whole preflight deleted. This one uses the clear-win fixture — incumbent
        engages 3 of 12, candidate 12 of 12 — which promotes reliably on matching splits. The ONLY
        difference between the two halves is the recorded split.
        """
        labels_inc = {f"r{i}": [("yes", "yes" if i < 3 else "no")] for i in range(12)}
        labels_cand = {f"r{i}": [("yes", "yes")] for i in range(12)}

        def _pair(root: Path, inc_split: str | None, cand_split: str | None):
            inc = write_arm(root, "incumbent", labels_inc, invocations=2, prefix="inc-")
            cand = write_arm(root, "candidate", labels_cand, invocations=2, prefix="cand-")
            for d in inc:
                set_split(d, inc_split)
            for d in cand:
                set_split(d, cand_split)
            return inc, cand

        (promoted,) = holm_promote([activation_verdict_over_arms(*_pair(tmp_path / "same", "train", "train"))])
        assert promoted.promoted is True, "the fixture must promote when the splits agree"

        (blocked,) = holm_promote([activation_verdict_over_arms(*_pair(tmp_path / "crossed", "train", "test"))])
        assert blocked.promoted is False
        assert blocked.gate_refusal is not None

    def test_holm_forces_not_promoted_and_keeps_the_refusal(self, tmp_path: Path) -> None:
        verdict = activation_verdict_over_arms(*split_labelled_arms(tmp_path, "train", "test"))
        (decided,) = holm_promote([verdict])
        assert decided.promoted is False
        assert decided.gate_refusal == verdict.gate_refusal
        # Outside the family by the p-based rule, so the "outside the family" note would be
        # redundant AND contradictory under a refusal headline.
        assert NOTE_OUTSIDE_FAMILY not in decided.notes

    def test_a_refused_verdict_does_not_shrink_a_siblings_holm_threshold(self, tmp_path: Path) -> None:
        """Family membership is `p_value is not None` and nothing else.

        Dropping a refused verdict from the family would shrink `m` and LOOSEN `alpha/m` for
        every sibling — the uncorrected-p degeneration, arrived at from the other side.
        """
        refused = activation_verdict_over_arms(*split_labelled_arms(tmp_path / "x", "train", "test"))
        incumbent, candidate = tiny_suite(positives=6, distractors=6)
        sibling = activation_verdict(shared_dirs(tmp_path / "y", incumbent, candidate))
        assert sibling.p_value is not None, "the sibling fixture must actually produce a p"

        alone = holm_promote([sibling])[0]
        with_refused = next(v for v in holm_promote([sibling, refused]) if v.p_value is not None)
        # Asserted on the RANK-DEPENDENT quantity, not on `holm_alpha`: that one is assigned
        # `alpha` unconditionally in both branches, so comparing it is 0.05 == 0.05 and passes
        # even with the family filter broken. `note_ordinary_negative` and `note_holm_family`
        # both spell the family SIZE, which is the number a dropped verdict would move.
        assert note_holm_family(1, DEFAULT_ALPHA) in alone.notes
        assert note_holm_family(1, DEFAULT_ALPHA) in with_refused.notes, (
            "the refused verdict was counted in the family — dropping it would shrink m and "
            "LOOSEN alpha/m for every sibling"
        )
        assert alone.promoted == with_refused.promoted


class TestNoiseFloorCarriesItsSplit:
    """The floor's split is DERIVED from the run dirs, never passed by a caller.

    A caller-supplied `split=` would default to `None` and reintroduce the cross-split bug on the
    first snippet that forgot it — unlike `model`, which a caller at least resolved and can see is
    unresolved.
    """

    @staticmethod
    def _dirs(tmp_path: Path, splits: list[str | None]) -> list[Path]:
        labels = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(6)}
        dirs = write_arm(tmp_path, "incumbent", labels, invocations=len(splits))
        for run_dir, split in zip(dirs, splits, strict=True):
            set_split(run_dir, split)
        return dirs

    def test_a_matching_split_is_stamped_on_the_floor(self, tmp_path: Path) -> None:
        floor = measure_noise_floor(
            run_dirs=self._dirs(tmp_path, ["train", "train"]),
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            model="claude-haiku-4-5",
            n_resamples=FAST_RESAMPLES,
        )
        assert floor is not None and floor.split == "train"

    def test_mismatched_splits_refuse_to_measure_and_log_why(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            floor = measure_noise_floor(
                run_dirs=self._dirs(tmp_path, ["train", "test"]),
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5",
                n_resamples=FAST_RESAMPLES,
            )
        assert floor is None, "a null split pooled across two row sets is not a floor"
        assert "DIFFERENT row selections" in caplog.text

    def test_an_unrecorded_dir_makes_the_floor_uncacheable(self, tmp_path: Path) -> None:
        dirs = self._dirs(tmp_path, ["train", "train"])
        (dirs[0] / "run.json").unlink()
        floor = measure_noise_floor(
            run_dirs=dirs,
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            model="claude-haiku-4-5",
            n_resamples=FAST_RESAMPLES,
        )
        # Still MEASURED — an unrecorded run dir stays usable — but carrying the sentinel, so
        # `record_noise_floor` refuses to write it and no lookup can ever match it.
        assert floor is not None and floor.split == UNRECORDED_SPLIT

    def test_the_execution_floor_carries_its_split_too(self, tmp_path: Path) -> None:
        dirs = weighted_arm(tmp_path, "incumbent", {f"r{i}": [0.2, 0.55, 0.9] for i in range(8)})
        set_split(dirs[0], "test")
        floor = execution_floor(dirs)
        assert floor is not None and floor.split == "test"
