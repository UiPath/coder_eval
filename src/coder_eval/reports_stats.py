"""Shared statistical + prompt-config helpers for the markdown and HTML reporters.

Kept in a standalone module so ``reports_html`` can consume them without
importing ``reports_experiment`` (which in turn imports ``reports_html``
for its HTML-write helpers, and would otherwise form a cycle).
"""

from __future__ import annotations

import logging
import math
import random
import statistics as _stats
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from coder_eval.models import EvaluationResult, ExperimentResult, ExperimentVariant, TaskExperimentSummary


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Statistical helpers (stdlib statistics module)
# ---------------------------------------------------------------------------


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


def fmt_mean_sd(values: list[float], fmt: str = ".3f") -> str:
    """Format mean ± stddev string. Omits ± when n < 2 (stddev undefined)."""
    if not values:
        return "N/A"
    m = mean(values)
    if len(values) < 2:
        return f"{m:{fmt}}"
    sd = stddev(values)
    return f"{m:{fmt}} ± {sd:{fmt}}"


def fmt_p(p: float | None) -> str:
    """Format p-value for display."""
    if p is None:
        return "—"
    if p < 0.001:
        return "<0.001"
    return f"{p:.3f}"


# ---------------------------------------------------------------------------
# Replicate statistics helpers (stdlib random + statistics)
# ---------------------------------------------------------------------------


# One resample count for every bootstrap in the codebase. Raising the former 1,000
# default only tightens Monte-Carlo error, so no report becomes less accurate — and the
# magic number stops existing in two places at two values.
BOOTSTRAP_RESAMPLES = 2000


