"""Unit tests for replicate statistics helpers in reports_stats."""

import random

import pytest

from coder_eval.reports_stats import (
    BOOTSTRAP_RESAMPLES,
    bootstrap_mean_ci,
    bootstrap_p_floor,
    cluster_bootstrap_diff_ci,
    cohens_d,
    holm_rejections,
    median_or_none,
    paired_t_ci,
    paired_t_test,
    student_t_critical,
    wilson_interval,
)


class TestBootstrapMeanCi:
    def test_empty_returns_triple_zero(self):
        assert bootstrap_mean_ci([]) == (0.0, 0.0, 0.0)

    def test_single_value_returns_triple(self):
        assert bootstrap_mean_ci([0.7]) == (0.7, 0.7, 0.7)

    @pytest.mark.parametrize("confidence", [-1.0, 0.0, 1.0, 1.1])
    def test_confidence_outside_unit_interval_raises(self, confidence):
        """Out-of-domain confidence must refuse, not return a wrong-width interval."""
        with pytest.raises(ValueError, match="confidence must be in"):
            bootstrap_mean_ci([0.1, 0.5, 0.9], confidence=confidence)

    def test_non_positive_n_resamples_raises(self):
        with pytest.raises(ValueError, match="n_resamples must be"):
            bootstrap_mean_ci([0.1, 0.5, 0.9], n_resamples=0)

    def test_mean_is_correct(self):
        m, _lo, _hi = bootstrap_mean_ci([0.0, 1.0])
        assert abs(m - 0.5) < 1e-9

    def test_ci_contains_mean_for_uniform(self):
        # [0.0]*50 + [1.0]*50 has mean 0.5; CI should contain 0.5
        values = [0.0] * 50 + [1.0] * 50
        m, lo, hi = bootstrap_mean_ci(values)
        assert lo <= m <= hi

    def test_is_deterministic(self):
        values = [float(i) / 9.0 for i in range(10)]
        result1 = bootstrap_mean_ci(values)
        result2 = bootstrap_mean_ci(values)
        assert result1 == result2

    def test_different_seeds_may_differ(self):
        values = [0.0] * 5 + [1.0] * 5
        r0 = bootstrap_mean_ci(values, seed=0)
        r1 = bootstrap_mean_ci(values, seed=1)
        # CIs might be different with different seeds (not guaranteed, but likely for this input)
        # At minimum, means are the same
        assert abs(r0[0] - r1[0]) < 1e-9

    def test_tight_ci_for_constant_input(self):
        values = [0.8] * 20
        m, lo, hi = bootstrap_mean_ci(values)
        assert abs(m - 0.8) < 1e-9
        assert abs(lo - 0.8) < 1e-9
        assert abs(hi - 0.8) < 1e-9

    def test_lo_le_mean_le_hi(self):
        values = [0.1, 0.3, 0.5, 0.7, 0.9, 0.2, 0.4, 0.6, 0.8, 1.0]
        m, lo, hi = bootstrap_mean_ci(values)
        assert lo <= m <= hi


class TestWilsonInterval:
    def test_zero_n_returns_zero_tuple(self):
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_all_failures_low_near_zero(self):
        lo, hi = wilson_interval(0, 10)
        assert lo < 1e-10  # clamped to ~0; exact value depends on floating-point z
        assert hi > 0.0

    def test_all_successes_high_near_one(self):
        lo, hi = wilson_interval(10, 10)
        assert hi > 1.0 - 1e-10  # clamped to ~1; exact value depends on floating-point z
        assert lo < 1.0

    def test_half_half_roughly_symmetric(self):
        lo, hi = wilson_interval(5, 10)
        # Should be roughly (0.24, 0.76) per Wilson formula
        assert 0.20 < lo < 0.30
        assert 0.70 < hi < 0.80

    def test_interval_contains_true_rate(self):
        lo, hi = wilson_interval(7, 10)
        assert lo <= 0.7 <= hi

    def test_bounds_clamped_to_unit_interval(self):
        lo, hi = wilson_interval(1, 1)
        assert 0.0 <= lo <= 1.0
        assert 0.0 <= hi <= 1.0


