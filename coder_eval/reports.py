"""Report generation and formatting for evaluation runs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .models import CommandStatistics, RunSummary


class ReportGenerator:
    """Generates reports from evaluation results."""

    @staticmethod
    def _generate_command_statistics_section(stats: CommandStatistics) -> list[str]:
        """Generate markdown section for command statistics.

        Args:
            stats: CommandStatistics object with aggregated metrics

        Returns:
            List of markdown lines
        """
        lines = [
            "## Command Telemetry",
            "",
            f"**Total Commands**: {stats.total_commands}",
        ]

        if stats.total_commands > 0:
            success_rate = (stats.successful_commands / stats.total_commands * 100) if stats.total_commands > 0 else 0
            lines.append(f"**Success Rate**: {stats.successful_commands}/{stats.total_commands} ({success_rate:.1f}%)")

        if stats.commands_by_tool:
            lines.extend(["", "### Commands by Tool", "", "| Tool | Count | % |", "|------|-------|---|"])

            total = stats.total_commands
            for tool, count in sorted(stats.commands_by_tool.items(), key=lambda x: x[1], reverse=True):
                pct = count / total * 100 if total > 0 else 0
                lines.append(f"| {tool} | {count} | {pct:.1f}% |")

        if stats.avg_command_time_ms and stats.avg_command_time_ms > 0:
            lines.extend(
                [
                    "",
                    "### Performance",
                    "",
                    f"- **Average Command Time**: {stats.avg_command_time_ms:.1f}ms",
                    f"- **Total Command Time**: {stats.total_command_time_ms / 1000:.2f}s",
                ]
            )

        if stats.slowest_commands:
            lines.extend(
                ["", "### Slowest Commands", "", "| Tool | Duration | Parameters |", "|------|----------|------------|"]
            )
            for cmd in stats.slowest_commands:
                params_str = str(cmd.parameters)[:50]
                if len(str(cmd.parameters)) > 50:
                    params_str += "..."
                lines.append(f"| {cmd.tool} | {cmd.duration_ms:.0f}ms | {params_str} |")

        if stats.most_common_sequence:
            lines.extend(["", f"**Most Common Pattern**: `{stats.most_common_sequence}`"])

        return lines

    @staticmethod
    def generate_markdown(summary: RunSummary, run_dir: Path | None = None) -> str:
        """Generate markdown report from run summary.

        Args:
            summary: RunSummary object containing evaluation results
            run_dir: Optional path to run directory to load command statistics

        Returns:
            Markdown-formatted report string
        """
        success_rate = (summary.tasks_succeeded / summary.tasks_run * 100) if summary.tasks_run > 0 else 0

        lines = [
            "# Evaluation Run Report",
            "",
            f"**Run ID**: `{summary.run_id}`",
            f"**Date**: {summary.start_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Duration**: {summary.total_duration_seconds:.2f}s",
            "",
            "## Summary",
            "",
            f"- **Total Tasks**: {summary.tasks_run}",
            f"- **Succeeded**: {summary.tasks_succeeded}",
            f"- **Failed**: {summary.tasks_failed}",
            f"- **Errors**: {summary.tasks_error}",
            f"- **Success Rate**: {success_rate:.1f}%",
            "",
            "## Task Details",
            "",
            "| Task ID | Status | Weighted Score | Duration |",
            "|---------|--------|----------------|----------|",
        ]

        for task_result in summary.task_results:
            status_display = task_result["status"]
            weighted_score = task_result.get("weighted_score")
            score_str = f"{weighted_score:.3f}" if weighted_score is not None else "N/A"
            lines.append(
                f"| {task_result['task_id']} | {status_display} | {score_str} | {task_result['duration']:.2f}s |"
            )

        # Add aggregated command statistics if run_dir is provided
        if run_dir:
            aggregated_stats = ReportGenerator._aggregate_command_statistics(run_dir)
            if aggregated_stats and aggregated_stats.total_commands > 0:
                lines.extend(["", ""])  # Spacing
                lines.extend(ReportGenerator._generate_command_statistics_section(aggregated_stats))

        lines.extend(
            [
                "",
                "## Environment",
                "",
                f"- **Framework**: {summary.framework_version}",
            ]
        )

        for key, value in summary.environment_info.items():
            lines.append(f"- **{key}**: {value}")

        return "\n".join(lines)

    @staticmethod
    def _aggregate_command_statistics(run_dir: Path) -> CommandStatistics | None:
        """Aggregate command statistics from all task reports in a run.

        Args:
            run_dir: Path to run directory containing task subdirectories

        Returns:
            Aggregated CommandStatistics or None if no stats available
        """
        from .analysis import calculate_command_statistics
        from .models import EvaluationResult, TurnRecord

        all_turns: list[TurnRecord] = []

        # Iterate through task subdirectories and load their reports
        for task_dir in run_dir.iterdir():
            if not task_dir.is_dir() or task_dir.name in {"artifacts", ".git"}:
                continue

            report_path = task_dir / "report.json"
            if not report_path.exists():
                continue

            try:
                result = EvaluationResult.model_validate_json(report_path.read_text())
                all_turns.extend(result.turns)
            except Exception:
                # Skip tasks that can't be loaded
                continue

        if not all_turns:
            return None

        return calculate_command_statistics(all_turns)

    @staticmethod
    def load_from_run_dir(run_dir: Path) -> tuple[str, Path]:
        """Load report from run directory.

        Tries to load pre-generated markdown report first, then falls back
        to regenerating from JSON summary if needed.

        Args:
            run_dir: Path to run directory containing report files

        Returns:
            Tuple of (report_content, source_path)

        Raises:
            FileNotFoundError: If no report files exist in the directory
        """
        # Resolve symlink if necessary (e.g., runs/latest)
        if run_dir.is_symlink():
            run_dir = run_dir.resolve()

        report_md_path = run_dir / "run-report.md"
        summary_json_path = run_dir / "run-summary.json"

        # Try pre-generated markdown report first
        if report_md_path.exists():
            return report_md_path.read_text(), report_md_path

        # Fall back to regenerating from JSON summary
        if summary_json_path.exists():
            from .models import RunSummary

            summary = RunSummary.model_validate_json(summary_json_path.read_text())
            report_md = ReportGenerator.generate_markdown(summary, run_dir=run_dir)
            return report_md, summary_json_path

        # Neither file exists
        raise FileNotFoundError(
            f"No report found in {run_dir}. Expected {report_md_path.name} or {summary_json_path.name}"
        )
