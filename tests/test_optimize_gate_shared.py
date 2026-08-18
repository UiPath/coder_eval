"""Unit tests for `coder_eval.optimize.gate` — the half BOTH tracks share.

The Holm family and its one `holm_rejections` call site, the notes both wrappers emit verbatim, the
cost/latency guardrails, and Stage C's shared classifier. A claim about ONE track belongs in that
track's file; a claim that the two AGREE belongs in `test_optimize_layering.py`.
"""

import math
import random
from pathlib import Path

import pytest

from coder_eval.models import EvaluationResult
from coder_eval.optimize.activation import _holm_threshold
from coder_eval.optimize.gate import (
    GATE_MAX_FAMILY,
    GATE_P_PRECISION,
    GATE_RESAMPLES,
    MATERIALITY_FLOOR,
    classify_confirm,
    cost_latency_guardrails,
    note_resolution_degraded,
)
from coder_eval.reports_stats import DEFAULT_ALPHA
from tests.optimize_fixtures import (
    EXEC_SUITE,
    activation_verdict,
    cost_check,
    cost_rows,
    costed_result,
    eval_result,
    exec_gate,
    expected_resolution_note,
    experiment_json,
    shared_dirs,
    write_row,
)


def _duration_rows(per_row: dict[str, list[float]]) -> dict[str, list[EvaluationResult]]:
    return {
        rid: [costed_result(rid, [("yes", "yes")], cost=1.0, duration=d) for d in durations]
        for rid, durations in per_row.items()
    }