class TestPairedTCi:
    """The Student-t paired interval that replaced the percentile bootstrap — it
    shares a distribution with paired_t_test, so the two always agree about 0."""

    def test_none_on_invalid_input(self):
        assert paired_t_ci([1.0, 2.0], [1.0]) is None
        assert paired_t_ci([1.0], [0.5]) is None
        assert paired_t_ci([], []) is None
        assert paired_t_ci([1.0, float("nan")], [0.5, 0.5]) is None

    def test_zero_variance_shift_is_a_point_interval(self):
        result = paired_t_ci([1.0] * 20, [0.5] * 20)
        assert result is not None
        mean_diff, lo, hi = result
        assert abs(mean_diff - 0.5) < 1e-12
        # Every diff identical ⇒ sd = 0 ⇒ the interval collapses onto the mean.
        assert (lo, hi) == (0.5, 0.5)

    def test_ci_contains_diff_and_is_symmetric(self):
        a = [0.3, 0.5, 0.9, 0.2, 0.7]
        b = [0.2, 0.55, 0.6, 0.3, 0.5]
        result = paired_t_ci(a, b)
        assert result is not None
        mean_diff, lo, hi = result
        assert lo <= mean_diff <= hi
        assert abs((mean_diff - lo) - (hi - mean_diff)) < 1e-12

    def test_agrees_with_paired_t_test_about_zero(self):
        """The interval excludes 0 exactly when the p-value is below alpha."""
        for a, b in (
            ([0.9, 0.5, 0.8, 0.7, 0.95], [0.7, 0.55, 0.6, 0.72, 0.8]),  # p = 0.153, not significant
            ([0.9, 0.85, 0.95, 0.8, 0.9], [0.2, 0.25, 0.15, 0.3, 0.2]),  # strongly significant
        ):
            ci = paired_t_ci(a, b, confidence=0.95)
            p = paired_t_test(a, b)
            assert ci is not None and p is not None
            _, lo, hi = ci
            excludes_zero = lo > 0 or hi < 0
            assert excludes_zero == (p < 0.05)


class TestStudentTCritical:
    def test_matches_t_table(self):
        assert abs(student_t_critical(0.95, 4) - 2.776) < 1e-3
        assert abs(student_t_critical(0.95, 10) - 2.228) < 1e-3
        assert abs(student_t_critical(0.99, 10) - 3.169) < 1e-3

    def test_converges_to_normal_at_large_df(self):
        assert abs(student_t_critical(0.95, 1e7) - 1.959964) < 1e-4

    def test_rejects_confidence_outside_unit_interval(self):
        with pytest.raises(ValueError, match="confidence must be in"):
            student_t_critical(1.0, 5)

    def test_degenerate_df_is_infinite(self):
        assert student_t_critical(0.95, 0) == float("inf")


class TestCohensD:
    def test_none_on_length_mismatch(self):
        assert cohens_d([1.0, 2.0], [1.0]) is None

    def test_none_on_single_pair(self):
        assert cohens_d([1.0], [0.5]) is None

    def test_none_on_zero_variance(self):
        # When all diffs are the same, stddev=0 → return None
        a = [1.0, 1.0, 1.0]
        b = [0.5, 0.5, 0.5]
        assert cohens_d(a, b) is None

    def test_positive_d_when_a_greater(self):
        a = [1.0, 0.9, 1.0, 0.95, 1.0]
        b = [0.5, 0.4, 0.6, 0.5, 0.55]
        d = cohens_d(a, b)
        assert d is not None
        assert d > 0

    def test_negative_d_when_b_greater(self):
        # Need non-constant diffs for stddev > 0
        import random

        rng = random.Random(42)
        a = [0.3 + rng.gauss(0, 0.05) for _ in range(20)]
        b = [0.8 + rng.gauss(0, 0.05) for _ in range(20)]
        d = cohens_d(a, b)
        assert d is not None
        assert d < 0

    def test_known_analytic_value(self):
        # diffs = [1.0, -1.0, 1.0, -1.0]  mean=0, stddev=~1.1547 → d≈0
        a = [1.0, 0.0, 1.0, 0.0]
        b = [0.0, 1.0, 0.0, 1.0]
        d = cohens_d(a, b)
        # mean(diffs)=0 → d=0/s=0
        assert d is not None
        assert abs(d) < 1e-9

    def test_large_effect_gives_large_d(self):
        # All same diff of 1.0 → stddev of diffs=0 → None
        # Use near-constant diffs instead
        import random

        rng = random.Random(42)
        diffs = [1.0 + rng.gauss(0, 0.01) for _ in range(30)]
        a = [0.0 + d for d in diffs]
        b = [0.0] * 30
        d = cohens_d(a, b)
        assert d is not None
        assert d > 50  # very large effect


