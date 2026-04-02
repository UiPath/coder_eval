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

        # Skill tool usage callout — useful for skill-impact experiments
        if stats.commands_by_tool:
            skill_count = stats.commands_by_tool.get("Skill", 0)
            if skill_count > 0:
                lines.extend(["", f"**Skill Tool Invoked**: {skill_count} time(s)"])

        return lines

    @staticmethod
    def _generate_generation_metrics_section(task_results: list[dict[str, Any]]) -> list[str]:
        """Generate Generation Metrics section showing per-task latency and iteration breakdown.

        Args:
            task_results: List of task result dictionaries from RunSummary

        Returns:
            List of markdown lines
        """
        lines = [
            "## Generation Metrics",
            "",
            "| Task ID | Total Latency | Turns | Asst Turns | Avg Turn Latency | Self-Corrections |",
            "|---------|---------------|-------|------------|------------------|------------------|",
        ]

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

            lines.append(
                f"| {task_id} | {total_latency} | {num_turns} | {asst_turns} | {avg_turn_str} | {self_corrections} |"
            )

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
        evaluable = summary.tasks_run - summary.tasks_error
        success_rate = (summary.tasks_succeeded / evaluable * 100) if evaluable > 0 else 0

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

        # Task Details table
        has_similarity = any(t.get("reference_similarity") is not None for t in summary.task_results)
        has_model = any(t.get("model_used") for t in summary.task_results)
        has_tags = any(t.get("tags") for t in summary.task_results)
        has_cmds_efficiency = any(t.get("commands_efficiency") is not None for t in summary.task_results)

        header = "| Task ID | Status | Reliability Score | Iterations | Latency |"
        separator = "|---------|--------|-------------------|------------|---------|"
        if has_model:
            header += " Model |"
            separator += "-------|"
        if has_tags:
            header += " Tags |"
            separator += "------|"
        if has_similarity:
            header += " Similarity |"
            separator += "------------|"
        if has_cmds_efficiency:
            header += " Cmd Efficiency |"
            separator += "----------------|"

        lines.extend(["", "## Task Details", "", header, separator])

        for task_result in summary.task_results:
            weighted_score = task_result.get("weighted_score")
            score_str = f"{weighted_score:.3f}" if weighted_score is not None else "N/A"
            iters = task_result.get("iteration_count", "N/A")
            duration = f"{task_result['duration']:.1f}s"

            row = f"| {task_result['task_id']} | {task_result['status']} | {score_str} | {iters} | {duration} |"
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
            if has_cmds_efficiency:
                eff = task_result.get("commands_efficiency")
                eff_str = f"{eff * 100:.1f}%" if eff is not None else "N/A"
                expected = task_result.get("expected_commands")
                actual = task_result.get("actual_commands")
                if expected is not None and actual is not None:
                    eff_str += f" ({expected}/{actual})"
                row += f" {eff_str} |"
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

        # Agent Settings section — prefer sdk_options (full SDK dump), fall back to agent_config
        sdk_opts_list = [t["sdk_options"] for t in summary.task_results if t.get("sdk_options")]
        agent_configs = [t["agent_config"] for t in summary.task_results if t.get("agent_config")]
        settings_source: dict[str, Any] | None = None
        is_sdk = False
        if sdk_opts_list:
            settings_source = sdk_opts_list[0]
            is_sdk = True
        elif agent_configs:
            settings_source = agent_configs[0]

        if settings_source:
            lines.append("")
            lines.extend(ReportGenerator._generate_agent_settings_section(settings_source, is_sdk))

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
    def _generate_agent_settings_section(settings_source: dict[str, Any], is_sdk: bool) -> list[str]:
        """Generate Agent Settings markdown lines from a settings dict.

        Args:
            settings_source: Either sdk_options or agent_config dict.
            is_sdk: Whether settings_source is from sdk_options.

        Returns:
            List of markdown lines (including heading).
        """
        lines = ["## Agent Settings", ""]
        lines.append(f"- **Permission Mode**: {settings_source.get('permission_mode', 'N/A')}")
        tools = settings_source.get("allowed_tools")
        lines.append(f"- **Allowed Tools**: {', '.join(tools) if tools else '(all)'}")
        model = settings_source.get("model")
        if model:
            lines.append(f"- **Model**: {model}")

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

        # Plugins (rendered for both sdk_options and agent_config)
        plugins = settings_source.get("plugins")
        if plugins is not None:
            if isinstance(plugins, list) and len(plugins) > 0:
                plugin_paths = [p.get("path", "unknown") if isinstance(p, dict) else str(p) for p in plugins]
                lines.append(f"- **Plugins**: {', '.join(plugin_paths)}")
            elif isinstance(plugins, list) and len(plugins) == 0:
                lines.append("- **Plugins**: (none)")

        return lines

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

        lines = ["## Token Usage", ""]

        total_input = sum(t.get("input_tokens") or 0 for t in tasks_with_tokens)
        total_output = sum(t.get("output_tokens") or 0 for t in tasks_with_tokens)
        total_cache_write = sum(t.get("cache_creation_input_tokens") or 0 for t in tasks_with_tokens)
        total_cache_read = sum(t.get("cache_read_input_tokens") or 0 for t in tasks_with_tokens)
        total_tokens = sum(t["total_tokens"] for t in tasks_with_tokens)
        costs = [t["total_cost_usd"] for t in tasks_with_tokens if t.get("total_cost_usd") is not None]
        total_cost = sum(costs) if costs else None

        lines.append(f"**Total Tokens**: {total_tokens:,} (input: {total_input:,}, output: {total_output:,})")
        if total_cache_write > 0 or total_cache_read > 0:
            lines.append(f"**Cache Tokens**: write: {total_cache_write:,}, read: {total_cache_read:,}")
        if total_cost is not None:
            lines.append(f"**Total Cost**: ${total_cost:.4f}")
        lines.append(f"**Avg Tokens/Task**: {total_tokens // len(tasks_with_tokens):,}")
        lines.append("")

        lines.extend(
            [
                "| Task ID | Input | Output | Cache Write | Cache Read | Total | Cost |",
                "|---------|-------|--------|-------------|------------|-------|------|",
            ]
        )

        for t in tasks_with_tokens:
            input_tok = t.get("input_tokens") or 0
            output_tok = t.get("output_tokens") or 0
            cache_write = t.get("cache_creation_input_tokens") or 0
            cache_read = t.get("cache_read_input_tokens") or 0
            tokens = t.get("total_tokens", 0)
            cost = t.get("total_cost_usd")
            cost_str = f"${cost:.4f}" if cost is not None else "N/A"
            row = (
                f"| {t['task_id']} | {input_tok:,} | {output_tok:,} "
                f"| {cache_write:,} | {cache_read:,} | {tokens:,} | {cost_str} |"
            )
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

        # Find all task.json files recursively to handle both flat and nested (experiment) layouts
        for report_path in run_dir.rglob("task.json"):
            if "artifacts" in report_path.parts or ".git" in report_path.parts:
                continue
            try:
                result = EvaluationResult.model_validate_json(report_path.read_text())
                all_turns.extend(result.turns)
            except Exception:
                logger.warning("Failed to load report %s for command statistics", report_path, exc_info=True)

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

        # Check for reports in order of preference:
        # 1. experiment.md/json (written by ExperimentReportGenerator)
        # 2. run.md/json (written by batch-level _generate_run_summary)
        for md_name, json_name in [("experiment.md", "experiment.json"), ("run.md", "run.json")]:
            report_md_path = run_dir / md_name
            summary_json_path = run_dir / json_name

            if report_md_path.exists():
                return report_md_path.read_text(), report_md_path

            if summary_json_path.exists():
                from .models import RunSummary

                summary = RunSummary.model_validate_json(summary_json_path.read_text())
                report_md = ReportGenerator.generate_markdown(summary, run_dir=run_dir)
                return report_md, summary_json_path

        raise FileNotFoundError(
            f"No report found in {run_dir}. Expected experiment.md, experiment.json, run.md, or run.json"
        )
