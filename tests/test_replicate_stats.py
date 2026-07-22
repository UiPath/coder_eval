"""Unit tests for replicate statistics helpers in reports_stats."""

import pytest

from coder_eval.reports_stats import (
    bootstrap_mean_ci,
    cohens_d,
    paired_bootstrap_diff_ci,
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


class TestPairedBootstrapDiffCi:
    def test_none_on_length_mismatch(self):
        assert paired_bootstrap_diff_ci([1.0, 2.0], [1.0]) is None

    def test_none_on_single_pair(self):
        assert paired_bootstrap_diff_ci([1.0], [0.5]) is None

    def test_none_on_empty(self):
        assert paired_bootstrap_diff_ci([], []) is None

    def test_detects_positive_shift(self):
        a = [1.0] * 20
        b = [0.5] * 20
        result = paired_bootstrap_diff_ci(a, b)
        assert result is not None
        mean_diff, lo, hi = result
        assert abs(mean_diff - 0.5) < 0.01
        # CI should be very tight around 0.5 (zero variance)
        assert lo > 0.45
        assert hi < 0.55

    def test_detects_negative_shift(self):
        # Use non-constant inputs so CI is not degenerate
        import random

        rng = random.Random(7)
        a = [0.3 + rng.gauss(0, 0.05) for _ in range(20)]
        b = [0.8 + rng.gauss(0, 0.05) for _ in range(20)]
        result = paired_bootstrap_diff_ci(a, b)
        assert result is not None
        mean_diff, lo, hi = result
        assert mean_diff < 0
        assert lo < hi

    def test_ci_contains_diff(self):
        a = [float(i) / 9 for i in range(10)]
        b = [float(i) / 9 + 0.1 for i in range(10)]
        result = paired_bootstrap_diff_ci(a, b)
        assert result is not None
        mean_diff, lo, hi = result
        assert lo <= mean_diff <= hi

    def test_is_deterministic(self):
        a = [0.1 * i for i in range(1, 11)]
        b = [0.1 * i + 0.05 for i in range(1, 11)]
        r1 = paired_bootstrap_diff_ci(a, b)
        r2 = paired_bootstrap_diff_ci(a, b)
        assert r1 == r2


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