def _mean(values: list[float]) -> float:
    """Statistic under test for the cluster bootstrap — 0.0 for an empty pool, matching the
    div-by-zero convention every real statistic here uses."""
    return sum(values) / len(values) if values else 0.0


def _f1_yes(pairs: list[tuple[str, str]]) -> float:
    """`f1.yes` through the criterion layer's own routine, exactly as the gate computes it."""
    from coder_eval.criteria._classification_aggregate import classification_metrics

    cm = classification_metrics(pairs)
    return cm.metric("f1.yes") if cm else 0.0


class TestClusterBootstrapDiffCi:
    """Resamples ROWS, not observations: within-row replicates are not independent, so
    drawing them individually would understate the interval."""

    def test_paired_draw_uses_same_indices(self):
        # Identical arms ⇒ every draw cancels EXACTLY, so the interval has zero width. The
        # zero width is the whole assertion: an UNPAIRED implementation (two independent
        # index vectors) still gives point_diff 0.0 and still contains 0 — measured at
        # [-2.25, 2.25] on this fixture — so a `lo <= 0 <= hi` check would pass on the very
        # bug this test exists to catch.
        clusters = [[float(i), float(i) + 0.5] for i in range(8)]
        result = cluster_bootstrap_diff_ci(clusters, [list(c) for c in clusters], _mean)
        assert result is not None
        point_diff, lo, hi, _p = result
        assert point_diff == 0.0
        assert (lo, hi) == (0.0, 0.0)

    def test_separated_arms_ci_excludes_zero(self):
        # Candidate f1.yes = 1.0 (every row engages), incumbent = 0.4 (3 of 12 engage).
        candidate = [[("yes", "yes")] for _ in range(12)]
        incumbent = [[("yes", "yes")] if i < 3 else [("yes", "no")] for i in range(12)]
        assert abs(_f1_yes([p for c in incumbent for p in c]) - 0.4) < 1e-12

        result = cluster_bootstrap_diff_ci(candidate, incumbent, _f1_yes)
        assert result is not None
        point_diff, lo, _hi, _p = result
        assert abs(point_diff - 0.6) < 1e-12
        assert lo > 0.0

    def test_tied_arms_ci_contains_zero(self):
        rng_pairs = [("yes", "yes"), ("yes", "no"), ("no", "no")]
        arm_a = [[rng_pairs[i % 3]] for i in range(12)]
        arm_b = [[rng_pairs[(i + 1) % 3]] for i in range(12)]
        result = cluster_bootstrap_diff_ci(arm_a, arm_b, _f1_yes)
        assert result is not None
        _diff, lo, hi, p = result
        assert lo <= 0.0 <= hi
        assert p > 0.05

    def test_rejects_misaligned_lengths(self):
        with pytest.raises(ValueError, match="aligned by index"):
            cluster_bootstrap_diff_ci([[1.0], [2.0]], [[1.0]], _mean)

    @pytest.mark.parametrize("confidence", [0.0, 1.0, -0.1, 1.5])
    def test_rejects_bad_confidence(self, confidence):
        with pytest.raises(ValueError, match="confidence must be in"):
            cluster_bootstrap_diff_ci([[1.0], [2.0]], [[1.0], [2.0]], _mean, confidence=confidence)

    def test_rejects_non_positive_n_resamples(self):
        with pytest.raises(ValueError, match="n_resamples must be"):
            cluster_bootstrap_diff_ci([[1.0], [2.0]], [[1.0], [2.0]], _mean, n_resamples=0)

    def test_fewer_than_two_clusters_returns_none(self):
        assert cluster_bootstrap_diff_ci([], [], _mean) is None
        assert cluster_bootstrap_diff_ci([[1.0]], [[0.0]], _mean) is None

    def test_is_deterministic_for_a_seed(self):
        a = [[float(i)] for i in range(10)]
        b = [[float(i) * 0.5] for i in range(10)]
        assert cluster_bootstrap_diff_ci(a, b, _mean, seed=7) == cluster_bootstrap_diff_ci(a, b, _mean, seed=7)

    def test_p_value_uses_the_phipson_smyth_estimator(self):
        # Perfectly separated arms: no draw can produce a diff <= 0, so the naive count is 0.
        # The unbiased estimator adds the observed statistic to both numerator and denominator,
        # so the two-sided p comes back at 2/(m+1) — STRICTLY LARGER than the 1/m the old clamp
        # reported. That understatement is the defect, so the direction is part of the assertion.
        a = [[1.0] for _ in range(10)]
        b = [[0.0] for _ in range(10)]
        result = cluster_bootstrap_diff_ci(a, b, _mean)
        assert result is not None
        assert result[3] == pytest.approx(2.0 / (BOOTSTRAP_RESAMPLES + 1))
        assert result[3] > 1.0 / BOOTSTRAP_RESAMPLES

    def test_p_value_denominator_is_the_draw_count_plus_one(self):
        """The one test a naive count wearing a (b+1)/(m+1)-shaped FLOOR would fail.

        Every other test here uses a degenerate fixture — no null draws, or nothing but null
        draws — and on those `max(2/(m+1), 2*b/m)` is indistinguishable from the real estimator.
        This fixture has an intermediate tail count (b_le = 185 of 2,000 draws), where the two
        forms diverge in the fourth digit: 2*186/2001 = 0.185907 against 2*185/2000 = 0.185.
        Both halves are asserted — the exact value, and the structural property that the
        denominator is `m + 1` rather than `m`.
        """
        rng = random.Random(11)
        a = [[rng.gauss(0.55, 0.3)] for _ in range(20)]
        b = [[rng.gauss(0.45, 0.3)] for _ in range(20)]
        result = cluster_bootstrap_diff_ci(a, b, _mean)
        assert result is not None
        p = result[3]

        b_le = 185  # the null-draw count this seed produces; the estimator adds one to it
        assert p == pytest.approx(2.0 * (b_le + 1) / (BOOTSTRAP_RESAMPLES + 1))
        assert p != pytest.approx(2.0 * b_le / BOOTSTRAP_RESAMPLES), "a naive b/m count, floored, would land here"
        # p is an exact multiple of 2/(m+1) and NOT of 2/m — the denominator, pinned structurally.
        assert (p * (BOOTSTRAP_RESAMPLES + 1) / 2.0) == pytest.approx(round(p * (BOOTSTRAP_RESAMPLES + 1) / 2.0))
        assert (p * BOOTSTRAP_RESAMPLES / 2.0) != pytest.approx(round(p * BOOTSTRAP_RESAMPLES / 2.0))

    def test_identical_arms_report_p_one(self):
        # Every diff is exactly 0.0, so it counts in BOTH tails: 2*(m+1)/(m+1) = 2.0, which the
        # min(1.0, ...) clamp brings back to 1.0. The clamp is load-bearing well beyond this
        # fixture: an exact even split with no ties at all gives 2*(m/2+1)/(m+1) > 1 too, so it
        # binds on ordinary near-null data and must not be deleted once "the tie case is handled".
        clusters = [[float(i)] for i in range(6)]
        result = cluster_bootstrap_diff_ci(clusters, [list(c) for c in clusters], _mean)
        assert result is not None
        assert result[3] == 1.0

    def test_p_value_never_zero_at_a_single_resample(self):
        a = [[1.0] for _ in range(10)]
        b = [[0.0] for _ in range(10)]
        result = cluster_bootstrap_diff_ci(a, b, _mean, n_resamples=1)
        assert result is not None
        assert result[3] == 1.0

    def test_p_value_is_monotone_in_the_null_count(self):
        # Two fixtures whose null-draw counts differ: the tied arms produce a null on every draw,
        # the separated arms on none. A p that did not move with the count would mean the `min`
        # picked the wrong tail.
        separated = cluster_bootstrap_diff_ci([[1.0] for _ in range(10)], [[0.0] for _ in range(10)], _mean)
        tied = cluster_bootstrap_diff_ci([[1.0] for _ in range(10)], [[1.0] for _ in range(10)], _mean)
        assert separated is not None and tied is not None
        assert tied[3] > separated[3]

    @pytest.mark.parametrize("n_resamples", [0, -1, -5])
    def test_bootstrap_p_floor_refuses_a_draw_count_the_estimator_would_reject(self, n_resamples):
        # 2/(0+1) = 2.0 is a "p floor" above 1.0, which would make every p read as AT the floor.
        # The two bootstraps refuse the same input for the same reason; so does this.
        with pytest.raises(ValueError, match="n_resamples must be"):
            bootstrap_p_floor(n_resamples)

    @pytest.mark.parametrize("n_resamples", [1, 7, 500, BOOTSTRAP_RESAMPLES])
    def test_bootstrap_p_floor_matches_what_the_estimator_can_actually_return(self, n_resamples):
        # What makes the exported helper trustworthy enough for the gate to decide against.
        a = [[1.0] for _ in range(10)]
        b = [[0.0] for _ in range(10)]
        result = cluster_bootstrap_diff_ci(a, b, _mean, n_resamples=n_resamples)
        assert result is not None
        assert result[3] == pytest.approx(bootstrap_p_floor(n_resamples))

    def test_unequal_within_cluster_counts_are_legal(self):
        # A row that errored on one invocation contributes fewer observations, not an error.
        a = [[1.0, 1.0], [1.0], [1.0, 1.0, 1.0], [1.0]]
        b = [[0.0], [0.0, 0.0], [0.0], [0.0]]
        result = cluster_bootstrap_diff_ci(a, b, _mean)
        assert result is not None
        assert abs(result[0] - 1.0) < 1e-12

    def test_a_cluster_empty_on_one_side_contributes_nothing(self):
        # Legal input — the caller counts the eroded rows. The empty cluster contributes no
        # observation to arm a's pool, so the arms' means stay identical rather than the
        # hole reading as a 0.0 sample.
        a = [[1.0], [], [1.0], [1.0]]
        b = [[1.0], [1.0], [1.0], [1.0]]
        result = cluster_bootstrap_diff_ci(a, b, _mean)
        assert result is not None
        assert result[0] == 0.0

    def test_statistic_errors_propagate(self):
        def _boom(_values: list[float]) -> float:
            raise RuntimeError("degenerate resample")

        with pytest.raises(RuntimeError, match="degenerate resample"):
            cluster_bootstrap_diff_ci([[1.0], [2.0]], [[1.0], [2.0]], _boom)


