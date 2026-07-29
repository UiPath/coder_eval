"""Report generation and formatting for evaluation runs."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from .models import (
    CriterionAggregate,
    CriterionStats,
    FailedRowSummary,
    SuiteRollup,
    TaskResult,
    ThresholdCheck,
    eval_overhead_costs,
    row_cost_incomplete,
)
from .path_utils import build_task_run_dir


if TYPE_CHECKING:
    from .models import CommandStatistics, RunSummary

logger = logging.getLogger(__name__)

# Cap on how many failed rows are carried in suite.json / rendered in suite.md.
_FAILED_SAMPLE_LIMIT = 20
# Cap on how many criterion failure reasons we record per failed row.
_FAILURE_REASONS_PER_ROW = 3
# Cap on each failure-reason string so suite.md stays readable.
_FAILURE_REASON_MAX_LEN = 240


SYSTEM_PROMPT_PREVIEW_CHARS = 200
SLOW_PARAMS_PREVIEW_CHARS = 50


def resolve_agent_settings(task_dicts: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """Pick the best agent settings source from a list of task result dicts.

    Prefers ``sdk_options`` (full SDK dump) over ``agent_config``.

    Returns:
        Tuple of (settings_dict_or_None, is_sdk_options).
    """
    sdk_opts_list = [t["sdk_options"] for t in task_dicts if t.get("sdk_options")]
    if sdk_opts_list:
        return sdk_opts_list[0], True
    agent_configs = [t["agent_config"] for t in task_dicts if t.get("agent_config")]
    if agent_configs:
        return agent_configs[0], False
    return None, False


def collect_agent_settings_rows(settings_source: dict[str, Any], is_sdk: bool) -> list[tuple[str, str]]:
    """Extract ordered label/value pairs from an agent settings dict.

    Shared by markdown and HTML reporters so field ordering, defaulting,
    and truncation stay consistent between the two formats.
    """
    rows: list[tuple[str, str]] = [
        ("Permission Mode", str(settings_source.get("permission_mode", "N/A"))),
    ]
    tools = settings_source.get("allowed_tools")
    rows.append(("Allowed Tools", ", ".join(tools) if tools else "(all)"))
    model = settings_source.get("model")
    if model:
        rows.append(("Model", str(model)))

    if is_sdk:
        for key, label in (
            ("max_turns", "Max Turns"),
            ("max_budget_usd", "Max Budget (USD)"),
            ("thinking", "Thinking"),
            ("effort", "Effort"),
        ):
            if settings_source.get(key) is not None:
                rows.append((label, str(settings_source[key])))
        mcp = settings_source.get("mcp_servers")
        if mcp:
            rows.append(("MCP Servers", ", ".join(mcp.keys()) if isinstance(mcp, dict) else str(mcp)))
        betas = settings_source.get("betas")
        if betas:
            rows.append(("Betas", ", ".join(betas)))
        if settings_source.get("system_prompt") is not None:
            prompt_str = str(settings_source["system_prompt"]).replace("\n", " ")
            if len(prompt_str) > SYSTEM_PROMPT_PREVIEW_CHARS:
                prompt_str = prompt_str[:SYSTEM_PROMPT_PREVIEW_CHARS] + "..."
            rows.append(("System Prompt", prompt_str))

    plugins = settings_source.get("plugins")
    if isinstance(plugins, list):
        if plugins:
            paths = [p.get("path", "unknown") if isinstance(p, dict) else str(p) for p in plugins]
            rows.append(("Plugins", ", ".join(paths)))
        else:
            rows.append(("Plugins", "(none)"))
    return rows


def group_consecutive_by_iteration[T](
    items: Iterable[T],
    iteration_of: Callable[[T], int | None],
) -> list[list[T]]:
    """Group consecutive items sharing the same iteration value into runs (input order preserved)."""
    groups: list[list[T]] = []
    last_iter: int | None = None
    for item in items:
        it = iteration_of(item)
        if groups and it == last_iter:
            groups[-1].append(item)
        else:
            groups.append([item])
            last_iter = it
    return groups


def count_partials_by_outcome[T](
    groups: Iterable[Sequence[T]],
    crashed_of: Callable[[T], bool],
) -> tuple[int, int, int]:
    """Return ``(total, recovered, terminal)`` partial counts; recovered = group has a non-crashed turn."""
    total = recovered = terminal = 0
    for group in groups:
        partials = sum(1 for item in group if crashed_of(item))
        if not partials:
            continue
        total += partials
        if any(not crashed_of(item) for item in group):
            recovered += partials
        else:
            terminal += partials
    return total, recovered, terminal


def _count_crashed_partials(task_results: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Run-wide ``(total, recovered, terminal)`` over the markdown rollup's dict shape."""

    def _iteration_of(t: dict[str, Any]) -> int | None:
        # Coerce to int so a serialized "1" doesn't fragment from a sibling int 1.
        raw = t.get("iteration")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    total = recovered = terminal = 0
    for task in task_results:
        turns = task.get("iterations") or []
        groups = group_consecutive_by_iteration(turns, _iteration_of)
        t_count, r_count, term_count = count_partials_by_outcome(groups, lambda t: bool(t.get("crashed")))
        total += t_count
        recovered += r_count
        terminal += term_count
    return total, recovered, terminal