class TestCostLatencyGuardrails:
    def test_a_non_finite_cost_reports_unmeasured_rather_than_nan_arithmetic(self) -> None:
        """The end-to-end guard, not just the helper's unit contract.

        A corrupt `total_cost_usd` used to reach the relative-change arithmetic as a `nan`, where
        every comparison answers neither way while the check still reports a number. `row_cost_levels`
        drops such a row exactly as it drops an empty one, so the arm genuinely has no measurement
        and the existing "not evaluated" note is TRUE of it — which is what licenses the collapse in
        `reports_stats.median_or_none`. Asserted here because the claim is about the CALLERS; the
        helper's own tests cannot see whether their messages survive it.
        """
        incumbent = cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = cost_rows({f"r{i}": [float("nan")] for i in range(12)})

        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.candidate is None, "a corrupt arm must read as unmeasured, never as a nan level"
        assert check.relative_change is None
        assert check.passed is True, "an unevaluable guardrail may not veto — it has measured nothing"
        assert check.note is not None and "not evaluated" in check.note

    def test_one_corrupt_row_does_not_poison_the_rest_of_the_arm(self) -> None:
        """The row is dropped, not the arm: eleven clean rows still produce a real comparison.

        `check.passed is False` here is the DOUBLING being vetoed, and the interval is what says so
        — asserted explicitly, because a `nan` interval also produces `passed is False` (`nan <= x`
        is False) and the two are indistinguishable from the flag alone. That is not hypothetical:
        it is the defect the sibling test below was written for.
        """
        incumbent = cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = cost_rows({f"r{i}": [2.0] for i in range(11)} | {"r11": [float("inf")]})

        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.candidate == 2.0
        assert check.ci_low is not None and math.isfinite(check.ci_low), "the interval must be real"
        assert check.ci_low > 0.0, "the doubling is what vetoes — not an unusable interval"
        assert check.passed is False

    def test_a_corrupt_row_cannot_veto_two_otherwise_identical_arms(self) -> None:
        """The cross-phase defect: filtered levels, unfiltered clusters.

        `row_cost_levels` drops a non-finite row from the reported medians and from the incumbent
        MEAN, but the clusters handed to `cluster_bootstrap_diff_ci` are the raw ones — so one
        corrupt figure put a `nan` in `ci_low`, and `nan <= materiality * mean` is False, which is a
        VETO. The rendered block showed `incumbent 1.000 -> candidate 1.000`, a relative change of
        0.0, and no note: a promotion blocked with the page saying nothing changed.

        The medians, the mean and the interval must all see the SAME rows, which is what makes the
        "not evaluated" note true rather than merely printed.

        Reachable from a CALLER rather than from a run directory — pydantic serialises a non-finite
        `total_cost_usd` as `null`, so a row read from `task.json` arrives with no cost rather than a
        bad one. This function is public and takes the rows it is handed, which is the same reason
        `TestGuardrailsNeverRaiseOnACallerSuppliedRow` below exists.
        """
        incumbent = cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = cost_rows({f"r{i}": [1.0] for i in range(11)} | {"r11": [float("nan")]})

        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.incumbent == check.candidate == 1.0
        assert check.ci_low is not None and math.isfinite(check.ci_low)
        assert check.passed is True, "identical arms must not be vetoed by one unusable row"
        # And the drop is VISIBLE: a silently narrowed comparison is the thing not to ship.
        assert check.note is not None and "non-finite" in check.note

    def test_an_arm_whose_every_row_is_corrupt_reads_as_unmeasured(self) -> None:
        # The boundary: nothing usable left, so it takes the not-evaluated path rather than
        # comparing an empty sample.
        incumbent = cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = cost_rows({f"r{i}": [float("nan")] for i in range(12)})

        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.candidate is None
        assert check.passed is True
        assert check.note is not None and "not evaluated" in check.note

    def test_fails_on_a_large_consistent_increase(self) -> None:
        incumbent = cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = cost_rows({f"r{i}": [2.0] for i in range(12)})
        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.passed is False
        assert check.ci_low is not None and check.ci_low > MATERIALITY_FLOOR * 1.0

    def test_passes_on_a_noisy_wash(self) -> None:
        """The regression test for the measured false-positive mode.

        12 rows whose per-row costs are drawn from the SAME distribution (mean 1.0, CV 0.25), so the
        true difference is zero. This seed is chosen because the sample medians still land 19.3%
        apart (0.983 vs 1.173) purely by noise: a fixed 15% tolerance on the median VETOES this
        candidate, which is exactly the false positive the measured CVs predicted.

        The second assertion is what makes the test attributable, and it is the reason this seed was
        chosen over others that also clear 15%: with the materiality floor set to ZERO the check
        still passes, so it is the bootstrap interval — which contains zero — absorbing the noise,
        not the floor suppressing it. A seed where only the floor saves the candidate would pass
        this test while proving nothing about the redesign.
        """
        rng = random.Random(13)
        incumbent = cost_rows({f"r{i}": [max(0.05, rng.gauss(1.0, 0.25))] for i in range(12)})
        candidate = cost_rows({f"r{i}": [max(0.05, rng.gauss(1.0, 0.25))] for i in range(12)})

        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.relative_change is not None and check.relative_change > 0.15  # a fixed rule fires
        assert check.passed is True
        assert check.ci_low is not None and check.ci_low < 0.0  # the interval contains zero

        floorless = cost_check(
            cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate, materiality=0.0)
        )
        assert floorless.passed is True, "the interval must absorb this, not the materiality floor"

    def test_reports_the_interval_not_just_the_verdict(self) -> None:
        incumbent = cost_rows({f"r{i}": [1.0, 1.1] for i in range(8)})
        candidate = cost_rows({f"r{i}": [1.2, 1.3] for i in range(8)})
        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.ci_low is not None and check.ci_high is not None
        assert check.ci_low <= check.ci_high

    def test_materiality_floor_only_suppresses(self) -> None:
        # A statistically real but small increase passes; the SAME increase scaled past the floor
        # fails. Driven off MATERIALITY_FLOOR, never a hardcoded number.
        small = MATERIALITY_FLOOR / 2.0
        large = MATERIALITY_FLOOR * 2.0
        incumbent = cost_rows({f"r{i}": [1.0] for i in range(12)})
        assert cost_check(
            cost_latency_guardrails(
                incumbent_rows=incumbent, candidate_rows=cost_rows({f"r{i}": [1.0 + small] for i in range(12)})
            )
        ).passed
        assert not cost_check(
            cost_latency_guardrails(
                incumbent_rows=incumbent, candidate_rows=cost_rows({f"r{i}": [1.0 + large] for i in range(12)})
            )
        ).passed

    def test_with_no_recorded_cost_passes_with_a_note(self) -> None:
        incumbent = cost_rows({f"r{i}": [None] for i in range(6)})  # type: ignore[arg-type]
        candidate = cost_rows({f"r{i}": [None] for i in range(6)})  # type: ignore[arg-type]
        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.passed is True
        assert check.incumbent is None
        assert check.note is not None and "not evaluated" in check.note

    def test_a_zero_incumbent_does_not_divide(self) -> None:
        incumbent = cost_rows({f"r{i}": [0.0] for i in range(12)})
        candidate = cost_rows({f"r{i}": [0.5] for i in range(12)})
        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.relative_change is None
        assert check.passed is True
        assert check.note is not None and "the incumbent measured zero" in check.note

    def test_notes_an_asymmetric_measurement_count(self) -> None:
        incumbent = cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = cost_rows({f"r{i}": ([1.0] if i else [None]) for i in range(12)})  # type: ignore[arg-type]
        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.note is not None and "incumbent row(s) vs" in check.note

    def test_latency_is_guarded_too(self) -> None:
        incumbent = _duration_rows({f"r{i}": [10.0] for i in range(12)})
        candidate = _duration_rows({f"r{i}": [30.0] for i in range(12)})
        latency = next(
            c
            for c in cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate)
            if c.name.startswith("latency")
        )
        assert latency.passed is False