def bootstrap_mean_ci(
    values: list[float],
    n_resamples: int = BOOTSTRAP_RESAMPLES,
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


def cluster_bootstrap_diff_ci[T](
    clusters_a: list[list[T]],
    clusters_b: list[list[T]],
    statistic: Callable[[list[T]], float],
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float, float] | None:
    """Paired cluster bootstrap for ``statistic(a) - statistic(b)``.

    Returns ``(point_diff, ci_low, ci_high, p_value)``, or ``None`` with fewer than 2
    clusters — an interval over one cluster is a fabrication, not a wide interval.

    ``clusters_a`` and ``clusters_b`` are **aligned by index**: cluster ``i`` is the same
    unit (a dataset row, say) measured under each arm. A length mismatch is a wiring bug
    rather than a degraded input, so it raises. Each draw samples cluster *indices* with
    replacement **once** and applies them to both arms — that shared index vector is what
    makes the comparison paired — then pools the drawn clusters' observations and applies
    ``statistic`` to each arm's flattened pool. Resampling observations individually instead
    would treat within-cluster replicates as independent and understate the interval.

    Clusters need not be the same length as each other, or even non-empty: an arm that
    produced nothing for a row simply contributes nothing to that arm's pool. Only the
    *number* of clusters has to match.

    ``p_value`` is the standard two-sided bootstrap p, ``2 * min(P(diff <= 0), P(diff >= 0))``,
    clamped to ``[1 / n_resamples, 1.0]`` — a p of exactly 0 is bounded below by the resample
    resolution, not established as zero.

    Same contract as :func:`bootstrap_mean_ci`: ``ValueError`` for a ``confidence`` outside
    (0, 1) or a non-positive ``n_resamples``, and ``random.Random(seed)`` for determinism.
    An exception from ``statistic`` propagates: a swallowed statistic error would silently
    narrow the interval below what the data supports.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence!r}")
    if n_resamples < 1:
        raise ValueError(f"n_resamples must be >= 1, got {n_resamples!r}")
    if len(clusters_a) != len(clusters_b):
        raise ValueError(
            f"clusters_a and clusters_b must be aligned by index, got {len(clusters_a)} and {len(clusters_b)} clusters"
        )
    n = len(clusters_a)
    if n < 2:
        return None

    point_diff = statistic([obs for c in clusters_a for obs in c]) - statistic([obs for c in clusters_b for obs in c])

    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(n_resamples):
        drawn = [rng.randrange(n) for _ in range(n)]
        pooled_a = [obs for i in drawn for obs in clusters_a[i]]
        pooled_b = [obs for i in drawn for obs in clusters_b[i]]
        diffs.append(statistic(pooled_a) - statistic(pooled_b))
    diffs.sort()

    alpha = (1.0 - confidence) / 2.0
    ci_low = diffs[int(alpha * n_resamples)]
    ci_high = diffs[int((1.0 - alpha) * n_resamples) - 1]

    p_le = sum(1 for d in diffs if d <= 0.0) / n_resamples
    p_ge = sum(1 for d in diffs if d >= 0.0) / n_resamples
    p_value = min(1.0, max(1.0 / n_resamples, 2.0 * min(p_le, p_ge)))
    return (point_diff, ci_low, ci_high, p_value)


def holm_rejections(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni step-down over a family of p-values. Rejections come back in INPUT order.

    Order the ``S`` p-values ascending and reject the ``i``-th (1-indexed) only while
    ``p_(i) <= alpha / (S - i + 1)`` *and* every earlier one was rejected — the step-down is
    what makes Holm uniformly more powerful than Bonferroni while controlling the same
    family-wise error rate.

    The correction is a property of the FAMILY: calling this with a one-element list
    correctly degenerates to ``p <= alpha``, and dividing alpha by the family size at a single
    gate would be plain Bonferroni, not Holm. Empty input returns ``[]``.

    Raises ``ValueError`` for an ``alpha`` outside (0, 1) or a non-finite p-value.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    if any(not math.isfinite(p) for p in p_values):
        raise ValueError(f"p_values must all be finite, got {p_values!r}")

    total = len(p_values)
    rejections = [False] * total
    for rank, i in enumerate(sorted(range(total), key=lambda k: p_values[k])):
        if p_values[i] > alpha / (total - rank):
            break
        rejections[i] = True
    return rejections


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


# ---------------------------------------------------------------------------
# Aggregate-metric series
# ---------------------------------------------------------------------------


class VariantSeries(NamedTuple):
    """One variant's numeric series across all tasks — the raw inputs to the
    Aggregate Metrics rows and their p-values."""

    scores: list[float]
    durations: list[float]
    tokens: list[float]
    asst_turns: list[float]


def collect_variant_series(result: ExperimentResult) -> dict[str, VariantSeries]:
    """Per-variant (scores, durations, tokens, assistant-turns) series, keyed by variant id.

    Shared by the markdown and HTML reporters so both render the same numbers.
    ``VariantResult.duration_seconds`` is *summed* across replicates, so it is
    divided by ``replicate_count`` to give a per-run duration comparable across
    variants that ran different replicate counts.
    """
    series = {vid: VariantSeries([], [], [], []) for vid in result.variant_ids}
    for ts in result.task_summaries:
        for vr in ts.variant_results:
            s = series.get(vr.variant_id)
            if s is None:  # a task result for a variant not in variant_ids
                continue
            s.scores.append(vr.weighted_score)
            s.durations.append(vr.duration_seconds / vr.replicate_count)
            if vr.total_tokens is not None:
                s.tokens.append(float(vr.total_tokens))
            if vr.total_assistant_turns is not None:
                s.asst_turns.append(float(vr.total_assistant_turns))
    return series


class PairedComparison(NamedTuple):
    """A 2-variant paired comparison over per-task mean scores.

    ``task_count`` is the number of tasks both variants scored. When it is < 2
    the statistics are all ``None`` — there is nothing to compare, and the
    reporters say so rather than rendering an empty section. ``excluded_count``
    is the number of tasks that appeared for at least one variant but could not
    be paired (missing or empty on the other side); the reporters surface it so
    a silently narrowed sample is visible.
    """

    vid_a: str
    vid_b: str
    task_count: int
    excluded_count: int
    mean_diff: float | None
    ci_low: float | None
    ci_high: float | None
    effect_size: float | None
    p_value: float | None


def paired_comparison(result: ExperimentResult, confidence: float = 0.95) -> PairedComparison | None:
    """Pair the two variants' per-task mean scores. Returns None unless the
    experiment has exactly 2 variants with at least one commonly-scored task.

    The task is the unit of analysis: replicate slots within a task share the task
    effect and are not independent, so pairing them individually would understate
    the standard error. Replicate counts need not match — a task's mean score is a
    well-defined pair member either way.
    """
    if len(result.variant_ids) != 2:
        return None
    vid_a, vid_b = result.variant_ids[0], result.variant_ids[1]
    per_rep_a = result.per_replicate_scores.get(vid_a, {})
    per_rep_b = result.per_replicate_scores.get(vid_b, {})
    common_tasks = sorted(t for t in set(per_rep_a) & set(per_rep_b) if per_rep_a[t] and per_rep_b[t])
    if not common_tasks:
        # No shared task, or per_replicate_scores absent (results from before it existed).
        return None

    # Tasks seen for at least one variant but not paired (missing or empty on the
    # other side) — surfaced so a silently narrowed sample doesn't go unnoticed.
    excluded_count = len(set(per_rep_a) | set(per_rep_b)) - len(common_tasks)

    if len(common_tasks) < 2:
        return PairedComparison(vid_a, vid_b, len(common_tasks), excluded_count, None, None, None, None, None)

    a_scores = [mean(per_rep_a[task_id]) for task_id in common_tasks]
    b_scores = [mean(per_rep_b[task_id]) for task_id in common_tasks]
    ci = paired_t_ci(a_scores, b_scores, confidence=confidence)
    if ci is None:  # non-finite scores
        return PairedComparison(vid_a, vid_b, len(common_tasks), excluded_count, None, None, None, None, None)
    mean_diff, ci_low, ci_high = ci
    return PairedComparison(
        vid_a,
        vid_b,
        len(common_tasks),
        excluded_count,
        mean_diff,
        ci_low,
        ci_high,
        cohens_d(a_scores, b_scores),
        paired_t_test(a_scores, b_scores),
    )


# ---------------------------------------------------------------------------
# Prompt config + variant-result loaders
# ---------------------------------------------------------------------------


def has_final_reply(result: EvaluationResult) -> bool:
    """True iff any iteration emitted a non-empty ResultMessage.result.

    Mirrors the evalboard rendering: a "final reply" is a text answer the
    agent produced that becomes the trailing entry in the Turn timeline.
    """
    for t in result.iterations:
        if t.result_summary is not None:
            r = t.result_summary.result
            if isinstance(r, str) and r.strip():
                return True
    return False


def visible_turn_count(result: EvaluationResult) -> int:
    """Count of agent actions visible in the timeline so far.

    A "turn" here is one entry rendered in the Turn timeline: each tool
    invocation contributes 1, plus 1 for the final assistant reply when
    present. This is the canonical metric — distinct from the SDK's
    ``num_turns`` which counts assistant *messages* and can bundle tool
    use with trailing text into a single turn.
    """
    commands = sum(len(t.commands) for t in result.iterations)
    return commands + (1 if has_final_reply(result) else 0)


def expected_turns_overage(result: EvaluationResult) -> tuple[int, int] | None:
    """Return ``(visible_turns, expected)`` when the visible-events turn
    count strictly exceeds ``run_limits.expected_turns``; else ``None``.

    Safe against missing ``task_config``, missing ``run_limits``, and
    non-int ``expected_turns`` values.
    """
    task_cfg = result.task_config
    if task_cfg is None:
        return None
    run_limits = (task_cfg.resolved or {}).get("run_limits") or {}
    if not isinstance(run_limits, dict):
        return None
    expected = run_limits.get("expected_turns")
    if not isinstance(expected, int) or expected < 1:
        return None
    actual = visible_turn_count(result)
    if actual > expected:
        return actual, expected
    return None


def describe_prompt_config(variant: ExperimentVariant) -> str:
    """Return a short description of the variant's prompt configuration.

    Returns strings like ``"(base prompt)"``, ``"(prompt override)"``, or
    ``"(2 mutations: prefix, suffix)"``.
    """
    if variant.initial_prompt is not None or variant.initial_prompt_file is not None:
        return "(prompt override)"
    if variant.prompt_mutations:
        type_names = [m.type for m in variant.prompt_mutations]
        return f"({len(type_names)} mutations: {', '.join(type_names)})"
    return "(base prompt)"


def load_variant_eval_results(
    run_dir: Path, variant_id: str, task_summaries: list[TaskExperimentSummary]
) -> list[EvaluationResult]:
    """Load EvaluationResult objects for a variant from disk.

    Walks all ``<run_dir>/<variant_id>/<task_id>/NN/task.json`` replicate
    subdirs for each task in ``task_summaries`` and returns every result that
    loads successfully.
    """
    variant_dir = run_dir / variant_id
    results: list[EvaluationResult] = []

    if not variant_dir.is_dir():
        return results

    for ts in task_summaries:
        task_dir = variant_dir / ts.task_id
        if not task_dir.is_dir():
            continue
        for rep_subdir in sorted(task_dir.glob("[0-9][0-9]")):
            task_json = rep_subdir / "task.json"
            if task_json.exists():
                try:
                    results.append(EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8")))
                except Exception:
                    logger.warning("Failed to load %s for variant report", task_json, exc_info=True)

    return results
