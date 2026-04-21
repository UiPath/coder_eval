"""Shared statistical + prompt-config helpers for the markdown and HTML reporters.

Kept in a standalone module so ``reports_html`` can consume them without
importing ``reports_experiment`` (which in turn imports ``reports_html``
for its HTML-write helpers, and would otherwise form a cycle).
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from coder_eval.models import EvaluationResult, ExperimentVariant, TaskExperimentSummary


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Statistical helpers (no scipy dependency)
# ---------------------------------------------------------------------------


def mean(values: list[float]) -> float:
    """Compute arithmetic mean."""
    return sum(values) / len(values) if values else 0.0


def stddev(values: list[float]) -> float:
    """Compute sample standard deviation (Bessel-corrected)."""
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """Compute regularized incomplete beta function I_x(a, b).

    Uses the continued fraction expansion with Lentz's algorithm.
    Required for t-distribution p-value computation without scipy.
    """
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    # Use symmetry relation for better convergence
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _regularized_incomplete_beta(1.0 - x, b, a)

    ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - ln_beta) / a

    # Continued fraction via modified Lentz's method
    tiny = 1e-30
    c = 1.0
    d = 1.0 - (a + b) * x / (a + 1.0)
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    f = d

    for i in range(1, 200):
        m = i // 2
        if i % 2 == 0:
            num = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))

        d = 1.0 + num * d
        if abs(d) < tiny:
            d = tiny
        d = 1.0 / d

        c = 1.0 + num / c
        if abs(c) < tiny:
            c = tiny

        delta = d * c
        f *= delta

        if abs(delta - 1.0) < 1e-10:
            break

    return front * f


def welch_t_test(a: list[float], b: list[float]) -> float | None:
    """Compute two-tailed p-value using Welch's t-test.

    Returns None if either group has fewer than 2 observations.
    """
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return None

    mean_a, mean_b = mean(a), mean(b)
    var_a = sum((x - mean_a) ** 2 for x in a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (n_b - 1)

    se_sq = var_a / n_a + var_b / n_b
    if se_sq == 0:
        return 1.0

    t_stat = abs(mean_a - mean_b) / math.sqrt(se_sq)

    # Welch-Satterthwaite degrees of freedom
    num = se_sq**2
    den = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = num / den if den > 0 else 1.0

    # Two-tailed p-value: P(|T| > t) = I_{df/(df+t²)}(df/2, 1/2)
    x = df / (df + t_stat * t_stat)
    return _regularized_incomplete_beta(x, df / 2.0, 0.5)


def fmt_mean_sd(values: list[float], fmt: str = ".3f") -> str:
    """Format mean ± stddev string."""
    if not values:
        return "N/A"
    m = mean(values)
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
# Prompt config + variant-result loaders
# ---------------------------------------------------------------------------


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

    Scans ``<run_dir>/<variant_id>/<task_id>/task.json`` for each task in
    ``task_summaries`` and returns every result that loads successfully.
    """
    variant_dir = run_dir / variant_id
    results: list[EvaluationResult] = []

    if not variant_dir.is_dir():
        return results

    for ts in task_summaries:
        task_json = variant_dir / ts.task_id / "task.json"
        if task_json.exists():
            try:
                results.append(EvaluationResult.model_validate_json(task_json.read_text()))
            except Exception:
                logger.warning("Failed to load %s for variant report", task_json, exc_info=True)

    return results