class TestGuardrailScaleAndHoles:
    """Two ways the guardrail produced a number that meant something else."""

    def test_a_uniform_increase_is_judged_against_the_same_statistic_it_measures(self) -> None:
        """The interval is on the difference of MEANS, so the floor must scale by the mean.

        Per-row cost is strongly right-skewed. Measured against a median-scaled floor, a uniform
        +10% on `[0.01]*11 + [1.00]*9` rendered `FAIL ... 0.010 -> 0.011` against a 25% floor — a
        line that contradicts itself, and a real win killed by a unit mismatch.
        """
        costs = [0.01] * 11 + [1.00] * 9
        incumbent = cost_rows({f"r{i}": [c] for i, c in enumerate(costs)})
        candidate = cost_rows({f"r{i}": [c * 1.10] for i, c in enumerate(costs)})
        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.passed is True, "a 10% increase must not breach a 25% floor"

    def test_a_genuinely_large_increase_still_fails_on_the_same_distribution(self) -> None:
        costs = [0.01] * 11 + [1.00] * 9
        incumbent = cost_rows({f"r{i}": [c] for i, c in enumerate(costs)})
        candidate = cost_rows({f"r{i}": [c * 2.0] for i, c in enumerate(costs)})
        assert cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate)).passed is False

    def test_a_row_measured_on_one_arm_only_cannot_fabricate_a_zero(self) -> None:
        """`mean([])` is 0.0, so an unfiltered empty cluster reads as "this arm cost nothing".

        Measured before the fix: incumbent $0.10/row, candidate $1.00 on half its rows and no cost
        recorded on the rest — a 10x increase PASSING with ci_low = -0.1, the incumbent's own mean
        negated by draws where the candidate contributed nothing.
        """
        incumbent = cost_rows({f"r{i}": [0.10] for i in range(4)})
        candidate = cost_rows({f"r{i}": ([1.00] if i < 2 else [None]) for i in range(4)})  # type: ignore[arg-type]
        check = cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.ci_low is not None and check.ci_low > 0.0, "the interval must not include the fabricated zero"
        assert check.passed is False, "a 10x cost increase must breach the floor"