class TestHolmRejections:
    def test_order_and_stepdown(self):
        # 0.001 <= 0.05/3 rejects; 0.04 > 0.05/2 stops the step-down, so the third is not
        # tested at all even though 0.04 <= 0.05.
        assert holm_rejections([0.001, 0.04, 0.04], alpha=0.05) == [True, False, False]

    def test_rejections_come_back_in_input_order(self):
        assert holm_rejections([0.04, 0.001, 0.04], alpha=0.05) == [False, True, False]

    def test_is_more_powerful_than_bonferroni(self):
        # Bonferroni would reject only p=0.001 (0.02 > 0.05/3); Holm rejects both.
        assert holm_rejections([0.001, 0.02, 0.9], alpha=0.05) == [True, True, False]

    def test_empty_and_single(self):
        assert holm_rejections([]) == []
        assert holm_rejections([0.049], alpha=0.05) == [True]
        assert holm_rejections([0.051], alpha=0.05) == [False]

    @pytest.mark.parametrize("alpha", [0.0, 1.0, -0.5, 2.0])
    def test_rejects_bad_alpha(self, alpha):
        with pytest.raises(ValueError, match="alpha must be in"):
            holm_rejections([0.01], alpha=alpha)

    def test_rejects_non_finite_p_values(self):
        with pytest.raises(ValueError, match="must all be finite"):
            holm_rejections([0.01, float("nan")])


class TestMedianOrNone:
    """The empty-sample contract, which is the entire reason this exists beside `mean`.

    `mean` folds an empty sample to `0.0`, and a cost guardrail cannot tell that from an arm that
    genuinely cost nothing — so `None` and `0.0` must never collapse into each other.
    """

    def test_returns_none_on_an_empty_sample(self):
        assert median_or_none([]) is None

    def test_a_median_of_zero_is_not_none(self):
        assert median_or_none([0.0]) == 0.0
        assert median_or_none([0.0]) is not None
        assert median_or_none([-1.0, 0.0, 1.0]) == 0.0

    def test_it_is_the_ordinary_median_otherwise(self):
        assert median_or_none([3.0, 1.0, 2.0]) == 2.0
        # Even count averages the middle pair, as `statistics.median` does.
        assert median_or_none([1.0, 2.0, 3.0, 4.0]) == 2.5

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_sample_is_none_rather_than_nan(self, bad):
        """A `nan` median reaches a guardrail's relative-change arithmetic and answers neither way.

        Both callers already branch on `None` — one emits an unevaluated check with a note, the
        other treats the point as absent — and neither has a branch for a `nan`.
        """
        assert median_or_none([bad]) is None
        assert median_or_none([1.0, 2.0, bad]) is None
