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
from coder_eval.reports import resolve_agent_settings
from coder_eval.reports_stats import (
    describe_prompt_config,
    fmt_mean_sd,
    fmt_p,
    load_variant_eval_results,
    mean,
    stddev,
    welch_t_test,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: build task_result dict from EvaluationResult (for variant reports)
# ---------------------------------------------------------------------------


def eval_result_to_task_dict(
    result: EvaluationResult,
    *,
    variant_id: str | None = None,
    tags: list[str] | None = None,
    duration_override: float | None = None,
) -> dict[str, Any]:
    """Convert an EvaluationResult to the task_result dict format used by ReportGenerator.

    Args:
        result: The evaluation result to convert.
        variant_id: Optional variant ID to include in the dict.
        tags: Optional tags list (defaults to []).
        duration_override: Optional duration value (defaults to result.duration_seconds).
    """
    ref_similarity: float | None = None
    for cr in result.success_criteria_results:
        if cr.criterion_type == "reference_comparison":
            ref_similarity = cr.score
            break

    d: dict[str, Any] = {
        "task_id": result.task_id,
        "status": result.final_status,
        "weighted_score": result.weighted_score,
        "duration": duration_override if duration_override is not None else result.duration_seconds,
        "iteration_count": result.iteration_count,
        "tags": tags if tags is not None else [],
        "turns": [
            {
                "iteration": t.iteration,
                "duration_seconds": t.duration_seconds,
                "command_count": len(t.commands),
                "assistant_turn_count": t.assistant_turn_count,
            }
            for t in result.turns
        ],
        "model_used": result.model_used,
        "reference_similarity": ref_similarity,
        "input_tokens": (result.total_token_usage.input_tokens if result.total_token_usage else None),
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
            "| Variant | Score | Status | Duration | Tokens |",
            "|---------|-------|--------|----------|--------|",
        ]

        for v in summary.variant_results:
            tokens_str = f"{v.total_tokens:,}" if v.total_tokens is not None else "N/A"
            lines.append(
                f"| {v.variant_id} | {v.weighted_score:.3f} | {v.final_status}"
                + f" | {v.duration_seconds:.1f}s | {tokens_str} |"
            )

        return "\n".join(lines)

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
        lines = [
            f"# Experiment Report: {result.experiment_id}",
            "",
            f"**Description**: {result.description}",
            f"**Variants**: {', '.join(result.variant_ids)}",
            f"**Total Duration**: {result.total_duration_seconds:.1f}s",
        ]

        # ── Variant prompt configuration (if experiment definition available) ──
        if experiment is not None:
            variant_map = {v.variant_id: v for v in experiment.variants}
            has_prompt_config = bool(experiment.defaults and experiment.defaults.prompt_mutations) or any(
                v.prompt_mutations or v.initial_prompt or v.initial_prompt_file for v in experiment.variants
            )
            if has_prompt_config:
                lines.extend(["", "## Prompt Configuration", ""])
                for vid in result.variant_ids:
                    v = variant_map.get(vid)
                    desc = describe_prompt_config(v) if v else "(unknown)"
                    lines.append(f"- **{vid}**: {desc}")

        # ── Aggregate Metrics (vertical: metrics as rows, variants as columns) ──
        # Collect per-task values for each variant
        variant_scores: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}
        variant_durations: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}
        variant_tokens: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}
        variant_iterations: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}
        variant_asst_turns: dict[str, list[float]] = {vid: [] for vid in result.variant_ids}

        for ts in result.task_summaries:
            for vr in ts.variant_results:
                variant_scores[vr.variant_id].append(vr.weighted_score)
                variant_durations[vr.variant_id].append(vr.duration_seconds)
                if vr.total_tokens is not None:
                    variant_tokens[vr.variant_id].append(float(vr.total_tokens))
                if vr.iteration_count is not None:
                    variant_iterations[vr.variant_id].append(float(vr.iteration_count))
                if vr.total_assistant_turns is not None:
                    variant_asst_turns[vr.variant_id].append(float(vr.total_assistant_turns))

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

        lines.extend(["", "## Aggregate Metrics", "", header, sep])

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

        # Row: Score (mean ± stddev, p-value)
        row = "| Score"
        for vid in result.variant_ids:
            row += f" | {fmt_mean_sd(variant_scores[vid])}"
        if show_p_values:
            p = welch_t_test(variant_scores[vid_a], variant_scores[vid_b])
            row += f" | {fmt_p(p)}"
        lines.append(row + " |")

        # Row: Duration
        row = "| Duration (s)"
        for vid in result.variant_ids:
            row += f" | {fmt_mean_sd(variant_durations[vid], '.1f')}"
        if show_p_values:
            p = welch_t_test(variant_durations[vid_a], variant_durations[vid_b])
            row += f" | {fmt_p(p)}"
        lines.append(row + " |")

        # Row: Iterations (if data available)
        if any(variant_iterations[vid] for vid in result.variant_ids):
            row = "| Iterations"
            for vid in result.variant_ids:
                row += f" | {fmt_mean_sd(variant_iterations[vid], '.1f')}"
            if show_p_values:
                p = welch_t_test(variant_iterations[vid_a], variant_iterations[vid_b])
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

        # ── Win/loss/tie analysis ──
        if result.task_summaries:
            lines.extend(["", "## Win Rates", ""])
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
            lines.extend(["", "## Per-Task Comparison", ""])
            header = "| Task | " + " | ".join(result.variant_ids) + " | Best | Spread |"
            sep = "|------|" + "|".join("------" for _ in result.variant_ids) + "|------|--------|"
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
                lines.append(f"| {ts.task_id} | " + " | ".join(cells) + f" | {best_str} | {ts.score_spread:.3f} |")

            # ── Highest divergence ──
            sorted_tasks = sorted(result.task_summaries, key=lambda t: t.score_spread, reverse=True)
            if sorted_tasks and sorted_tasks[0].score_spread > 0:
                lines.extend(["", "## Most Divergent Tasks", ""])
                for ts in sorted_tasks[:5]:
                    lines.append(f"- **{ts.task_id}**: spread={ts.score_spread:.3f}, best={ts.best_variant}")

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
            f"- **Failed**: {agg.tasks_failed}",
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
        durations = [vr.duration_seconds for vr in variant_results]
        iterations = [float(vr.iteration_count) for vr in variant_results if vr.iteration_count is not None]

        if scores and len(scores) >= 2:
            lines.append(f"- **Score Stddev**: {stddev(scores):.3f}")
        if durations and len(durations) >= 2:
            lines.append(f"- **Duration Stddev**: {stddev(durations):.1f}s")
        if iterations:
            lines.append(f"- **Avg Iterations**: {mean(iterations):.1f}")

        # Task Details table
        has_similarity = any(vr.reference_similarity is not None for vr in variant_results)

        header = "| Task | Score | Status | Duration | Iterations |"
        separator = "|------|-------|--------|----------|------------|"
        if has_similarity:
            header += " Similarity |"
            separator += "------------|"

        lines.extend(["", "## Task Details", "", header, separator])

        for ts in result.task_summaries:
            for vr in ts.variant_results:
                if vr.variant_id == variant_id:
                    iters_str = str(vr.iteration_count) if vr.iteration_count is not None else "N/A"
                    row = (
                        f"| {ts.task_id} | {vr.weighted_score:.3f} | {vr.final_status}"
                        f" | {vr.duration_seconds:.1f}s | {iters_str} |"
                    )
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
                if any(d.get("turns") for d in task_dicts):
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
                rel_link = f"{vr.task_id}/task.html"
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