class TestResolutionDegradedNote:
    """What resolution the gate ACTUALLY achieved, said at the one place the family size is known.

    `GATE_RESAMPLES` is derived from `GATE_P_PRECISION` at the strictest Holm threshold for
    `GATE_MAX_FAMILY` survivors. Above that S the threshold tightens while the draw count does not,
    so the Monte-Carlo error of the p stops being the declared fraction of the threshold it is
    compared against — and the block printed the family size while the declared precision sat on a
    constant nobody reads at that moment.

    Every figure below is RECOMPUTED from the shipped constants rather than typed in: a hardcoded
    table is a claim nobody checks the day one of them moves.
    """

    @pytest.mark.parametrize("family_size", [1, 3, GATE_MAX_FAMILY])
    def test_nothing_is_said_at_or_below_the_family_the_gate_is_sized_for(self, family_size: int) -> None:
        # The declared precision holds there, and a note saying so on every verdict is noise.
        assert note_resolution_degraded(family_size, GATE_RESAMPLES, DEFAULT_ALPHA) is None

    @pytest.mark.parametrize("family_size", [GATE_MAX_FAMILY + 1, 8, 10])
    def test_the_note_reports_the_threshold_the_precision_and_the_count_that_restores_it(
        self, family_size: int
    ) -> None:
        note = note_resolution_degraded(family_size, GATE_RESAMPLES, DEFAULT_ALPHA)
        assert note is not None
        threshold, achieved, needed = expected_resolution_note(family_size)
        assert f"alpha/{family_size} = {threshold:.5f}" in note
        assert f"{achieved:.4f}" in note
        assert f"n_resamples={needed}" in note
        assert f"{GATE_P_PRECISION:.2f} this gate declares" in note

    def test_it_is_not_a_refusal_and_says_so(self) -> None:
        note = note_resolution_degraded(8, GATE_RESAMPLES, DEFAULT_ALPHA)
        assert note is not None and "The decision above stands" in note

    def test_a_coarser_draw_count_reports_a_worse_precision(self) -> None:
        # Read off the verdict rather than from the constant: a caller may pass a custom count, and
        # the note has to describe the measurement that actually happened.
        coarse = note_resolution_degraded(8, 2_000, DEFAULT_ALPHA)
        fine = note_resolution_degraded(8, GATE_RESAMPLES, DEFAULT_ALPHA)
        assert coarse is not None and fine is not None and coarse != fine
        assert f"{expected_resolution_note(8, 2_000)[1]:.4f}" in coarse

    @pytest.mark.parametrize(("family_size", "n_resamples"), [(0, GATE_RESAMPLES), (8, 0)])
    def test_the_degenerate_inputs_are_guarded_rather_than_dividing(self, family_size: int, n_resamples: int) -> None:
        # Unreachable from either wrapper (no members means no notes loop), but the division is
        # guarded rather than left to raise out of a user's inline snippet.
        assert note_resolution_degraded(family_size, n_resamples, DEFAULT_ALPHA) is None


class TestHolmThreshold:
    def test_returns_alpha_over_s_for_the_smallest_and_alpha_for_the_largest(self) -> None:
        family = [0.001, 0.01, 0.04]
        assert _holm_threshold(family, 0.001, 0.05) == pytest.approx(0.05 / 3)
        assert _holm_threshold(family, 0.04, 0.05) == pytest.approx(0.05)

    def test_ties_take_the_strictest_rank(self) -> None:
        # sorted().index() returns the FIRST occurrence, so every tied verdict is decided against
        # alpha/S. Conservative in the refusal direction, which is the right way to be wrong here.
        assert _holm_threshold([0.01] * 4, 0.01, 0.05) == pytest.approx(0.05 / 4)