def _fmt_rate(rate: float | None) -> str:
    """Render a 0-1 rate as a percentage, or ``n/a`` when it is unknown."""
    return f"{rate * 100:.1f}%" if rate is not None else "n/a"


def _pass_rate_lines(summary: RunSummary) -> list[str]:
    """The pass rate, plus how much of it is being held down by errors.

    One rate over every dispatched task. The old line divided by
    ``tasks_run - tasks_error``, which read like a pass rate but paid a bonus for
    erroring: a run that fell over enough could print a near-perfect score while
    passing almost nothing. When errors are material the error share is printed
    beside the rate so a bad infrastructure night is visible rather than absorbed.
    """
    lines = [f"- **Pass Rate**: {_fmt_rate(summary.pass_rate)} ({summary.tasks_succeeded}/{summary.tasks_run})"]
    if summary.tasks_error:
        lines.append(
            f"- **Error Share**: {_fmt_rate(summary.error_share)} of tasks never produced a "
            + "gradeable attempt and count as misses"
        )
    return lines


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
            "| Task ID | Total Latency | Turns | Asst Turns | Avg Turn Latency |",
            "|---------|---------------|-------|------------|------------------|",
        ]

        for task in task_results:
            task_id = task["task_id"]
            total_latency = f"{task['duration']:.1f}s"
            turns = task.get("iterations", [])
            num_turns = len(turns)

            asst_turns = sum(t.get("assistant_turn_count", 0) for t in turns)

            if turns:
                avg_turn = sum(t["duration_seconds"] for t in turns) / len(turns)
                avg_turn_str = f"{avg_turn:.1f}s"
            else:
                avg_turn_str = "N/A"

            lines.append(f"| {task_id} | {total_latency} | {num_turns} | {asst_turns} | {avg_turn_str} |")

        return lines

    @staticmethod
    def _report_header_lines(summary: RunSummary) -> list[str]:
        """Title + Run ID / Date / Duration + the Model(s) line.

        First block — returns the title with no leading blank (the caller prepends
        the blanks between sections).
        """
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
        return lines

    @staticmethod
    def _summary_section_lines(summary: RunSummary) -> list[str]:
        """The ``## Summary`` block: totals, success rate, and the P0 metrics.

        Returns a ``## Header``-led block with no leading blank (caller prepends it).
        """
        failed_line = f"- **Failed**: {summary.tasks_failed}"
        if summary.tasks_token_budget_exceeded or summary.tasks_cost_budget_exceeded:
            failed_line += (
                f" (incl. {summary.tasks_token_budget_exceeded} token budget, "
                f"{summary.tasks_cost_budget_exceeded} cost budget exceeded)"
            )

        lines = [
            "## Summary",
            "",
            f"- **Total Tasks**: {summary.tasks_run}",
            f"- **Succeeded**: {summary.tasks_succeeded}",
            failed_line,
            f"- **Errors**: {summary.tasks_error}",
            *_pass_rate_lines(summary),
        ]

        # Aggregate P0 metrics
        scores = [t["weighted_score"] for t in summary.task_results if t.get("weighted_score") is not None]
        durations = [t["duration"] for t in summary.task_results if t["duration"] > 0]
        similarities = [
            t["reference_similarity"] for t in summary.task_results if t.get("reference_similarity") is not None
        ]

        if scores:
            lines.append(f"- **Avg Reliability Score**: {sum(scores) / len(scores):.3f}")
        if durations:
            lines.append(f"- **Avg Generation Latency**: {sum(durations) / len(durations):.1f}s")

        total_asst_turns = sum(
            sum(t.get("assistant_turn_count", 0) for t in task.get("iterations", [])) for task in summary.task_results
        )
        if total_asst_turns > 0:
            lines.append(f"- **Total Assistant Turns**: {total_asst_turns}")

        crashed_total, recovered_partials, terminal_partials = _count_crashed_partials(summary.task_results)
        if crashed_total > 0:
            lines.append(
                f"- **Crashed Partials**: {crashed_total} "
                + f"({recovered_partials} recovered, {terminal_partials} terminal)"
            )

        if similarities:
            lines.append(f"- **Avg Ground Truth Similarity**: {sum(similarities) / len(similarities):.3f}")

        return lines

    @staticmethod
    def _task_details_lines(summary: RunSummary) -> list[str]:
        """The ``## Task Details`` dynamic-column table.

        The ``has_*`` column flags are computed over ALL task_results, then every row
        renders exactly the columns those flags enable. Returns a ``## Header``-led
        block with no leading blank (caller prepends it).
        """
        has_similarity = any(t.get("reference_similarity") is not None for t in summary.task_results)
        has_model = any(t.get("model_used") for t in summary.task_results)
        has_tags = any(t.get("tags") for t in summary.task_results)
        has_cmds_efficiency = any(t.get("commands_efficiency") is not None for t in summary.task_results)

        header = "| Task ID | Status | Reliability Score | Latency |"
        separator = "|---------|--------|-------------------|---------|"
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

        lines = ["## Task Details", "", header, separator]

        for task_result in summary.task_results:
            weighted_score = task_result.get("weighted_score")
            score_str = f"{weighted_score:.3f}" if weighted_score is not None else "N/A"
            duration = f"{task_result['duration']:.1f}s"

            row = f"| {task_result['task_id']} | {task_result['status']} | {score_str} | {duration} |"
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

        return lines

    @staticmethod
    def _runtime_notes_lines(summary: RunSummary) -> list[str]:
        """The ``## Run-time Notes`` blockquotes (max_turns exhaustion + expected_turns
        overage). Returns ``[]`` when there are no notes so the caller adds nothing —
        preserving the "only render the section when notes exist" behavior.
        """
        # Surface per-task signals as plain blockquote one-liners. Same surface for
        # both so the Markdown report has parity with the HTML report's badges.
        notes: list[str] = []
        for t in summary.task_results:
            task_id = t.get("task_id", "?")
            if t.get("max_turns_exhausted"):
                notes.append(f"> **WARNING:** [{task_id}] max_turns exhausted")
            overage_field = t.get("expected_turns_overage")
            if (
                isinstance(overage_field, (list, tuple))
                and len(overage_field) == 2
                and all(isinstance(x, int) for x in overage_field)
            ):
                actual, expected = overage_field
                notes.append(
                    f"> **WARNING:** [{task_id}] expected_turns exceeded: {actual}/{expected} (cumulative SDK turns)"
                )
            if t.get("stopped_early"):
                reason = t.get("early_stop_reason") or "unknown"
                turns_remaining = t.get("turns_remaining_at_stop")
                avoided = f" <= {turns_remaining} turn(s) avoided —" if isinstance(turns_remaining, int) else ""
                notes.append(
                    f"> **NOTE:** [{task_id}] stopped early ({reason});{avoided}"
                    + " gated on armed criteria only; other criteria are advisory"
                )
        if not notes:
            return []
        return ["## Run-time Notes", "", *notes]

    @staticmethod
    def _environment_lines(summary: RunSummary) -> list[str]:
        """The ``## Environment`` block. Returns a ``## Header``-led block with no leading
        blank (caller prepends it)."""
        lines = ["## Environment", ""]
        for key, value in summary.environment_info.items():
            lines.append(f"- **{key}**: {value}")
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
        lines = ReportGenerator._report_header_lines(summary)

        lines.append("")
        lines.extend(ReportGenerator._summary_section_lines(summary))

        lines.append("")
        lines.extend(ReportGenerator._task_details_lines(summary))

        notes_lines = ReportGenerator._runtime_notes_lines(summary)
        if notes_lines:
            lines.append("")
            lines.extend(notes_lines)

        # Generation Metrics section
        if any(t.get("iterations") for t in summary.task_results):
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
        settings_source, is_sdk = resolve_agent_settings(summary.task_results)
        if settings_source:
            lines.append("")
            lines.extend(ReportGenerator._generate_agent_settings_section(settings_source, is_sdk))

        # Installed Tools section (per-task tool versions from sandbox npm packages etc.)
        installed_tools_lines = ReportGenerator._generate_installed_tools_section(summary.task_results)
        if installed_tools_lines:
            lines.extend([""])
            lines.extend(installed_tools_lines)

        lines.append("")
        lines.extend(ReportGenerator._environment_lines(summary))

        return "\n".join(lines)

    @staticmethod
    def _generate_agent_settings_section(settings_source: dict[str, Any], is_sdk: bool) -> list[str]:
        """Generate Agent Settings markdown lines from a settings dict."""
        lines = ["## Agent Settings", ""]
        for label, value in collect_agent_settings_rows(settings_source, is_sdk):
            lines.append(f"- **{label}**: {value}")
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

        # Rows whose recorded spend is missing money, either because a turn's tokens
        # could not be priced or because a hard kill lost the in-flight turn
        # entirely. Worded cause-agnostically ("spend missing") because those two
        # causes reach the same conclusion and the report cannot always tell which
        # applied. Reported explicitly because the alternative is a total that reads
        # materially low with nothing to say the number is incomplete. Both of these
        # come from the same helpers RunSummary.tasks_unpriced /
        # eval_overhead_cost_usd use, so the report and run.json cannot disagree
        # about which rows lost money.
        unpriced = [t for t in task_results if row_cost_incomplete(t)]
        overhead = eval_overhead_costs(task_results)

        lines.append(f"**Total Tokens**: {total_tokens:,} (input: {total_input:,}, output: {total_output:,})")
        if total_cache_write > 0 or total_cache_read > 0:
            lines.append(f"**Cache Tokens**: write: {total_cache_write:,}, read: {total_cache_read:,}")
        # Judge/simulator spend is broken out only when there is some: it is a
        # property of the suite's criteria and identical across harnesses, so folding
        # it into the agent bill would make two harnesses look closer than they are.
        # **Total Cost** always means the whole bill.
        if overhead and total_cost is not None:
            lines.append(f"**Agent Cost**: ${total_cost:.4f}")
            lines.append(f"**Eval Overhead (judge + simulator)**: ${sum(overhead):.4f}")
        if total_cost is not None:
            cost_line = f"**Total Cost**: ${total_cost + sum(overhead):.4f}"
            if unpriced:
                cost_line += f" (floor — {len(unpriced)} task(s) have spend missing from this total)"
            lines.append(cost_line)
        elif unpriced:
            lines.append(f"**Total Cost**: unavailable — {len(unpriced)} task(s) have spend missing from this total")
        lines.append(f"**Avg Tokens/Task**: {total_tokens // len(tasks_with_tokens):,}")
        lines.append("")

        lines.extend(
            [
                "| Task ID | Input (uncached) | Output | Cache Write | Cache Read | Total | Cost |",
                "|---------|------------------|--------|-------------|------------|-------|------|",
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
                result = EvaluationResult.model_validate_json(report_path.read_text(encoding="utf-8"))
                all_turns.extend(result.iterations)
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
                return report_md_path.read_text(encoding="utf-8"), report_md_path

            if summary_json_path.exists():
                from .models import RunSummary

                summary = RunSummary.model_validate_json(summary_json_path.read_text(encoding="utf-8"))
                report_md = ReportGenerator.generate_markdown(summary, run_dir=run_dir)
                return report_md, summary_json_path

        raise FileNotFoundError(
            f"No report found in {run_dir}. Expected experiment.md, experiment.json, run.md, or run.json"
        )


def _evaluate_thresholds(
    aggregate: CriterionAggregate, suite_thresholds: dict[str, float] | None
) -> CriterionAggregate:
    """Return a new CriterionAggregate with threshold_checks + passed filled in.

    When a threshold references a metric the aggregate didn't produce, the check
    records actual_value=None and fails.
    """
    if not suite_thresholds:
        return aggregate.model_copy(update={"threshold_checks": [], "passed": True})

    checks: list[ThresholdCheck] = []
    for metric, min_value in suite_thresholds.items():
        actual = aggregate.metrics.get(metric)
        passed = actual is not None and actual >= min_value
        checks.append(ThresholdCheck(metric=metric, min_value=min_value, actual_value=actual, passed=passed))

    return aggregate.model_copy(update={"threshold_checks": checks, "passed": all(c.passed for c in checks)})


def _attach_row_accounting(agg: CriterionAggregate, rows_total: int, rows_aggregated: int) -> CriterionAggregate:
    """Record the suite-size denominator on an aggregate and derive completion_rate.

    ERROR rows carry ``success_criteria_results=[]`` and are silently dropped by
    the per-criterion position slice, shrinking the denominator behind metrics
    like ``recall``. This stamps the visible counts (``rows_total`` /
    ``rows_excluded``) and mirrors a gateable ``completion_rate`` into ``metrics``
    so a ``suite_thresholds: {completion_rate: ...}`` gate can fail when too many
    rows errored. ``rows_aggregated`` is ``len(per_rows)`` — never read back from
    ``metrics["count"]`` (the missing-aggregator stub has no ``count`` key).
    """
    excluded = rows_total - rows_aggregated
    metrics = dict(agg.metrics)
    metrics["completion_rate"] = (rows_aggregated / rows_total) if rows_total else 0.0
    return agg.model_copy(update={"rows_total": rows_total, "rows_excluded": excluded, "metrics": metrics})


def _build_missing_aggregator(
    criterion_type: str, suite_thresholds: dict[str, float], description: str | None = None
) -> CriterionAggregate:
    """A stub aggregate used when a criterion declares suite_thresholds but its
    checker doesn't implement ``aggregate()``. Marks every threshold as failed
    with no actual value and records an error."""
    checks = [
        ThresholdCheck(metric=metric, min_value=min_value, actual_value=None, passed=False)
        for metric, min_value in suite_thresholds.items()
    ]
    return CriterionAggregate(
        criterion_type=criterion_type,
        description=description,
        metrics={},
        threshold_checks=checks,
        passed=False,
        details={},
        error="Criterion checker did not produce an aggregate, so thresholds cannot be evaluated.",
    )


def _compute_suite_rollup(
    suite_id: str,
    variant_id: str,
    rows: list[TaskResult],
    run_dir: Path,
    task_criteria: list[Any] | None = None,
) -> SuiteRollup:
    """Compute a SuiteRollup from a list of row-level TaskResults in one variant.

    ``task_criteria`` is the original ``success_criteria`` list from the task
    definition. It drives per-criterion ``aggregate()`` invocation and
    ``suite_thresholds`` evaluation. Pass None when unavailable — per-criterion
    stats still compute but no aggregate/threshold gating happens.
    """
    from .criteria import CriterionRegistry, init_criteria

    rows_total = len(rows)
    rows_passed = sum(1 for r in rows if r.result.final_status.category == "succeeded")
    rows_failed = sum(1 for r in rows if r.result.final_status.category == "failed")
    rows_error = sum(1 for r in rows if r.result.final_status.category == "error")

    scored = [r.result.weighted_score for r in rows if r.result.weighted_score is not None]
    average_weighted_score = sum(scored) / len(scored) if scored else None

    # Per-criterion-type tallies (scores + errors) for the type-level summary.
    by_type: dict[str, list[float]] = defaultdict(list)
    errors_by_type: dict[str, int] = defaultdict(int)
    for row in rows:
        for cr in row.result.success_criteria_results:
            by_type[cr.criterion_type].append(cr.score)
            if cr.error is not None:
                errors_by_type[cr.criterion_type] += 1

    criterion_stats = [
        CriterionStats(
            criterion_type=ctype,
            rows_evaluated=len(scores),
            average_score=sum(scores) / len(scores) if scores else 0.0,
            error_count=errors_by_type.get(ctype, 0),
        )
        for ctype, scores in sorted(by_type.items())
    ]

    # Drive each criterion's aggregate() + evaluate suite_thresholds. Per-row
    # results are sliced per criterion INSTANCE by position: SuccessChecker.
    # check_all appends one CriterionResult per criterion in declared order
    # (evaluation/checker.py), so row.success_criteria_results[i] belongs to
    # task_criteria[i]. Aggregating per-instance (not pooled by type) is what
    # lets a task stack many criteria of the SAME type — e.g. activation's
    # per-skill skill_triggered criteria — and get a distinct aggregate
    # (per-skill recall / F1) for each, instead of one type-pooled number
    # repeated once per instance. The aggregate carries the criterion's
    # description so the stacked instances stay distinguishable downstream.
    criterion_aggregates: list[CriterionAggregate] = []
    if task_criteria is not None:
        init_criteria(validate=False)
        for i, criterion in enumerate(task_criteria):
            ctype = criterion.type
            per_rows = [
                row.result.success_criteria_results[i] for row in rows if i < len(row.result.success_criteria_results)
            ]
            suite_thresholds = getattr(criterion, "suite_thresholds", None)
            description = getattr(criterion, "description", None)
            try:
                checker_cls = CriterionRegistry.get_checker(ctype)
            except KeyError:
                logger.warning("No checker registered for criterion type %s; skipping aggregate", ctype)
                continue
            checker = checker_cls()
            aggregate = checker.aggregate(criterion, per_rows)
            if aggregate is None:
                if suite_thresholds:
                    # Thresholds declared but nothing produced — fail loudly.
                    stub = _build_missing_aggregator(ctype, suite_thresholds, description)
                    stub = _attach_row_accounting(stub, rows_total, len(per_rows))
                    # completion_rate is now a real value in metrics; refresh the
                    # threshold checks so the rendered actual matches it, but keep
                    # the aggregate failed because the real metrics are absent.
                    stub = _evaluate_thresholds(stub, suite_thresholds).model_copy(update={"passed": False})
                    criterion_aggregates.append(stub)
                continue
            aggregate = aggregate.model_copy(update={"description": description})
            aggregate = _attach_row_accounting(aggregate, rows_total, len(per_rows))
            criterion_aggregates.append(_evaluate_thresholds(aggregate, suite_thresholds))

    suite_passed = all(a.passed for a in criterion_aggregates)

    # Sample up to K failed/errored rows for error analysis
    failed_samples: list[FailedRowSummary] = []
    for row in rows:
        if row.result.final_status.category == "succeeded":
            continue
        if len(failed_samples) >= _FAILED_SAMPLE_LIMIT:
            break
        reasons: list[str] = []
        for cr in row.result.success_criteria_results:
            # Informational (weight: 0) criteria cannot fail a row, so they must
            # not supply the reason the row is reported as failed.
            if not cr.gating:
                continue
            if cr.error is not None or cr.score < cr.pass_threshold:
                reason = cr.error or cr.details or f"{cr.criterion_type}: score={cr.score:.2f}"
                reasons.append(reason[:_FAILURE_REASON_MAX_LEN])
            if len(reasons) >= _FAILURE_REASONS_PER_ROW:
                break
        task_json_path = (
            build_task_run_dir(run_dir, variant_id, row.task_id, replicate_index=row.replicate_index) / "task.json"
        )
        try:
            # Persist as POSIX — this value lands in suite.json and in
            # suite.md markdown links, both of which must be platform-agnostic.
            rel = task_json_path.relative_to(run_dir).as_posix()
        except ValueError:
            rel = task_json_path.as_posix()
        failed_samples.append(
            FailedRowSummary(
                row_id=row.row_id,
                task_id=row.task_id,
                final_status=row.result.final_status,
                weighted_score=row.result.weighted_score,
                failure_reasons=reasons,
                error_message=row.result.error_message,
                task_json_relpath=rel,
                replicate_index=row.replicate_index,
            )
        )

    return SuiteRollup(
        suite_id=suite_id,
        variant_id=variant_id,
        rows_total=rows_total,
        rows_passed=rows_passed,
        rows_failed=rows_failed,
        rows_error=rows_error,
        pass_rate=rows_passed / rows_total if rows_total else 0.0,
        average_weighted_score=average_weighted_score,
        criterion_stats=criterion_stats,
        failed_samples=failed_samples,
        criterion_aggregates=criterion_aggregates,
        passed=suite_passed,
    )


def _render_suite_markdown(rollup: SuiteRollup) -> str:
    """Render a SuiteRollup as a concise markdown report."""
    lines: list[str] = [
        f"# Suite Rollup: {rollup.suite_id}",
        "",
        f"**Variant**: `{rollup.variant_id}`",
        (
            f"**Rows**: {rollup.rows_total} total — "
            f"{rollup.rows_passed} passed, {rollup.rows_failed} failed, {rollup.rows_error} errored"
        ),
        f"**Pass rate**: {rollup.pass_rate * 100:.1f}%",
    ]
    if rollup.average_weighted_score is not None:
        lines.append(f"**Average weighted score**: {rollup.average_weighted_score:.3f}")

    if rollup.criterion_stats:
        lines.extend(
            [
                "",
                "## Criterion stats",
                "",
                "| Criterion | Rows | Avg score | Errors |",
                "|---|---|---|---|",
            ]
        )
        for cs in rollup.criterion_stats:
            lines.append(f"| `{cs.criterion_type}` | {cs.rows_evaluated} | {cs.average_score:.3f} | {cs.error_count} |")

    if rollup.criterion_aggregates:
        for aggregate in rollup.criterion_aggregates:
            lines.extend(_render_criterion_aggregate(aggregate))
        lines.extend(
            [
                "",
                f"**Suite gate**: {'PASSED' if rollup.passed else 'FAILED'}",
            ]
        )

    if rollup.failed_samples:
        lines.extend(
            [
                "",
                f"## Failed/errored samples (up to {_FAILED_SAMPLE_LIMIT})",
                "",
            ]
        )
        for s in rollup.failed_samples:
            lines.append(f"### `{s.task_id}` — {s.final_status.value}")
            if s.weighted_score is not None:
                lines.append(f"- score: {s.weighted_score:.3f}")
            if s.error_message:
                lines.append(f"- error: {s.error_message[:_FAILURE_REASON_MAX_LEN]}")
            for r in s.failure_reasons:
                lines.append(f"- {r}")
            # Strip the leading variant segment so the link resolves from the
            # suite dir where suite.md lives. PurePosixPath keeps the separator
            # POSIX on Windows too. Fall back to the raw relpath if it isn't
            # prefixed with the variant (e.g. serialized from a legacy shape).
            rel_path = PurePosixPath(s.task_json_relpath)
            try:
                suite_rel: PurePosixPath = rel_path.relative_to(rollup.variant_id)
            except ValueError:
                suite_rel = rel_path
            lines.append(f"- [task.json](./{suite_rel})")
            lines.append("")

    return "\n".join(lines) + "\n"


def _render_criterion_aggregate(aggregate: CriterionAggregate) -> list[str]:
    """Render one CriterionAggregate as a markdown section.

    Shape:
      - flat metrics table (always)
      - threshold_checks table (when thresholds were configured)
      - confusion matrix (when details carry 'labels' + 'confusion' — by convention)
      - per-label P/R/F1 table (when details carry 'per_label')
    """
    status = "PASSED" if aggregate.passed else "FAILED"
    lines: list[str] = [
        "",
        f"## Aggregate metrics — `{aggregate.criterion_type}` ({status})",
        "",
    ]
    if aggregate.error:
        lines.append(f"_Error: {aggregate.error}_")
        lines.append("")

    if aggregate.rows_excluded > 0:
        included = aggregate.rows_total - aggregate.rows_excluded
        lines.append(
            f"_Denominator: {included}/{aggregate.rows_total} rows"
            + f" ({aggregate.rows_excluded} excluded — errored before criteria ran)_"
        )
        lines.append("")

    if aggregate.metrics:
        lines.extend(["| metric | value |", "|---|---|"])
        for key in sorted(aggregate.metrics):
            lines.append(f"| `{key}` | {aggregate.metrics[key]:.3f} |")

    if aggregate.threshold_checks:
        lines.extend(
            [
                "",
                "### Thresholds",
                "",
                "| metric | minimum | actual | passed |",
                "|---|---|---|---|",
            ]
        )
        for check in aggregate.threshold_checks:
            actual = f"{check.actual_value:.3f}" if check.actual_value is not None else "—"
            passed = "✓" if check.passed else "✗"
            lines.append(f"| `{check.metric}` | {check.min_value:.3f} | {actual} | {passed} |")

    per_label = aggregate.details.get("per_label") if aggregate.details else None
    if isinstance(per_label, list) and per_label:
        lines.extend(
            [
                "",
                "### Per-label breakdown",
                "",
                "| label | precision | recall | f1 | support |",
                "|---|---|---|---|---|",
            ]
        )
        for row in per_label:
            lines.append(
                f"| `{row['label']}` | {row['precision']:.3f} | {row['recall']:.3f}"
                + f" | {row['f1']:.3f} | {row['support']} |"
            )

    labels = aggregate.details.get("labels") if aggregate.details else None
    confusion = aggregate.details.get("confusion") if aggregate.details else None
    if isinstance(labels, list) and isinstance(confusion, list) and confusion:
        lines.extend(
            [
                "",
                "### Confusion matrix",
                "",
                "| expected \\\\ observed | " + " | ".join(f"`{lbl}`" for lbl in labels) + " |",
                "|---" * (len(labels) + 1) + "|",
            ]
        )
        grid: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for entry in confusion:
            grid[entry["expected"]][entry["observed"]] = entry["count"]
        for expected in labels:
            cells = [str(grid[expected].get(observed, 0)) for observed in labels]
            lines.append(f"| `{expected}` | " + " | ".join(cells) + " |")

    return lines


def write_suite_rollups(
    run_dir: Path,
    task_results: list[TaskResult],
    resolved_tasks: list[Any] | None = None,
) -> list[SuiteRollup]:
    """Write per-suite pass-rate rollups for all dataset-backed tasks in this run.

    Groups results by ``(variant_id, suite_id)`` for rows where ``suite_id`` is
    set by the dataset expander. Non-dataset tasks are ignored — this is a
    no-op when no task used ``dataset:``.

    ``resolved_tasks`` carries the resolved TaskDefinitions used to drive
    across-row ``aggregate()`` and to evaluate each criterion's
    ``suite_thresholds``. When omitted, aggregates + threshold gating are
    skipped (per-criterion stats + failed-sample listing still work).

    For each group, writes:
        ``<run_dir>/<variant_id>/<suite_id>/suite.json``
        ``<run_dir>/<variant_id>/<suite_id>/suite.md``

    Returns the computed SuiteRollup objects (useful for CLI exit-code logic).
    """
    groups: dict[tuple[str, str], list[TaskResult]] = defaultdict(list)
    for tr in task_results:
        if tr.suite_id is None:
            continue
        groups[(tr.variant_id, tr.suite_id)].append(tr)

    # Map (variant_id, suite_id) -> task_criteria from the first matching resolved task.
    # Rows in the same suite share identical criteria (expand_dataset copies them).
    criteria_by_group: dict[tuple[str, str], list[Any]] = {}
    if resolved_tasks is not None:
        for rt in resolved_tasks:
            task = rt.task
            if task.suite_id is None:
                continue
            key = (rt.variant_id, task.suite_id)
            if key not in criteria_by_group:
                criteria_by_group[key] = list(task.success_criteria)

    rollups: list[SuiteRollup] = []
    for (variant_id, suite_id), rows in groups.items():
        suite_dir = run_dir / variant_id / suite_id
        suite_dir.mkdir(parents=True, exist_ok=True)
        task_criteria = criteria_by_group.get((variant_id, suite_id))
        rollup = _compute_suite_rollup(suite_id, variant_id, rows, run_dir, task_criteria=task_criteria)
        (suite_dir / "suite.json").write_text(rollup.model_dump_json(indent=2), encoding="utf-8")
        (suite_dir / "suite.md").write_text(_render_suite_markdown(rollup), encoding="utf-8")
        rollups.append(rollup)
        logger.info(
            "Wrote suite rollup: variant=%s suite=%s pass_rate=%.1f%% (%d/%d) gate=%s",
            variant_id,
            suite_id,
            rollup.pass_rate * 100,
            rollup.rows_passed,
            rollup.rows_total,
            "PASS" if rollup.passed else "FAIL",
        )
    return rollups
