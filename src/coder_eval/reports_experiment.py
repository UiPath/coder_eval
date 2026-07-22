"""Report generation for experiment results (cross-variant and experiment-level)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from coder_eval.models import (
    EvaluationResult,
    ExperimentDefinition,
    ExperimentResult,
    TaskExperimentSummary,
)
from coder_eval.path_utils import replicate_subdir_name
from coder_eval.reports import resolve_agent_settings
from coder_eval.reports_stats import (
    bootstrap_mean_ci,
    cohens_d,
    describe_prompt_config,
    fmt_mean_sd,
    fmt_p,
    load_variant_eval_results,
    mean,
    paired_bootstrap_diff_ci,
    paired_t_test,
    stddev,
    welch_t_test,
    wilson_interval,
)


logger = logging.getLogger(__name__)

# Default pass_threshold from BaseSuccessCriterion — used for Wilson pass-rate in replicate stats.
_REPLICATE_PASS_THRESHOLD = 0.9


# ---------------------------------------------------------------------------
# Helper: build task_result dict from EvaluationResult (for variant reports)
# ---------------------------------------------------------------------------


def eval_result_to_task_dict(
    result: EvaluationResult,
    *,
    variant_id: str | None = None,
    tags: list[str] | None = None,
    task_path: str | None = None,
    duration_override: float | None = None,
    replicate_index: int | None = None,
) -> dict[str, Any]:
    """Convert an EvaluationResult to the task_result dict format used by ReportGenerator.

    Args:
        result: The evaluation result to convert.
        variant_id: Optional variant ID to include in the dict.
        tags: Optional tags list (defaults to []).
        task_path: Optional path of the task YAML (as supplied to the runner) —
            lets downstream consumers (evalboard) derive groupings like skill
            from the source folder structure instead of guessing from tags.
        duration_override: Optional duration value (defaults to result.duration_seconds).
        replicate_index: Replicate index of this row (the ``<variant>/<task>/<NN>``
            sub-dir). Repeated runs of the same task share a ``task_id``, so
            without this the row is indistinguishable from its siblings and
            downstream consumers (evalboard) collapse them to one. ``None`` when
            the caller doesn't track replicates (repeats disabled / legacy).
    """
    from coder_eval.reports_stats import expected_turns_overage, visible_turn_count
    from coder_eval.reports_stats import has_final_reply as _has_final_reply

    ref_similarity: float | None = None
    for cr in result.success_criteria_results:
        if cr.criterion_type == "reference_comparison":
            ref_similarity = cr.score
            break

    overage = expected_turns_overage(result)

    total_turns = sum((t.num_turns or 0) for t in result.iterations)

    # Whether the agent emitted a text reply (becomes the trailing entry
    # in the Turn timeline). Carried as a row-level boolean so evalboard
    # grid/trends can compute the visible turn count without re-reading
    # per-task content.
    has_reply = _has_final_reply(result)

    expected_turns_value: int | None = None
    if result.task_config is not None:
        rl = (result.task_config.resolved or {}).get("run_limits") or {}
        if isinstance(rl, dict):
            raw = rl.get("expected_turns")
            if isinstance(raw, int) and raw >= 1:
                expected_turns_value = raw

    d: dict[str, Any] = {
        "task_id": result.task_id,
        "replicate_index": replicate_index,
        "status": result.final_status,
        "weighted_score": result.weighted_score,
        "duration": duration_override if duration_override is not None else result.duration_seconds,
        "iteration_count": result.iteration_count,
        "tags": tags if tags is not None else [],
        "task_path": task_path,
        "iterations": [
            {
                "iteration": t.iteration,
                "duration_seconds": t.duration_seconds,
                "command_count": len(t.commands),
                "assistant_turn_count": t.assistant_turn_count,
                "crashed": t.crashed,
                "crash_reason": t.crash_reason,
            }
            for t in result.iterations
        ],
        "model_used": result.model_used,
        "reference_similarity": ref_similarity,
        "input_tokens": (result.total_token_usage.uncached_input_tokens if result.total_token_usage else None),
        "output_tokens": (result.total_token_usage.output_tokens if result.total_token_usage else None),
        "cache_creation_input_tokens": (
            result.total_token_usage.cache_creation_input_tokens if result.total_token_usage else None
        ),
        "cache_read_input_tokens": (
            result.total_token_usage.cache_read_input_tokens if result.total_token_usage else None
        ),
        "total_tokens": (result.total_token_usage.total_tokens if result.total_token_usage else None),
        "total_cost_usd": (result.total_token_usage.total_cost_usd if result.total_token_usage else None),
        "expected_commands": result.expected_commands,
        "actual_commands": result.actual_commands,
        "commands_efficiency": result.commands_efficiency,
        "agent_config": (result.agent_config.model_dump() if result.agent_config else None),
        "sdk_options": result.sdk_options,
        "installed_tools": result.environment_info.get("installed_tools"),
        "max_turns_exhausted": result.max_turns_exhausted,
        "expected_turns_overage": list(overage) if overage is not None else None,
        "total_turns": total_turns,
        # Documented "visible turns" (tool calls + final reply) — the canonical
        # turn count the run-level "within expected turns" metric compares against
        # expected_turns. Distinct from total_turns (SDK num_turns).
        "visible_turns": visible_turn_count(result),
        "expected_turns": expected_turns_value,
        "has_final_reply": has_reply,
        # Early-stop surfaces (opt-in run_limits.stop_early). None/False on the
        # default path so downstream analysis never confuses a truncated run
        # with a full one.
        "stopped_early": result.early_stop is not None,
        "early_stop_reason": (result.early_stop.reason.value if result.early_stop is not None else None),
        "turns_remaining_at_stop": (
            result.early_stop.turns_remaining_at_stop if result.early_stop is not None else None
        ),
    }
    d["variant_id"] = variant_id
    return d


class ExperimentReportGenerator:
    """Generates markdown reports for experiment results."""

    @staticmethod
    def generate_task_report(summary: TaskExperimentSummary) -> str:
        """Generate task-report content for a single task's cross-variant comparison.

        Args:
            summary: Cross-variant summary for one task.

        Returns:
            Markdown string.
        """
        lines = [
            f"# Task Report: {summary.task_id}",
            "",
            f"**Best variant**: {summary.best_variant}",
            f"**Score spread**: {summary.score_spread:.3f}",
            "",
            "## Variant Comparison",
            "",
            "| Variant | Score | Status | Avg Duration | Tokens |",
            "|---------|-------|--------|--------------|--------|",
        ]

        for v in summary.variant_results:
            tokens_str = f"{v.total_tokens:,}" if v.total_tokens is not None else "N/A"
            avg_dur = v.duration_seconds / v.replicate_count
            lines.append(
                f"| {v.variant_id} | {v.weighted_score:.3f} | {v.final_status}" + f" | {avg_dur:.1f}s | {tokens_str} |"
            )

        return "\n".join(lines)

    @staticmethod
    def _experiment_header_lines(result: ExperimentResult) -> list[str]:
        """Title + Description / Variants / Total Duration. First block (no leading blank)."""
        return [
            f"# Experiment Report: {result.experiment_id}",
            "",
            f"**Description**: {result.description}",
            f"**Variants**: {', '.join(result.variant_ids)}",
            f"**Total Duration**: {result.total_duration_seconds:.1f}s",
        ]

    @staticmethod
    def _prompt_config_lines(result: ExperimentResult, experiment: ExperimentDefinition | None) -> list[str]:
        """The ``## Prompt Configuration`` block. Returns ``[]`` when there is no
        experiment definition or no variant carries prompt config (both guards preserved)."""
        # ── Variant prompt configuration (if experiment definition available) ──
        if experiment is None:
            return []
        variant_map = {v.variant_id: v for v in experiment.variants}
        has_prompt_config = bool(experiment.defaults and experiment.defaults.prompt_mutations) or any(
            v.prompt_mutations or v.initial_prompt or v.initial_prompt_file for v in experiment.variants
        )
        if not has_prompt_config:
            return []
        lines = ["", "## Prompt Configuration", ""]
        for vid in result.variant_ids:
            v = variant_map.get(vid)
            desc = describe_prompt_config(v) if v else "(unknown)"
            lines.append(f"- **{vid}**: {desc}")
        return lines

    @staticmethod
    def _collect_variant_series(
        result: ExperimentResult,
    ) -> tuple[dict[str, list[float]], dict[str, list[float]], dict[str, list[float]], dict[str, list[float]]]:
        """Per-variant (scores, durations, tokens, assistant-turns) series collected
        across all task summaries — the raw inputs to the Aggregate Metrics stat rows."""
        variant_scores: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}
        variant_durations: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}
        variant_tokens: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}
        variant_asst_turns: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}

        for ts in result.task_summaries:
            for vr in ts.variant_results:
                variant_scores[vr.variant_id].append(vr.weighted_score)
                variant_durations[vr.variant_id].append(vr.duration_seconds / vr.replicate_count)
                if vr.total_tokens is not None:
                    variant_tokens[vr.variant_id].append(float(vr.total_tokens))
                if vr.total_assistant_turns is not None:
                    variant_asst_turns[vr.variant_id].append(float(vr.total_assistant_turns))
        return variant_scores, variant_durations, variant_tokens, variant_asst_turns

    @staticmethod
    def _aggregate_count_rows(result: ExperimentResult, show_p_values: bool) -> list[str]:
        """The integer-aggregate rows of the Aggregate Metrics table: Tasks Run,
        Succeeded, Failed, the optional budget sub-rows, Errors, and Success Rate.
        Each row appends ``" | —"`` in the p-value column when ``show_p_values``."""
        lines: list[str] = []

        # Row: Tasks Run (count, no stddev)
        row = "| Tasks Run"
        for vid in result.variant_ids:
            agg = result.variant_aggregates[vid]
            row += f" | {agg.tasks_run}"
        if show_p_values:
            row += " | —"
        lines.append(row + " |")

        # Row: Succeeded
        row = "| Succeeded"
        for vid in result.variant_ids:
            agg = result.variant_aggregates[vid]
            row += f" | {agg.tasks_succeeded}"
        if show_p_values:
            row += " | —"
        lines.append(row + " |")

        # Row: Failed
        row = "| Failed"
        for vid in result.variant_ids:
            agg = result.variant_aggregates[vid]
            row += f" | {agg.tasks_failed}"
        if show_p_values:
            row += " | —"
        lines.append(row + " |")

        # Optional sub-rows: only rendered when at least one variant has budget-exceeded tasks.
        if any(result.variant_aggregates[vid].tasks_token_budget_exceeded > 0 for vid in result.variant_ids):
            row = "| - Token budget"
            for vid in result.variant_ids:
                row += f" | {result.variant_aggregates[vid].tasks_token_budget_exceeded}"
            if show_p_values:
                row += " | —"
            lines.append(row + " |")
        if any(result.variant_aggregates[vid].tasks_cost_budget_exceeded > 0 for vid in result.variant_ids):
            row = "| - Cost budget"
            for vid in result.variant_ids:
                row += f" | {result.variant_aggregates[vid].tasks_cost_budget_exceeded}"
            if show_p_values:
                row += " | —"
            lines.append(row + " |")

        # Row: Errors
        row = "| Errors"
        for vid in result.variant_ids:
            agg = result.variant_aggregates[vid]
            row += f" | {agg.tasks_error}"
        if show_p_values:
            row += " | —"
        lines.append(row + " |")

        # Row: Success Rate (errors excluded from denominator — they're infrastructure failures, not task failures)
        row = "| Success Rate"
        for vid in result.variant_ids:
            agg = result.variant_aggregates[vid]
            evaluable = agg.tasks_run - agg.tasks_error
            rate = (agg.tasks_succeeded / evaluable * 100) if evaluable > 0 else 0
            row += f" | {rate:.1f}%"
        if show_p_values:
            row += " | —"
        lines.append(row + " |")

        return lines

    @staticmethod
    def _aggregate_stat_rows(
        result: ExperimentResult,
        variant_scores: dict[str, list[float]],
        variant_durations: dict[str, list[float]],
        variant_tokens: dict[str, list[float]],
        variant_asst_turns: dict[str, list[float]],
        show_p_values: bool,
        vid_a: str,
        vid_b: str,
    ) -> list[str]:
        """The mean ± stddev rows of the Aggregate Metrics table (Score, Avg Duration,
        and the optional Assistant Turns / Tokens rows), each with a Welch t-test
        p-value when ``show_p_values``."""
        lines: list[str] = []

        # Row: Score (mean ± stddev, p-value)
        row = "| Score"
        for vid in result.variant_ids:
            row += f" | {fmt_mean_sd(variant_scores[vid])}"
        if show_p_values:
            p = welch_t_test(variant_scores[vid_a], variant_scores[vid_b])
            row += f" | {fmt_p(p)}"
        lines.append(row + " |")

        # Row: Duration
        row = "| Avg Duration (s)"
        for vid in result.variant_ids:
            row += f" | {fmt_mean_sd(variant_durations[vid], '.1f')}"
        if show_p_values:
            p = welch_t_test(variant_durations[vid_a], variant_durations[vid_b])
            row += f" | {fmt_p(p)}"
        lines.append(row + " |")

        # Row: Assistant Turns (if data available)
        if any(variant_asst_turns[vid] for vid in result.variant_ids):
            row = "| Assistant Turns"
            for vid in result.variant_ids:
                row += f" | {fmt_mean_sd(variant_asst_turns[vid], '.1f')}"
            if show_p_values:
                p = welch_t_test(variant_asst_turns[vid_a], variant_asst_turns[vid_b])
                row += f" | {fmt_p(p)}"
            lines.append(row + " |")

        # Row: Tokens (if data available)
        if any(variant_tokens[vid] for vid in result.variant_ids):
            row = "| Tokens"
            for vid in result.variant_ids:
                row += f" | {fmt_mean_sd(variant_tokens[vid], ',.0f')}"
            if show_p_values:
                p = welch_t_test(variant_tokens[vid_a], variant_tokens[vid_b])
                row += f" | {fmt_p(p)}"
            lines.append(row + " |")

        return lines

    @staticmethod
    def _aggregate_metrics_lines(result: ExperimentResult) -> list[str]:
        """The ``## Aggregate Metrics`` vertical table (metrics as rows, variants as
        columns). The p-value column + Welch t-tests appear only for exactly 2 variants;
        ``vid_a``/``vid_b`` stay local so the 3+-variant path never indexes them."""
        # ── Aggregate Metrics (vertical: metrics as rows, variants as columns) ──
        variant_scores, variant_durations, variant_tokens, variant_asst_turns = (
            ExperimentReportGenerator._collect_variant_series(result)
        )

        show_p_values = len(result.variant_ids) == 2
        vid_a, vid_b = (result.variant_ids[0], result.variant_ids[1]) if show_p_values else ("", "")

        # Build header
        header = "| Metric | " + " | ".join(result.variant_ids)
        sep = "|--------|" + "|".join("--------" for _ in result.variant_ids)
        if show_p_values:
            header += " | p-value"
            sep += "|--------"
        header += " |"
        sep += "|"

        lines = ["", "## Aggregate Metrics", "", header, sep]
        lines += ExperimentReportGenerator._aggregate_count_rows(result, show_p_values)
        lines += ExperimentReportGenerator._aggregate_stat_rows(
            result, variant_scores, variant_durations, variant_tokens, variant_asst_turns, show_p_values, vid_a, vid_b
        )

        # Row: Replicates/task (if any variant ran >1 replicate)
        if any(result.variant_aggregates[vid].replicate_count > 1 for vid in result.variant_ids):
            row = "| Replicates/task"
            for vid in result.variant_ids:
                agg = result.variant_aggregates[vid]
                row += f" | {agg.replicate_count}"
            if show_p_values:
                row += " | —"
            lines.append(row + " |")

        return lines

    @staticmethod
    def _win_loss_lines(result: ExperimentResult) -> list[str]:
        """The ``## Win Rates`` + ``## Per-Task Comparison`` + ``## Most Divergent Tasks``
        block. Returns ``[]`` when there are no task summaries."""
        # ── Win/loss/tie analysis ──
        if not result.task_summaries:
            return []
        lines = ["", "## Win Rates", ""]
        win_counts: dict[str, int] = {vid: 0 for vid in result.variant_ids}
        tie_count = 0
        for ts in result.task_summaries:
            if ts.is_tie:
                tie_count += 1
            else:
                win_counts[ts.best_variant] = win_counts.get(ts.best_variant, 0) + 1
        total_tasks = len(result.task_summaries)
        for vid in result.variant_ids:
            wins = win_counts.get(vid, 0)
            lines.append(f"- **{vid}**: {wins}/{total_tasks} tasks ({wins / total_tasks * 100:.0f}%)")
        if tie_count > 0:
            lines.append(f"- **Ties**: {tie_count}/{total_tasks} tasks ({tie_count / total_tasks * 100:.0f}%)")

        # ── Per-task detailed comparison ──
        show_reps = any(ts.replicate_count > 1 for ts in result.task_summaries)
        lines.extend(["", "## Per-Task Comparison", ""])
        header = "| Task | " + " | ".join(result.variant_ids) + " | Best | Spread |"
        sep = "|------|" + "|".join("------" for _ in result.variant_ids) + "|------|--------|"
        if show_reps:
            header += " Reps |"
            sep += "------|"
        lines.append(header)
        lines.append(sep)

        for ts in result.task_summaries:
            scores_by_variant = {vr.variant_id: vr for vr in ts.variant_results}
            cells = []
            for vid in result.variant_ids:
                vr = scores_by_variant.get(vid)
                if vr:
                    status_icon = vr.final_status.icon
                    cells.append(f"{vr.weighted_score:.3f} ({status_icon})")
                else:
                    cells.append("N/A")
            best_str = f"{'TIE' if ts.is_tie else ts.best_variant}"
            row = f"| {ts.task_id} | " + " | ".join(cells) + f" | {best_str} | {ts.score_spread:.3f} |"
            if show_reps:
                row += f" {ts.replicate_count} |"
            lines.append(row)

        # ── Highest divergence ──
        sorted_tasks = sorted(result.task_summaries, key=lambda t: t.score_spread, reverse=True)
        if sorted_tasks and sorted_tasks[0].score_spread > 0:
            lines.extend(["", "## Most Divergent Tasks", ""])
            for ts in sorted_tasks[:5]:
                lines.append(f"- **{ts.task_id}**: spread={ts.score_spread:.3f}, best={ts.best_variant}")
        return lines

    @staticmethod
    def _replicate_stats_lines(result: ExperimentResult) -> list[str]:
        """The ``## Replicate Statistics`` block: per-variant bootstrap-CI / Wilson
        pass-rate table. Returns ``[]`` when no variant ran more than one replicate."""
        # ── Replicate Statistics (only when any variant ran >1 replicate) ──
        if not any(ts.replicate_count > 1 for ts in result.task_summaries):
            return []
        lines = ["", "## Replicate Statistics", ""]

        # Per-variant bootstrap CI + Wilson pass-rate table
        lines.append("| Variant | Replicates/task | Mean score | 95% CI | Pass-rate (Wilson 95%) |")
        lines.append("|---------|-----------------|------------|--------|------------------------|")
        for vid in result.variant_ids:
            per_rep = result.per_replicate_scores.get(vid, {})
            all_scores: list[float] = [s for scores in per_rep.values() for s in scores]
            passes = sum(1 for s in all_scores if s >= _REPLICATE_PASS_THRESHOLD)
            m, lo, hi = bootstrap_mean_ci(all_scores)
            wlo, whi = wilson_interval(passes, len(all_scores))
            agg = result.variant_aggregates.get(vid)
            rep_count = agg.replicate_count if agg else 1
            lines.append(
                f"| {vid} | {rep_count} | {m:.3f} | [{lo:.3f}, {hi:.3f}]"
                + f" | {passes}/{len(all_scores)} [{wlo:.2f}, {whi:.2f}] |"
            )

        return lines

    @staticmethod
    def _paired_comparison_lines(result: ExperimentResult) -> list[str]:
        """The ``## Paired Comparison`` block for 2-variant experiments.

        Pairs the two variants' *per-task mean* scores, which removes between-task
        difficulty variance and is therefore more powerful than the pooled Welch
        test in ``## Aggregate Metrics``. The task is the unit of analysis: replicate
        slots within a task share the task effect and are not independent, so
        pairing them individually would understate the standard error. Unlike
        ``## Replicate Statistics`` this has no replicate gate — single-replicate
        experiments pair task-by-task. Returns ``[]`` when there is nothing to pair.
        """
        if len(result.variant_ids) != 2:
            return []
        vid_a, vid_b = result.variant_ids[0], result.variant_ids[1]
        per_rep_a = result.per_replicate_scores.get(vid_a, {})
        per_rep_b = result.per_replicate_scores.get(vid_b, {})
        common_tasks = sorted(set(per_rep_a) & set(per_rep_b))
        a_scores: list[float] = []
        b_scores: list[float] = []
        skipped_tasks: list[str] = []
        for task_id in common_tasks:
            rep_a = per_rep_a[task_id]
            rep_b = per_rep_b[task_id]
            if len(rep_a) == len(rep_b):
                a_scores.append(mean(rep_a))
                b_scores.append(mean(rep_b))
            else:
                skipped_tasks.append(task_id)

        if len(a_scores) < 2:
            if skipped_tasks:
                return [
                    "",
                    "## Paired Comparison",
                    "",
                    f"*Paired statistics skipped — unequal replicate counts between {vid_a} and {vid_b}.*",
                ]
            # Nothing to pair: 0/1 common tasks, or per_replicate_scores absent (old results).
            return []

        # Equal lengths and >=2 pairs are guaranteed above, so this never returns None.
        diff = paired_bootstrap_diff_ci(a_scores, b_scores)
        if diff is None:  # pragma: no cover - defensive
            return []
        mean_diff, d_lo, d_hi = diff
        d_val = cohens_d(a_scores, b_scores)
        d_str = f"{d_val:.2f}" if d_val is not None else "n/a"
        suffix = f" ({len(skipped_tasks)} task(s) excluded: unequal replicate counts)" if skipped_tasks else ""
        return [
            "",
            "## Paired Comparison",
            "",
            f"*Paired over the per-task mean score of {len(a_scores)} task(s) common to both variants"
            + " — removes between-task variance; more powerful than the pooled Welch test above.*",
            f"**Paired mean diff ({vid_a} - {vid_b})**: {mean_diff:+.3f}"
            + f" [95% CI {d_lo:+.3f}, {d_hi:+.3f}], Cohen's d = {d_str}"
            + f", p = {fmt_p(paired_t_test(a_scores, b_scores))}{suffix}",
        ]

    @staticmethod
    def generate_experiment_report(
        result: ExperimentResult,
        experiment: ExperimentDefinition | None = None,
    ) -> str:
        """Generate experiment-report content for the full experiment.

        Produces a vertical "Aggregate Metrics" table (metrics as rows, variants
        as columns) with mean ± stddev and Welch's t-test p-values.

        Args:
            result: Complete experiment result.
            experiment: Optional experiment definition (enables prompt config display).

        Returns:
            Markdown string.
        """
        lines = ExperimentReportGenerator._experiment_header_lines(result)
        lines += ExperimentReportGenerator._prompt_config_lines(result, experiment)
        lines += ExperimentReportGenerator._aggregate_metrics_lines(result)
        lines += ExperimentReportGenerator._win_loss_lines(result)
        lines += ExperimentReportGenerator._replicate_stats_lines(result)
        lines += ExperimentReportGenerator._paired_comparison_lines(result)
        return "\n".join(lines)

    @staticmethod
    def generate_variant_report(variant_id: str, result: ExperimentResult, run_dir: Path | None = None) -> str:
        """Generate a comprehensive variant report matching run-report.md format.

        When run_dir is provided, loads full EvaluationResult data from disk to
        include generation metrics, token usage, command telemetry, agent settings,
        and environment information.

        Args:
            variant_id: The variant to generate the report for.
            result: Complete experiment result.
            run_dir: Top-level run directory (enables rich report sections).

        Returns:
            Markdown string.
        """
        from coder_eval.reports import ReportGenerator

        agg = result.variant_aggregates[variant_id]
        evaluable = agg.tasks_run - agg.tasks_error
        success_rate = (agg.tasks_succeeded / evaluable * 100) if evaluable > 0 else 0
        tokens_str = f"{agg.total_tokens:,}" if agg.total_tokens is not None else "N/A"

        failed_line = f"- **Failed**: {agg.tasks_failed}"
        if agg.tasks_token_budget_exceeded or agg.tasks_cost_budget_exceeded:
            failed_line += (
                f" (incl. {agg.tasks_token_budget_exceeded} token budget, "
                f"{agg.tasks_cost_budget_exceeded} cost budget exceeded)"
            )

        lines = [
            f"# Variant Report: {variant_id}",
            "",
            f"**Experiment**: {result.experiment_id}",
            f"**Description**: {result.description}",
            "",
            "## Summary",
            "",
            f"- **Tasks Run**: {agg.tasks_run}",
            f"- **Succeeded**: {agg.tasks_succeeded}",
            failed_line,
            f"- **Errors**: {agg.tasks_error}",
            f"- **Success Rate**: {success_rate:.1f}%",
            f"- **Average Score**: {agg.average_score:.3f}",
            f"- **Average Duration**: {agg.average_duration:.1f}s",
            f"- **Total Tokens**: {tokens_str}",
        ]

        # Collect per-task variant results for stddev metrics
        variant_results = [
            vr for ts in result.task_summaries for vr in ts.variant_results if vr.variant_id == variant_id
        ]
        scores = [vr.weighted_score for vr in variant_results]
        durations = [vr.duration_seconds / vr.replicate_count for vr in variant_results]

        if scores and len(scores) >= 2:
            lines.append(f"- **Score Stddev**: {stddev(scores):.3f}")
        if durations and len(durations) >= 2:
            lines.append(f"- **Duration Stddev**: {stddev(durations):.1f}s")
        if agg.replicate_count > 1:
            per_rep = result.per_replicate_scores.get(variant_id, {})
            all_rep_scores: list[float] = [s for rep_scores in per_rep.values() for s in rep_scores]
            if all_rep_scores:
                _, lo, hi = bootstrap_mean_ci(all_rep_scores)
                lines.append(f"- **Replicates/task**: {agg.replicate_count}")
                lines.append(f"- **Score 95% CI**: [{lo:.3f}, {hi:.3f}] (bootstrap over {len(all_rep_scores)} samples)")

        # Task Details table
        has_similarity = any(vr.reference_similarity is not None for vr in variant_results)
        has_reps = any(vr.replicate_count > 1 for vr in variant_results)

        header = "| Task | Score | Status | Avg Duration |"
        separator = "|------|-------|--------|--------------|"
        if has_reps:
            header += " Reps |"
            separator += "------|"
        if has_similarity:
            header += " Similarity |"
            separator += "------------|"

        lines.extend(["", "## Task Details", "", header, separator])

        for ts in result.task_summaries:
            for vr in ts.variant_results:
                if vr.variant_id == variant_id:
                    avg_duration = vr.duration_seconds / vr.replicate_count
                    row = f"| {ts.task_id} | {vr.weighted_score:.3f} | {vr.final_status} | {avg_duration:.1f}s |"
                    if has_reps:
                        row += f" {vr.replicate_count} |"
                    if has_similarity:
                        sim_str = f"{vr.reference_similarity:.3f}" if vr.reference_similarity is not None else "N/A"
                        row += f" {sim_str} |"
                    lines.append(row)

        # ── Rich sections from EvaluationResult data (when run_dir available) ──
        if run_dir:
            eval_results = load_variant_eval_results(run_dir, variant_id, result.task_summaries)
            if eval_results:
                task_dicts = [eval_result_to_task_dict(er) for er in eval_results]

                # Generation Metrics
                if any(d.get("iterations") for d in task_dicts):
                    lines.extend(["", ""])
                    lines.extend(ReportGenerator._generate_generation_metrics_section(task_dicts))

                # Token Usage
                token_lines = ReportGenerator._generate_token_usage_section(task_dicts)
                if token_lines:
                    lines.extend(["", ""])
                    lines.extend(token_lines)

                # Command Telemetry (aggregate from variant dir)
                variant_dir = run_dir / variant_id
                aggregated_stats = ReportGenerator._aggregate_command_statistics(variant_dir)
                if aggregated_stats and aggregated_stats.total_commands > 0:
                    lines.extend(["", ""])
                    lines.extend(ReportGenerator._generate_command_statistics_section(aggregated_stats))

                # Agent Settings (from first task with data)
                settings_source, is_sdk = resolve_agent_settings(task_dicts)
                if settings_source:
                    lines.append("")
                    lines.extend(ReportGenerator._generate_agent_settings_section(settings_source, is_sdk))

                # Installed Tools
                installed_tools_lines = ReportGenerator._generate_installed_tools_section(task_dicts)
                if installed_tools_lines:
                    lines.extend([""])
                    lines.extend(installed_tools_lines)

                # Environment (from first result with data)
                for er in eval_results:
                    if er.environment_info:
                        env = {k: v for k, v in er.environment_info.items() if k != "installed_tools"}
                        if env:
                            lines.extend(["", "## Environment", ""])
                            for key, value in env.items():
                                lines.append(f"- **{key}**: {value}")
                            break

        return "\n".join(lines)

    @staticmethod
    def write_reports(
        result: ExperimentResult,
        run_dir: Path,
        experiment: ExperimentDefinition | None = None,
    ) -> None:
        """Write all experiment reports to disk.

        Creates:
            - <run_dir>/experiment.md          (cross-variant comparison)
            - <run_dir>/experiment.json         (full ExperimentResult)
            - <run_dir>/<variant_id>/variant.md  (per-variant aggregate)
            - <run_dir>/<variant_id>/variant.json

        Args:
            result: Complete experiment result.
            run_dir: Top-level run directory.
            experiment: Optional experiment definition (enables prompt config in reports).
        """
        run_dir.mkdir(parents=True, exist_ok=True)

        # Experiment-level reports at run root
        exp_report = ExperimentReportGenerator.generate_experiment_report(result, experiment=experiment)
        (run_dir / "experiment.md").write_text(exp_report, encoding="utf-8")
        (run_dir / "experiment.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")

        # Per-variant reports
        for vid in result.variant_ids:
            variant_dir = run_dir / vid
            variant_dir.mkdir(parents=True, exist_ok=True)

            agg = result.variant_aggregates.get(vid)
            if agg:
                variant_report = ExperimentReportGenerator.generate_variant_report(vid, result, run_dir=run_dir)
                (variant_dir / "variant.md").write_text(variant_report, encoding="utf-8")
                (variant_dir / "variant.json").write_text(agg.model_dump_json(indent=2), encoding="utf-8")

        # HTML reports — each write is wrapped by ``safe_write`` so a render
        # bug in one report cannot mask the run outcome.
        from .reports_html import write_experiment_html, write_variant_html

        # Build per-variant task link tables from task_summaries. Every
        # variant_id in task_summaries is guaranteed to appear in
        # ``result.variant_ids`` (the aggregator constructs them from the same
        # source), so we pre-seed the dict with all known variants and extend.
        task_links_by_variant: dict[str, list[tuple[str, str, float | None, str]]] = {
            vid: [] for vid in result.variant_ids
        }
        for summary in result.task_summaries:
            for vr in summary.variant_results:
                rel_link = f"{vr.task_id}/{replicate_subdir_name(vr.replicate_index)}/task.html"
                task_links_by_variant[vr.variant_id].append(
                    (vr.task_id, rel_link, vr.weighted_score, vr.final_status.value)
                )

        for vid in result.variant_ids:
            agg = result.variant_aggregates.get(vid)
            if agg is None:
                continue
            write_variant_html(
                vid,
                agg,
                task_links_by_variant.get(vid, []),
                run_dir / vid / "variant.html",
                result=result,
                run_dir=run_dir,
            )

        write_experiment_html(
            result,
            experiment,
            [(v, f"{v}/variant.html") for v in result.variant_ids],
            run_dir / "experiment.html",
        )