class TestClassifyConfirm:
    """The four answers, as a pure function of three floats — one rule, shared by both tracks.

    It lives in `optimize.gate` because both rank-2 track modules need it and neither may import the
    other, so per-track would mean two copies of promotion-relevant arithmetic. `TestTheClassifierIsShared`
    below asserts that placement, so a future copy in either module fails the build.
    """

    def test_the_signs_opposing_is_reversed(self) -> None:
        outcome, note = classify_confirm(0.08, -0.06, 0.02)
        assert outcome == "reversed"
        assert "+0.080" in note and "-0.060" in note

    def test_the_same_sign_within_the_margin_is_reproduced(self) -> None:
        outcome, _note = classify_confirm(0.08, 0.075, 0.02)
        assert outcome == "reproduced"

    def test_a_shortfall_beyond_the_margin_is_shrank(self) -> None:
        outcome, note = classify_confirm(0.08, 0.02, 0.01)
        assert outcome == "shrank" and "-0.060" in note

    def test_a_test_effect_of_exactly_zero_is_shrank_not_reproduced(self) -> None:
        # "Same sign" is undefined at zero, and calling no effect a reproduced one is the reading a
        # promotion would be built on.
        assert classify_confirm(0.08, 0.0, 0.02)[0] == "shrank"

    @pytest.mark.parametrize("mde", [None, 0.0, 2.7755575615628914e-17])
    def test_an_undefined_margin_is_undecided_never_shrank(self, mde: float | None) -> None:
        """`None`, `0.0` and floating-point RESIDUE all leave the comparison with no operand.

        Both pinned execution fixtures render `Minimum detectable effect: 0.000`, which
        `execution_gate` itself documents as "the floor was not checked" — so this is the common case,
        and with a margin of zero EVERY non-identical test effect would classify SHRANK.

        The third value is not hypothetical: it is what this repo's own winning fixture's null split
        returns, and an `== 0.0` test goes silently inert on it. `FLOOR_RESOLUTION` is the threshold
        the execution track already had to widen to for exactly that reason — it moved to rank 1 so
        this classifier could share it rather than the activation side declaring a second copy.
        """
        outcome, note = classify_confirm(0.08, 0.075, mde)
        assert outcome == "undecided"
        assert "could not be MEASURED" in note and "--repeats" in note

    @pytest.mark.parametrize(
        ("train", "test"), [(None, 0.05), (0.05, None), (None, None)], ids=["no-train", "no-test", "neither"]
    )
    def test_a_missing_effect_is_undecided(self, train: float | None, test: float | None) -> None:
        outcome, note = classify_confirm(train, test, 0.02)
        assert outcome == "undecided" and "not an effect of zero" in note

    @pytest.mark.parametrize(
        ("train", "test"),
        [(-0.08, -0.10), (-0.08, -0.02), (0.0, 0.06), (0.0, 0.0)],
        ids=["lost-harder", "lost-less", "appeared-from-nothing", "both-zero"],
    )
    def test_a_train_effect_that_is_not_a_win_is_undecided(self, train: float, test: float) -> None:
        """Stage C confirms a WIN, and the arithmetic was actively misleading without this guard.

        Measured on the version before it: `(-0.08, -0.10)` read REPRODUCED — a candidate that lost on
        train and lost harder on test — `(-0.08, -0.02)` read SHRANK, a LOSS reading like a diminished
        win, and `(0.0, +0.06)` read REPRODUCED when nothing was reproduced: an effect APPEARED where
        Stage B measured none. All four are the wrong headline on a block a promotion is read from.
        """
        outcome, note = classify_confirm(train, test, 0.02)
        assert outcome == "undecided"
        assert "not a win" in note and "Stage B WINNER" in note

    def test_a_zero_test_effect_gets_its_own_note_rather_than_the_shrank_arithmetic(self) -> None:
        # The SHRANK sentence quotes a shortfall against the margin. On the zero branch that sentence
        # was FALSE whenever the margin exceeded the train effect — it announced that a shortfall of
        # 0.020 exceeded a resolution of 0.050. The outcome is right; the reason had to be too.
        outcome, note = classify_confirm(0.02, 0.0, 0.05)
        assert outcome == "shrank"
        assert "measured exactly 0.000" in note
        assert "exceeds" not in note, "the zero branch must not quote a shortfall against the margin"

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_a_non_finite_value_is_undecided_rather_than_the_most_permissive_rung(self, bad: float) -> None:
        # A NaN compares False against every threshold, so without the guard it fell through to
        # REPRODUCED — the one rung a promotion is made on (rubric item 15).
        assert classify_confirm(bad, 0.05, 0.02)[0] == "undecided"
        assert classify_confirm(0.08, bad, 0.02)[0] == "undecided"
        assert classify_confirm(0.08, 0.05, bad)[0] == "undecided"

    def test_every_outcome_carries_a_non_empty_note(self) -> None:
        # A bare outcome word in a ledger read back weeks later cannot be reconstructed from.
        cases = [(0.08, -0.06, 0.02), (0.08, 0.075, 0.02), (0.08, 0.02, 0.01), (0.08, 0.075, None)]
        assert all(classify_confirm(*case)[1].strip() for case in cases)


