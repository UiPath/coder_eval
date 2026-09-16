"""Report-shaped helpers over variant and experiment results.

What is left here after the split is presentation and report assembly: the
variant/experiment series collectors, the paired-comparison summary, and the
formatters that turn a number into a cell (``fmt_mean_sd``, ``fmt_p``,
``format_score``). The numeric core moved to ``coder_eval.stats`` and the
``EvaluationResult`` metrics to ``coder_eval.result_metrics``.

**The cycle rationale is live, not historical.** These helpers stay in a module
of their own so ``reports.html`` can consume them without importing
``reports.experiment`` — which imports ``reports.html`` for its HTML-write
helpers. Folding this module into the experiment reporter would close
``experiment -> html -> helpers`` into a cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

from ..models import (
    EvaluationResult,
    ExperimentResult,
    ExperimentVariant,
    TaskExperimentSummary,
)
from ..path_utils import TASK_JSON_FILENAME
from ..stats import cohens_d, mean, paired_t_ci, paired_t_test, stddev


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Display formatters (presentation, not computation — the statistics are in
# coder_eval.stats)
# ---------------------------------------------------------------------------


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
# Aggregate-metric series
# ---------------------------------------------------------------------------


class VariantSeries(NamedTuple):
    """One variant's numeric series across all tasks — the raw inputs to the
    Aggregate Metrics rows and their p-values."""

    scores: list[float]
    durations: list[float]
    tokens: list[float]
    asst_turns: list[float]


# environment_info keys the Environment table must NOT render as ordinary rows.
# `installed_tools` has its own dedicated section; the rest are harness
# bookkeeping the reader did not ask for — `command_base_path` is a full PATH
# string on every row, and the graded_by_* provenance keys only appear on a
# re-graded row where they would read as facts about the run itself.
ENV_TABLE_EXCLUDE = frozenset({"installed_tools", "command_base_path", "reference_digest", "harness_contract"})


def is_env_table_key(key: str) -> bool:
    """Whether ``key`` belongs in a rendered Environment table."""
    return key not in ENV_TABLE_EXCLUDE and not key.startswith("graded_by_")


# What an ungraded row shows where a score would go. Deliberately not "0.000":
# an ungraded task was never measured, and a zero is indistinguishable from a
# task that was measured and scored nothing.
UNGRADED_SCORE_TEXT = "n/a"


def format_score(score: float | None) -> str:
    """Render a weighted score for a report table, or ``n/a`` when ungraded."""
    return UNGRADED_SCORE_TEXT if score is None else f"{score:.3f}"


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
            # Only the SCORE is dropped when there is none — never the row.
            # Duration, tokens and assistant turns are facts about the run that
            # grading has nothing to do with, and `execute`'s stated contract is
            # that only the verdict is withheld. Skipping the row whole made an
            # all-ungraded experiment render `Avg Duration | N/A | N/A` with the
            # Tokens and Assistant Turns rows absent entirely.
            #
            # The series are consumed independently (each statistic reads one
            # list), so they need not be index-aligned with each other;
            # `paired_comparison` pairs across VARIANTS by task id, not by index
            # into these lists. An earlier note here claimed an experiment is
            # either entirely graded or entirely ungraded because `grade` is
            # run-level — `run --resume` grades rows independently and folds a
            # failed one back ungraded, so mixed experiments are real.
            if vr.weighted_score is not None:
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
            task_json = rep_subdir / TASK_JSON_FILENAME
            if task_json.exists():
                try:
                    results.append(EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8")))
                except Exception:
                    logger.warning("Failed to load %s for variant report", task_json, exc_info=True)

    return results
