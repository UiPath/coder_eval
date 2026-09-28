"""Distribution-free statistical helpers for the report renderers.

This module must stay **dependency-free** — stdlib only, no ``coder_eval``
import, direct or relative. That is what lets the numeric core be tested and
reasoned about in isolation, and it is asserted by a test rather than left to
convention (``tests/test_stats.py``).

Display formatting deliberately lives elsewhere: ``fmt_mean_sd`` and ``fmt_p``
return ``"N/A"``, ``"—"`` and ``"<0.001"``, which is presentation, not
computation, so they sit with the other report formatters.
"""

from __future__ import annotations

import logging
import math
import random
import statistics as _stats


logger = logging.getLogger(__name__)


def mean(values: list[float]) -> float:
    return _stats.mean(values) if values else 0.0


def stddev(values: list[float]) -> float:
    """Sample standard deviation (Bessel-corrected). Returns 0.0 for n < 2."""
    return _stats.stdev(values) if len(values) >= 2 else 0.0


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (Lentz's method)."""
    max_iterations = 200
    eps = 3e-12
    fpmin = 1e-300

    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iterations + 1):
        m2 = 2 * m
        # Even step of the recurrence.
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        # Odd step.
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    logger.warning("Incomplete beta continued fraction did not converge for a=%r, b=%r, x=%r", a, b, x)
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b), for a, b > 0 and x in [0, 1].

    Raises ValueError outside that domain — returning NaN would let a bad input
    render as a real-looking statistic downstream.
    """
    if not (math.isfinite(a) and math.isfinite(b) and math.isfinite(x)):
        raise ValueError(f"a, b and x must be finite, got a={a!r}, b={b!r}, x={x!r}")
    if a <= 0.0 or b <= 0.0:
        raise ValueError(f"a and b must be positive, got a={a!r}, b={b!r}")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    front = math.exp(ln_front)
    # Use the continued fraction directly where it converges fast, else via symmetry.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_two_tailed_p(t_stat: float, df: float) -> float:
    """Exact two-tailed p-value for Student's t: P(|T| >= |t|) = I_x(df/2, 1/2), x = df/(df + t^2).

    Non-finite inputs fail closed to 1.0 — garbage must never read as significant.
    """
    if not math.isfinite(t_stat) or not math.isfinite(df) or df <= 0:
        return 1.0
    x = df / (df + t_stat * t_stat)
    return regularized_incomplete_beta(df / 2.0, 0.5, x)


def welch_t_test(a: list[float], b: list[float]) -> float | None:
    """Two-tailed p-value from Welch's unequal-variances t-test (exact t distribution).

    Degrees of freedom via Welch-Satterthwaite; the t CDF is evaluated exactly
    through the regularized incomplete beta (stdlib only, no scipy). Returns
    None if either group has fewer than 2 observations, or holds a non-finite
    value (rendered as "—" rather than a fabricated p-value).
    """
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return None
    if not all(math.isfinite(v) for v in (*a, *b)):
        return None

    mean_a, mean_b = _stats.mean(a), _stats.mean(b)
    var_a = _stats.variance(a)
    var_b = _stats.variance(b)

    se_sq = var_a / n_a + var_b / n_b
    if se_sq == 0:
        # Zero variance in both groups: identical constants (p=1) or a
        # deterministic difference (p=0).
        return 1.0 if mean_a == mean_b else 0.0

    t_stat = abs(mean_a - mean_b) / math.sqrt(se_sq)
    df = se_sq**2 / ((var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1))
    return student_t_two_tailed_p(t_stat, df)


def bootstrap_mean_ci(
    values: list[float],
    n_resamples: int = 1000,
    confidence: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Percentile-bootstrap confidence interval for the mean.

    Returns (mean, ci_low, ci_high). When ``len(values) < 2``, returns
    (values[0], values[0], values[0]) or (0, 0, 0) for empty input.
    Uses ``random.Random(seed)`` for determinism.

    Raises ValueError for a ``confidence`` outside (0, 1) or a non-positive
    ``n_resamples`` — clamping those would quietly return an interval of the
    wrong width, which is worse than refusing.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence!r}")
    if n_resamples < 1:
        raise ValueError(f"n_resamples must be >= 1, got {n_resamples!r}")
    if not values:
        return (0.0, 0.0, 0.0)
    m = sum(values) / len(values)
    if len(values) < 2:
        return (m, m, m)
    rng = random.Random(seed)
    n = len(values)
    resampled_means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(n_resamples))
    alpha = (1.0 - confidence) / 2.0
    lo = resampled_means[int(alpha * n_resamples)]
    hi = resampled_means[int((1.0 - alpha) * n_resamples) - 1]
    return (m, lo, hi)


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Returns (low, high).

    More reliable than the normal approximation at small N and near 0/1.
    """
    if n <= 0:
        return (0.0, 0.0)
    z = _stats.NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    p_hat = successes / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    half = (z * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4.0 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def cohens_d(a: list[float], b: list[float]) -> float | None:
    """Paired Cohen's d = mean(a_i - b_i) / stddev(a_i - b_i)."""
    if len(a) != len(b) or len(a) < 2:
        return None
    diffs = [ai - bi for ai, bi in zip(a, b, strict=True)]
    s = stddev(diffs)
    return (sum(diffs) / len(diffs)) / s if s > 0 else None


def student_t_critical(confidence: float, df: float) -> float:
    """Two-tailed critical value t* with P(|T| >= t*) = 1 - confidence.

    Inverts :func:`student_t_two_tailed_p` by bisection — that p is continuous and
    strictly decreasing in |t|, so a plain bracket-and-halve is exact to ~1e-12 and
    needs no separate quantile expansion.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence!r}")
    if df <= 0 or not math.isfinite(df):
        return math.inf
    alpha = 1.0 - confidence
    lo, hi = 0.0, 1.0
    while student_t_two_tailed_p(hi, df) > alpha:
        lo = hi
        hi *= 2.0
        if hi > 1e12:
            # Only reachable for a confidence so close to 1 that t* overflows the
            # bracket. Warn rather than return a silently wrong-width interval.
            logger.warning(
                "student_t_critical failed to bracket t* for confidence=%r, df=%r; returning a degraded upper bound",
                confidence,
                df,
            )
            return hi
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if mid in (lo, hi):
            break
        if student_t_two_tailed_p(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def paired_t_ci(a: list[float], b: list[float], confidence: float = 0.95) -> tuple[float, float, float] | None:
    """Student-t confidence interval for mean(a_i - b_i): mean ± t* · sd/√n.

    Returns (mean_diff, ci_low, ci_high), or None if lengths differ, n < 2, or any
    value is non-finite. Shares its distribution with :func:`paired_t_test`, so the
    interval and the p-value always agree about whether 0 is excluded.
    """
    if len(a) != len(b) or len(a) < 2:
        return None
    if not all(math.isfinite(v) for v in (*a, *b)):
        return None
    diffs = [ai - bi for ai, bi in zip(a, b, strict=True)]
    n = len(diffs)
    mean_diff = sum(diffs) / n
    half_width = student_t_critical(confidence, n - 1) * stddev(diffs) / math.sqrt(n)
    return (mean_diff, mean_diff - half_width, mean_diff + half_width)


def paired_t_test(a: list[float], b: list[float]) -> float | None:
    """Two-tailed p-value from a paired t-test on (a_i - b_i), exact t distribution.

    Equivalent to a one-sample t-test of the differences against 0, df = n - 1.
    Returns None if lengths differ, n < 2, or any value is non-finite.
    """
    if len(a) != len(b) or len(a) < 2:
        return None
    if not all(math.isfinite(v) for v in (*a, *b)):
        return None
    diffs = [ai - bi for ai, bi in zip(a, b, strict=True)]
    sd = stddev(diffs)
    mean_diff = sum(diffs) / len(diffs)
    if sd == 0:
        # All diffs identical: no difference (p=1) or a deterministic shift (p=0).
        return 1.0 if mean_diff == 0 else 0.0
    t_stat = abs(mean_diff) / (sd / math.sqrt(len(diffs)))
    return student_t_two_tailed_p(t_stat, len(diffs) - 1)
