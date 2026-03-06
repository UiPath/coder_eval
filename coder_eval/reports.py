"""Report generation and formatting for evaluation runs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .models import CommandStatistics, RunSummary

logger = logging.getLogger(__name__)


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
    def _generate_generation_metrics_section(task_results: list[dict[str, Any]]) -> list[str]:
        """Generate Generation Metrics section showing per-task latency and iteration breakdown.

        Args:
            task_results: List of task result dictionaries from RunSummary

        Returns:
            List of markdown lines
        """
        has_agent = any(t.get("agent_name") for t in task_results)

        header = "| Task ID |"
        separator = "|---------|"
        if has_agent:
            header += " Agent |"
            separator += "-------|"
        header += " Total Latency | Turns | Asst Turns | Avg Turn Latency | Self-Corrections |"
        separator += "---------------|-------|------------|------------------|------------------|"

        lines = ["## Generation Metrics", "", header, separator]

        for task in task_results:
            task_id = task["task_id"]
            total_latency = f"{task['duration']:.1f}s"
            turns = task.get("turns", [])
            num_turns = len(turns)
            iteration_count = task.get("iteration_count") or 0
            self_corrections = max(0, iteration_count - 1)

            # Sum assistant_turn_count across all turns for this task
            asst_turns = sum(t.get("assistant_turn_count", 0) for t in turns)

            if turns:
                avg_turn = sum(t["duration_seconds"] for t in turns) / len(turns)
                avg_turn_str = f"{avg_turn:.1f}s"
            else:
                avg_turn_str = "N/A"

            row = f"| {task_id} |"
            if has_agent:
                row += f" {task.get('agent_name') or 'N/A'} |"
            row += f" {total_latency} | {num_turns} | {asst_turns} | {avg_turn_str} | {self_corrections} |"
            lines.append(row)

        return lines

    @staticmethod
    def _generate_agent_comparison_section(task_results: list[dict[str, Any]]) -> list[str]:
        """Generate an Agent Comparison section for runs with multi-agent tasks.

        Groups task results by task_id when multiple agents ran the same task,
        then renders a side-by-side comparison table per task.

        Args:
            task_results: List of task result dicts from RunSummary

        Returns:
            List of markdown lines (empty if no multi-agent results present)
        """
        from collections import defaultdict

        groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for tr in task_results:
            if tr.get("agent_name"):
                groups[tr["task_id"]].append(tr)

        if not groups:
            return []

        lines = ["## Agent Comparison", ""]

        for task_id in sorted(groups):
            agent_results = groups[task_id]
            lines.append(f"### {task_id}")
            lines.append("")
            lines.extend(
                [
                    "| Agent | Status | Score | Iterations | Latency |",
                    "|-------|--------|-------|------------|---------|",
                ]
            )
            for ar in agent_results:
                agent_name = ar.get("agent_name", "N/A")
                status = ar["status"]
                score_val = ar.get("weighted_score")
                score = f"{score_val:.3f}" if score_val is not None else "N/A"
                iters = ar.get("iteration_count", "N/A")
                latency = f"{ar['duration']:.1f}s"
                lines.append(f"| {agent_name} | {status} | {score} | {iters} | {latency} |")
            lines.append("")

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

        # Collect unique models across all tasks
        models = sorted({t["model_used"] for t in summary.task_results if t.get("model_used")})

        lines = [
            "# Evaluation Run Report",
            "",
            f"**Run ID**: `{summary.run_id}`",
            f"**Date**: {summary.start_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Duration**: {summary.total_duration_seconds:.2f}s",
        ]

        if len(models) == 1:
            lines.append(f"**Model**: `{models[0]}`")
        elif len(models) > 1:
            lines.append(f"**Models**: {', '.join(f'`{m}`' for m in models)}")

        lines.extend(
            [
                "",
                "## Summary",
                "",
                f"- **Total Tasks**: {summary.tasks_run}",
                f"- **Succeeded**: {summary.tasks_succeeded}",
                f"- **Failed**: {summary.tasks_failed}",
                f"- **Errors**: {summary.tasks_error}",
                f"- **Success Rate**: {success_rate:.1f}%",
            ]
        )

        # Aggregate P0 metrics
        scores = [t["weighted_score"] for t in summary.task_results if t.get("weighted_score") is not None]
        durations = [t["duration"] for t in summary.task_results if t["duration"] > 0]
        iterations = [t["iteration_count"] for t in summary.task_results if t.get("iteration_count") is not None]
        similarities = [
            t["reference_similarity"] for t in summary.task_results if t.get("reference_similarity") is not None
        ]

        if scores:
            lines.append(f"- **Avg Reliability Score**: {sum(scores) / len(scores):.3f}")
        if durations:
            lines.append(f"- **Avg Generation Latency**: {sum(durations) / len(durations):.1f}s")
        if iterations:
            lines.append(f"- **Avg Self-Correction Iterations**: {sum(iterations) / len(iterations):.1f}")

        # Total assistant turns across all tasks
        total_asst_turns = sum(
            sum(t.get("assistant_turn_count", 0) for t in task.get("turns", [])) for task in summary.task_results
        )
        if total_asst_turns > 0:
            lines.append(f"- **Total Assistant Turns**: {total_asst_turns}")

        if similarities:
            lines.append(f"- **Avg Ground Truth Similarity**: {sum(similarities) / len(similarities):.3f}")

        # Agent Comparison section (for multi-agent runs)
        comparison_lines = ReportGenerator._generate_agent_comparison_section(summary.task_results)
        if comparison_lines:
            lines.extend(["", ""])
            lines.extend(comparison_lines)

        # Task Details table
        has_similarity = any(t.get("reference_similarity") is not None for t in summary.task_results)
        has_model = any(t.get("model_used") for t in summary.task_results)
        has_tags = any(t.get("tags") for t in summary.task_results)
        has_agent = any(t.get("agent_name") for t in summary.task_results)

        header = "| Task ID | Status | Reliability Score | Iterations | Latency |"
        separator = "|---------|--------|-------------------|------------|---------|"
        if has_agent:
            header += " Agent |"
            separator += "-------|"
        if has_model:
            header += " Model |"
            separator += "-------|"
        if has_tags:
            header += " Tags |"
            separator += "------|"
        if has_similarity:
            header += " Similarity |"
            separator += "------------|"

        lines.extend(["", "## Task Details", "", header, separator])

        for task_result in summary.task_results:
            weighted_score = task_result.get("weighted_score")
            score_str = f"{weighted_score:.3f}" if weighted_score is not None else "N/A"
            iters = task_result.get("iteration_count", "N/A")
            duration = f"{task_result['duration']:.1f}s"

            row = f"| {task_result['task_id']} | {task_result['status']} | {score_str} | {iters} | {duration} |"
            if has_agent:
                agent_name = task_result.get("agent_name") or "N/A"
                row += f" {agent_name} |"
            if has_model:
                model = task_result.get("model_used") or "N/A"
                row += f" {model} |"
            if has_tags:
                tags = task_result.get("tags", [])
                tags_str = ", ".join(tags) if tags else ""
                row += f" {tags_str} |"
            if has_similarity:
                sim = task_result.get("reference_similarity")
                sim_str = f"{sim:.3f}" if sim is not None else "N/A"
                row += f" {sim_str} |"
            lines.append(row)

        # Generation Metrics section
        if any(t.get("turns") for t in summary.task_results):
            lines.extend(["", ""])
            lines.extend(ReportGenerator._generate_generation_metrics_section(summary.task_results))

        # Token Usage section
        token_lines = ReportGenerator._generate_token_usage_section(summary.task_results)
        if token_lines:
            lines.extend(["", ""])
            lines.extend(token_lines)

        # Add aggregated command statistics if run_dir is provided
        if run_dir:
            aggregated_stats = ReportGenerator._aggregate_command_statistics(run_dir)
            if aggregated_stats and aggregated_stats.total_commands > 0:
                lines.extend(["", ""])
                lines.extend(ReportGenerator._generate_command_statistics_section(aggregated_stats))

        # Agent Settings section — prefer sdk_options (full SDK dump), fall back to agent_config.
        # For multi-agent runs, render one subsection per agent.
        # Deduplicate: same agent_name appears across multiple tasks; only emit settings once per agent.
        agent_entries: list[tuple[str | None, dict[str, Any], bool]] = []  # (agent_name, settings, is_sdk)
        seen_agent_names: set[str | None] = set()
        for tr in summary.task_results:
            agent_name = tr.get("agent_name")
            if agent_name in seen_agent_names:
                continue
            if tr.get("sdk_options"):
                agent_entries.append((agent_name, tr["sdk_options"], True))
                seen_agent_names.add(agent_name)
            elif tr.get("agent_config"):
                agent_entries.append((agent_name, tr["agent_config"], False))
                seen_agent_names.add(agent_name)

        if agent_entries:
            is_multi_agent = any(name for name, _, _ in agent_entries)
            lines.extend(["", "## Agent Settings", ""])
            for agent_name, settings_source, is_sdk in agent_entries:
                if is_multi_agent and agent_name:
                    lines.append(f"### {agent_name}")
                    lines.append("")
                # Common fields (shared between sdk_options and agent_config)
                lines.append(f"- **Permission Mode**: {settings_source.get('permission_mode', 'N/A')}")
                tools = settings_source.get("allowed_tools")
                lines.append(f"- **Allowed Tools**: {', '.join(tools) if tools else '(all)'}")
                model = settings_source.get("model")
                if model:
                    lines.append(f"- **Model**: {model}")

                # Additional SDK-specific fields (only when using sdk_options and non-default)
                if is_sdk:
                    if settings_source.get("max_turns") is not None:
                        lines.append(f"- **Max Turns**: {settings_source['max_turns']}")
                    if settings_source.get("max_budget_usd") is not None:
                        lines.append(f"- **Max Budget (USD)**: {settings_source['max_budget_usd']}")
                    if settings_source.get("thinking") is not None:
                        lines.append(f"- **Thinking**: {settings_source['thinking']}")
                    if settings_source.get("effort") is not None:
                        lines.append(f"- **Effort**: {settings_source['effort']}")
                    if settings_source.get("mcp_servers"):
                        mcp = settings_source["mcp_servers"]
                        if isinstance(mcp, dict):
                            lines.append(f"- **MCP Servers**: {', '.join(mcp.keys())}")
                        else:
                            lines.append(f"- **MCP Servers**: {mcp}")
                    if settings_source.get("betas"):
                        lines.append(f"- **Betas**: {', '.join(settings_source['betas'])}")
                    if settings_source.get("system_prompt") is not None:
                        prompt_str = str(settings_source["system_prompt"]).replace("\n", " ")
                        if len(prompt_str) > 200:
                            prompt_str = prompt_str[:200] + "..."
                        lines.append(f"- **System Prompt**: {prompt_str}")
                if is_multi_agent:
                    lines.append("")

        # Installed Tools section (per-task tool versions from sandbox npm packages etc.)
        installed_tools_lines = ReportGenerator._generate_installed_tools_section(summary.task_results)
        if installed_tools_lines:
            lines.extend([""])
            lines.extend(installed_tools_lines)

        lines.extend(
            [
                "",
                "## Environment",
                "",
            ]
        )

        for key, value in summary.environment_info.items():
            lines.append(f"- **{key}**: {value}")

        return "\n".join(lines)

    @staticmethod
    def _generate_token_usage_section(task_results: list[dict[str, Any]]) -> list[str]:
        """Generate Token Usage section for the report.

        Args:
            task_results: List of task result dictionaries from RunSummary

        Returns:
            List of markdown lines (empty if no token data available)
        """
        tasks_with_tokens = [t for t in task_results if t.get("total_tokens") is not None]
        if not tasks_with_tokens:
            return []

        has_agent = any(t.get("agent_name") for t in tasks_with_tokens)

        lines = ["## Token Usage", ""]

        total_tokens = sum(t["total_tokens"] for t in tasks_with_tokens)
        costs = [t["total_cost_usd"] for t in tasks_with_tokens if t.get("total_cost_usd") is not None]
        total_cost = sum(costs) if costs else None

        lines.append(f"**Total Tokens**: {total_tokens:,}")
        if total_cost is not None:
            lines.append(f"**Total Cost**: ${total_cost:.4f}")
        lines.append(f"**Avg Tokens/Task**: {total_tokens // len(tasks_with_tokens):,}")
        lines.append("")

        header = "| Task ID |"
        separator = "|---------|"
        if has_agent:
            header += " Agent |"
            separator += "-------|"
        header += " Total Tokens | Cost |"
        separator += "-------------|------|"
        lines.extend([header, separator])

        for t in tasks_with_tokens:
            tokens = t.get("total_tokens", 0)
            cost = t.get("total_cost_usd")
            cost_str = f"${cost:.4f}" if cost is not None else "N/A"
            row = f"| {t['task_id']} |"
            if has_agent:
                row += f" {t.get('agent_name') or 'N/A'} |"
            row += f" {tokens:,} | {cost_str} |"
            lines.append(row)

        return lines

    @staticmethod
    def _generate_installed_tools_section(task_results: list[dict[str, Any]]) -> list[str]:
        """Generate Installed Tools section showing per-task tool versions.

        Args:
            task_results: List of task result dictionaries from RunSummary

        Returns:
            List of markdown lines (empty if no tasks have installed tools)
        """
        tasks_with_tools = [t for t in task_results if t.get("installed_tools")]
        if not tasks_with_tools:
            return []

        lines = [
            "## Installed Tools",
            "",
            "| Task ID | Tool | Version |",
            "|---------|------|---------|",
        ]

        for task in tasks_with_tools:
            task_id = task["task_id"]
            for tool_name, version in sorted(task["installed_tools"].items()):
                lines.append(f"| {task_id} | {tool_name} | {version} |")

        return lines

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

        skip_dirs = {"artifacts", ".git"}

        # Iterate through task subdirectories and load their reports.
        # Handles both single-agent layout (run_dir/task_id/report.json) and
        # multi-agent layout (run_dir/task_id/agent_name/report.json).
        for task_dir in run_dir.iterdir():
            if not task_dir.is_dir() or task_dir.name in skip_dirs:
                continue

            report_path = task_dir / "report.json"
            if report_path.exists():
                # Single-agent task directory
                try:
                    result = EvaluationResult.model_validate_json(report_path.read_text())
                    all_turns.extend(result.turns)
                except Exception:
                    logger.warning("Failed to load report %s for command statistics", report_path, exc_info=True)
            else:
                # Maybe a multi-agent task directory — recurse one level into agent subdirs
                for agent_dir in task_dir.iterdir():
                    if not agent_dir.is_dir() or agent_dir.name in skip_dirs:
                        continue
                    agent_report = agent_dir / "report.json"
                    if not agent_report.exists():
                        continue
                    try:
                        result = EvaluationResult.model_validate_json(agent_report.read_text())
                        all_turns.extend(result.turns)
                    except Exception:
                        logger.warning("Failed to load report %s for command statistics", agent_report, exc_info=True)

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
