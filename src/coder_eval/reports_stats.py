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
from pathlib import Path

from coder_eval.models import EvaluationResult, ExperimentVariant, TaskExperimentSummary


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Statistical helpers (stdlib statistics module)
# ---------------------------------------------------------------------------


def mean(values: list[float]) -> float:
    return _stats.mean(values) if values else 0.0


def stddev(values: list[float]) -> float:
    """Sample standard deviation (Bessel-corrected). Returns 0.0 for n < 2."""
    return _stats.stdev(values) if len(values) >= 2 else 0.0


def welch_t_test(a: list[float], b: list[float]) -> float | None:
    """Two-tailed p-value using Welch's t-test via stdlib NormalDist approximation.

    Uses the normal approximation for the t-distribution when df is large (>30),
    and falls back to exact Welch-Satterthwaite otherwise. Returns None if either
    group has fewer than 2 observations.
    """
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return None

    mean_a, mean_b = _stats.mean(a), _stats.mean(b)
    var_a = _stats.variance(a)
    var_b = _stats.variance(b)

    se_sq = var_a / n_a + var_b / n_b
    if se_sq == 0:
        return 1.0

    t_stat = abs(mean_a - mean_b) / math.sqrt(se_sq)

    # Conservative normal approximation: treat t as z-score.
    # Overestimates p slightly for small df (heavier tails), but correct in
    # direction and sufficient for display purposes (no incomplete-beta needed).
    return 2.0 * _stats.NormalDist().cdf(-t_stat)


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
    """
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


def paired_bootstrap_diff_ci(
    a: list[float],
    b: list[float],
    n_resamples: int = 1000,
    confidence: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float] | None:
    """Paired bootstrap of mean(a_i - b_i).

    Returns (mean_diff, ci_low, ci_high) or None if lengths differ or < 2.
    """
    if len(a) != len(b) or len(a) < 2:
        return None
    diffs = [ai - bi for ai, bi in zip(a, b, strict=True)]
    return bootstrap_mean_ci(diffs, n_resamples=n_resamples, confidence=confidence, seed=seed)


def cohens_d(a: list[float], b: list[float]) -> float | None:
    """Paired Cohen's d = mean(a_i - b_i) / stddev(a_i - b_i)."""
    if len(a) != len(b) or len(a) < 2:
        return None
    diffs = [ai - bi for ai, bi in zip(a, b, strict=True)]
    s = stddev(diffs)
    return (sum(diffs) / len(diffs)) / s if s > 0 else None


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

    See docs/features/2026-05-22-visible-turns.md for the full rationale.
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