def test_the_gate_notes_keep_their_order(tmp_path: Path) -> None:
    """Note ORDER is observable — `render_markdown` prints them in order and the pins compare bytes.

    A fixture that trips three of them at once, asserted as an ordered list of prefixes rather than
    a set, so the extraction cannot quietly reorder the ladder.

    THREE, not all four: the zero-row note fires only when an arm loaded nothing, and an arm that
    loaded nothing pairs no rows — so it cannot co-occur with the hollow and unbalanced notes,
    which need paired rows to exist. `TestLoadAndPair` covers that one on its own fixture.
    """
    incumbent = {"shared": [("yes", "yes")], "only-inc": [("yes", "yes")]}
    candidate = {"shared": [("yes", "yes")]}
    run_dirs = shared_dirs(tmp_path, incumbent, candidate)
    for run_dir in run_dirs:
        write_row(run_dir, "candidate", "hollow", eval_result("hollow", []))
        write_row(run_dir, "incumbent", "hollow", eval_result("hollow", [("yes", "yes")]))
    write_row(run_dirs[0], "candidate", "shared", eval_result("shared", [("yes", "yes")]), replicate=1)

    verdict = activation_verdict(run_dirs)
    prefixes = [
        "1 row(s) present in only one arm",
        "1 row(s) scored on only one arm",
        "1 row(s) had different replicate counts",
    ]
    matched = [
        next(p for p in prefixes if n.startswith(p)) for n in verdict.notes if any(n.startswith(p) for p in prefixes)
    ]
    assert matched == prefixes, f"note order changed: {matched}"


class TestGuardrailsNeverRaiseOnACallerSuppliedRow:
    def test_a_row_id_absent_from_the_arms_is_skipped_not_indexed(self) -> None:
        # `execution_gate` passes the rows `paired_comparison` paired, which come from
        # experiment.json — an id named there but missing on disk used to raise a KeyError out of
        # the skill's inline snippet, discarding the wrong-path note composed just above it.
        checks = cost_latency_guardrails(incumbent_rows={}, candidate_rows={}, row_ids=["ghost-row"])
        assert [c.passed for c in checks] == [True, True]
        assert all(c.note is not None and "not evaluated" in c.note for c in checks)

    def test_an_unfanned_single_task_suite_returns_a_noted_verdict(self, tmp_path: Path) -> None:
        # `scoped_scores` keeps `task_id == suite_id`, so `removeprefix` leaves the SUITE id as the
        # row id — which no row directory is named. The contract is a noted verdict, not a raise.
        run_dir = tmp_path / "round1-gate"
        experiment_json(
            run_dir,
            ["incumbent", "candidate"],
            {"incumbent": {EXEC_SUITE: [0.4, 0.5]}, "candidate": {EXEC_SUITE: [0.8, 0.9]}},
        )
        verdict = exec_gate(run_dir)
        # The row tree is what is missing, and that outranks the "fewer than 2 paired rows" the
        # unfanned scores also produce: an arm with no rows at all is the more specific fault.
        assert verdict.gate_refusal is not None and "loaded ZERO rows" in verdict.gate_refusal
        assert [c.passed for c in verdict.guardrails] == [True, True]
