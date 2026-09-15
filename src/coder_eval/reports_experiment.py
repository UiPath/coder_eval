"""Report generation for experiment results (cross-variant and experiment-level)."""

from __future__ import annotations

import logging
from pathlib import Path

from coder_eval.models import (
    ExperimentDefinition,
    ExperimentResult,
    TaskExperimentSummary,
)
from coder_eval.path_utils import replicate_subdir_name
from coder_eval.reports import ReportGenerator, resolve_agent_settings
from coder_eval.reports_html import write_experiment_html, write_variant_html
from coder_eval.reports_stats import (
    VariantSeries,
    collect_variant_series,
    describe_prompt_config,
    fmt_mean_sd,
    fmt_p,
    format_score,
    is_env_table_key,
    load_variant_eval_results,
    paired_comparison,
)
from coder_eval.run_record import eval_result_to_task_dict
from coder_eval.stats import bootstrap_mean_ci, stddev, welch_t_test, wilson_interval


logger = logging.getLogger(__name__)

# Default pass_threshold from BaseSuccessCriterion — used for Wilson pass-rate in replicate stats.
_REPLICATE_PASS_THRESHOLD = 0.9


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
                f"| {v.variant_id} | {format_score(v.weighted_score)} | {v.final_status}"
                + f" | {avg_dur:.1f}s | {tokens_str} |"
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

        # Row: Not Graded — conditional, like the budget sub-rows above. Without
        # it Tasks Run / Succeeded / Failed / Errors stop summing to tasks_run on
        # an ungraded run, with nothing in the table to say where the rest went.
        if any(result.variant_aggregates[vid].tasks_not_graded > 0 for vid in result.variant_ids):
            row = "| Not Graded"
            for vid in result.variant_ids:
                row += f" | {result.variant_aggregates[vid].tasks_not_graded}"
            if show_p_values:
                row += " | —"
            lines.append(row + " |")

        # Every task the variant GRADED is in the denominator, errors included;
        # ungraded rows leave both sides.
        row = "| Pass Rate"
        for vid in result.variant_ids:
            rate = result.variant_aggregates[vid].pass_rate
            row += f" | {rate * 100:.1f}%" if rate is not None else " | n/a"
        if show_p_values:
            row += " | —"
        lines.append(row + " |")

        return lines

    @staticmethod
    def _aggregate_stat_rows(
        result: ExperimentResult,
        series: dict[str, VariantSeries],
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
            row += f" | {fmt_mean_sd(series[vid].scores)}"
        if show_p_values:
            p = welch_t_test(series[vid_a].scores, series[vid_b].scores)
            row += f" | {fmt_p(p)}"
        lines.append(row + " |")

        # Row: Duration
        row = "| Avg Duration (s)"
        for vid in result.variant_ids:
            row += f" | {fmt_mean_sd(series[vid].durations, '.1f')}"
        if show_p_values:
            p = welch_t_test(series[vid_a].durations, series[vid_b].durations)
            row += f" | {fmt_p(p)}"
        lines.append(row + " |")

        # Row: Assistant Turns (if data available)
        if any(series[vid].asst_turns for vid in result.variant_ids):
            row = "| Assistant Turns"
            for vid in result.variant_ids:
                row += f" | {fmt_mean_sd(series[vid].asst_turns, '.1f')}"
            if show_p_values:
                p = welch_t_test(series[vid_a].asst_turns, series[vid_b].asst_turns)
                row += f" | {fmt_p(p)}"
            lines.append(row + " |")

        # Row: Tokens (if data available)
        if any(series[vid].tokens for vid in result.variant_ids):
            row = "| Tokens"
            for vid in result.variant_ids:
                row += f" | {fmt_mean_sd(series[vid].tokens, ',.0f')}"
            if show_p_values:
                p = welch_t_test(series[vid_a].tokens, series[vid_b].tokens)
                row += f" | {fmt_p(p)}"
            lines.append(row + " |")

        return lines

    @staticmethod
    def _aggregate_metrics_lines(result: ExperimentResult) -> list[str]:
        """The ``## Aggregate Metrics`` vertical table (metrics as rows, variants as
        columns). The p-value column + Welch t-tests appear only for exactly 2 variants;
        ``vid_a``/``vid_b`` stay local so the 3+-variant path never indexes them."""
        # ── Aggregate Metrics (vertical: metrics as rows, variants as columns) ──
        series = collect_variant_series(result)

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
        lines += ExperimentReportGenerator._aggregate_stat_rows(result, series, show_p_values, vid_a, vid_b)

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
                    cells.append(f"{format_score(vr.weighted_score)} ({status_icon})")
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

        Renders :func:`coder_eval.reports_stats.paired_comparison`, which the HTML
        reporter renders too. Returns ``[]`` only when the two variants have no
        scored task in common; when they have exactly one, the section explains why
        no paired result is shown.
        """
        pc = paired_comparison(result)
        if pc is None:
            return []

        header = ["", "## Paired Comparison", ""]
        if pc.mean_diff is None or pc.ci_low is None or pc.ci_high is None:
            return [
                *header,
                f"*A paired comparison needs at least 2 tasks common to {pc.vid_a} and {pc.vid_b};"
                + f" found {pc.task_count}.*",
            ]

        d_str = f"{pc.effect_size:.2f}" if pc.effect_size is not None else "n/a"
        excluded = f" ({pc.excluded_count} task(s) excluded — not scored by both variants)" if pc.excluded_count else ""
        return [
            *header,
            f"*Paired over the per-task mean score of {pc.task_count} task(s) common to both variants"
            + excluded
            + " — pairing cancels between-task difficulty, which the pooled Welch test above cannot.*",
            f"**Paired mean diff ({pc.vid_a} - {pc.vid_b})**: {pc.mean_diff:+.3f}"
            + f" [95% CI {pc.ci_low:+.3f}, {pc.ci_high:+.3f}], Cohen's d = {d_str}"
            + f", p = {fmt_p(pc.p_value)}",
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

        agg = result.variant_aggregates[variant_id]
        pass_rate_str = f"{agg.pass_rate * 100:.1f}%" if agg.pass_rate is not None else "n/a"
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
            *([f"- **Not Graded**: {agg.tasks_not_graded}"] if agg.tasks_not_graded else []),
            # Denominator is the GRADED count, matching VariantAggregate.pass_rate —
            # an ungraded task was never measured and belongs on neither side.
            f"- **Pass Rate**: {pass_rate_str} ({agg.tasks_succeeded}/{agg.tasks_graded})",
            f"- **Average Score**: {format_score(agg.average_score)}",
            f"- **Average Duration**: {agg.average_duration:.1f}s",
            f"- **Total Tokens**: {tokens_str}",
        ]

        # Collect per-task variant results for stddev metrics
        variant_results = [
            vr for ts in result.task_summaries for vr in ts.variant_results if vr.variant_id == variant_id
        ]
        scores = [vr.weighted_score for vr in variant_results if vr.weighted_score is not None]
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
                    score_text = format_score(vr.weighted_score)
                    row = f"| {ts.task_id} | {score_text} | {vr.final_status} | {avg_duration:.1f}s |"
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
                        env = {k: v for k, v in er.environment_info.items() if is_env_table_key(k)}
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

        # HTML reports — each write is wrapped by ``safe_write`` so a render
        # bug in one report cannot mask the run outcome.
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
