"""Tests for HTML report generation (coder_eval.reports_html)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.models import (
    AgentConfig,
    AgentKind,
    CommandStatistics,
    CommandTelemetry,
    CriterionResult,
    EvaluationResult,
    ExperimentDefinition,
    ExperimentResult,
    ExperimentVariant,
    FinalStatus,
    PromptPrefix,
    SlowestCommandInfo,
    TaskExperimentSummary,
    TokenUsage,
    TurnRecord,
    VariantAggregate,
    VariantResult,
)
from coder_eval.reports_html import (
    HTMLReportGenerator,
    safe_write,
    write_experiment_html,
    write_task_html,
    write_variant_html,
)


def _make_command(
    tool_name: str,
    seq: int,
    status: str = "success",
    duration_ms: float = 120.0,
    parameters: dict | None = None,
    result_summary: str | None = None,
    error_message: str | None = None,
) -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=f"toolu_{seq:04d}",
        timestamp=datetime(2026, 1, 1, 12, 0, seq),
        duration_ms=duration_ms,
        parameters=parameters or {},
        result_status=status,  # type: ignore[arg-type]
        result_summary=result_summary,
        error_message=error_message,
        sequence_number=seq,
    )


def _make_result(
    *,
    final_status: FinalStatus = FinalStatus.SUCCESS,
    iterations: list[TurnRecord] | None = None,
    criteria: list[CriterionResult] | None = None,
    error_message: str | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        task_id="sample-task",
        task_description="Sample description for HTML test",
        variant_id="default",
        agent_type=AgentKind.CLAUDE_CODE,
        model_used="claude-sonnet-4-6",
        started_at=datetime(2026, 1, 1, 12, 0, 0),
        completed_at=datetime(2026, 1, 1, 12, 1, 30),
        duration_seconds=90.0,
        final_status=final_status,
        weighted_score=0.9 if final_status == FinalStatus.SUCCESS else 0.2,
        iteration_count=1,
        success_criteria_results=criteria or [],
        iterations=iterations or [],
        error_message=error_message,
    )


def test_task_html_renders_judge_section():
    """JudgeCriterionResult renders in a dedicated 'Judge Verdicts' section with
    a per-judge card showing rationale, findings, and transcript disclosure."""
    from coder_eval.models import JudgeCriterionResult, JudgeTranscript, JudgeTranscriptToolCall

    judge = JudgeCriterionResult(
        criterion_type="agent_judge",
        description="grade project",
        score=0.7,
        details="score=0.700\nrationale: workflow looks correct overall",
        findings=["main.xaml passes xmllint — correct", "tests/ directory missing — issue"],
        transcript=JudgeTranscript(
            tool_calls=[
                JudgeTranscriptToolCall(
                    tool_name="Bash",
                    detail="xmllint --noout main.xaml",
                    status="success",
                    result_preview="OK (no errors)",
                ),
                JudgeTranscriptToolCall(
                    tool_name="Read",
                    detail="main.xaml",
                    status="success",
                    result_preview="File read: 482 bytes",
                ),
            ],
            duration_seconds=2.5,
            raw_verdict='{"score": 0.7, "rationale": "ok"}',
            truncated=False,
        ),
    )
    result = _make_result(criteria=[judge])
    html = HTMLReportGenerator.generate_task_html(result)

    # Dedicated section — appears after the criteria table.
    assert "Judge Verdicts (1)" in html
    assert 'class="judge-section"' in html
    assert 'class="judge-card' in html
    # Rationale extracted from details.
    assert "workflow looks correct overall" in html
    # Findings rendered as a list (open by default).
    assert "Findings (2)" in html
    assert "main.xaml passes xmllint" in html
    # Transcript disclosure stays collapsible.
    assert "Judge transcript (2 tool calls)" in html
    assert "xmllint --noout main.xaml" in html
    assert "duration: 2.5s" in html
    assert "Raw verdict" in html


def test_task_html_judge_section_round_trips_from_extras():
    """Reading task.json back into a base CriterionResult preserves judge fields
    via ``extra='allow'`` — the Judge Verdicts section must surface them whether
    they arrive as typed JudgeCriterionResult or as model_extra dicts."""
    base = CriterionResult(
        criterion_type="agent_judge",
        description="grade project",
        score=0.6,
        details="score=0.600\nrationale: round-tripped",
        # These come in as extras after a model_validate_json round-trip.
        findings=["finding via extras"],  # type: ignore[call-arg]
        transcript={
            "tool_calls": [
                {
                    "tool_name": "Grep",
                    "detail": "pattern=foo",
                    "status": "success",
                    "result_preview": "match",
                }
            ],
            "duration_seconds": 1.0,
            "raw_verdict": "raw",
            "truncated": False,
            "token_usage": None,
        },  # type: ignore[call-arg]
    )
    result = _make_result(criteria=[base])
    html = HTMLReportGenerator.generate_task_html(result)

    assert "Judge Verdicts (1)" in html
    assert "round-tripped" in html
    assert "finding via extras" in html
    assert "pattern=foo" in html


def test_task_html_non_judge_criterion_no_judge_section():
    """A regular file_exists criterion should NOT emit a Judge Verdicts section."""
    result = _make_result(
        criteria=[
            CriterionResult(
                criterion_type="file_exists",
                description="Main.cs exists",
                score=1.0,
                details="File exists",
            )
        ],
    )
    html = HTMLReportGenerator.generate_task_html(result)
    assert "Judge Verdicts" not in html
    assert "Judge transcript" not in html
    assert "Findings (" not in html


def test_task_html_minimal_success():
    result = _make_result(
        criteria=[
            CriterionResult(
                criterion_type="file_exists",
                description="Main.cs exists",
                score=1.0,
                details="File exists",
                error=None,
            )
        ],
        iterations=[
            TurnRecord(
                iteration=1,
                user_input="Create Main.cs",
                agent_output="done",
                commands=[
                    _make_command("Write", 0, parameters={"file_path": "Main.cs"}, result_summary="File created"),
                ],
                token_usage=TokenUsage(input_tokens=150, output_tokens=80),
            )
        ],
    )
    html = HTMLReportGenerator.generate_task_html(result)

    # Structural sanity
    assert "<!DOCTYPE html>" in html
    assert "<title>coder_eval · sample-task · default</title>" in html
    # Status badge
    assert "SUCCESS" in html
    # Criteria table rendered
    assert "Main.cs exists" in html
    assert "file_exists" in html
    # Trace rendered
    assert "Conversation Trace" in html
    assert "Write" in html


def test_task_html_error_case_with_empty_turns():
    """When an agent fails before producing turns, HTML must still render and
    expose the error details, not crash."""
    result = _make_result(
        final_status=FinalStatus.ERROR,
        error_message="Communication with agent failed",
    )
    result.error_details = {
        "error_category": "agent_communication",
        "error_message": "Communication with agent failed",
        "component": "orchestrator.iteration_1",
        "is_retryable": False,
    }
    html = HTMLReportGenerator.generate_task_html(result)

    assert "ERROR" in html
    assert "Communication with agent failed" in html
    assert "agent_communication" in html
    assert "No turn data" in html
    # Without error_log_tail or stack_trace, the Logs disclosure is omitted.
    assert "Stack trace" not in html
    assert "<summary>Logs</summary>" not in html


def test_task_html_error_renders_error_log_tail():
    """``error_log_tail`` on the result drives the Logs disclosure."""
    result = _make_result(
        final_status=FinalStatus.ERROR,
        error_message="Communication with agent failed",
    )
    result.error_log_tail = (
        "2026-04-28 12:00:00 [INFO] coder_eval.orchestrator: starting task\n"
        "2026-04-28 12:00:01 [ERROR] coder_eval.orchestrator: <crashed> & burned\n"
    )

    html = HTMLReportGenerator.generate_task_html(result)

    assert "<summary>Logs</summary>" in html
    # HTML metacharacters in the tail are escaped before injection.
    assert "&lt;crashed&gt; &amp; burned" in html
    assert "<crashed>" not in html
    assert "Communication with agent failed" in html


def test_task_html_error_falls_back_to_stack_trace():
    """When error_log_tail is missing, the renderer falls back to the legacy
    stack_trace stored in error_details so reports regenerated against
    archived runs (pre-error_log_tail) still show diagnostics."""
    result = _make_result(
        final_status=FinalStatus.ERROR,
        error_message="Communication with agent failed",
    )
    result.error_details = {
        "error_category": "agent_communication",
        "is_retryable": False,
        "stack_trace": "Traceback (most recent call last):\n  RuntimeError: legacy boom",
    }

    html = HTMLReportGenerator.generate_task_html(result)

    assert "<summary>Logs</summary>" in html
    assert "RuntimeError: legacy boom" in html


def test_task_html_error_log_tail_takes_precedence_over_stack_trace():
    """When both are present, error_log_tail wins (richer, sanitised, bounded)."""
    result = _make_result(
        final_status=FinalStatus.ERROR,
        error_message="boom",
    )
    result.error_log_tail = "agent crashed at iteration 3"
    result.error_details = {
        "error_category": "agent_communication",
        "stack_trace": "Traceback: legacy stack frame",
    }

    html = HTMLReportGenerator.generate_task_html(result)

    assert "<summary>Logs</summary>" in html
    assert "agent crashed at iteration 3" in html
    assert "legacy stack frame" not in html


def test_task_html_error_no_logs_when_no_tail_and_no_stack_trace():
    """No Logs disclosure when neither tail nor stack_trace is present."""
    result = _make_result(
        final_status=FinalStatus.ERROR,
        error_message="Communication with agent failed",
    )
    result.error_details = {
        "error_category": "agent_communication",
        "is_retryable": True,
    }

    html = HTMLReportGenerator.generate_task_html(result)

    assert "<summary>Logs</summary>" not in html
    assert "Communication with agent failed" in html


def test_task_html_truncates_long_parameters():
    """Long tool parameters should be truncated, not embedded verbatim."""
    huge = "X" * 2000
    result = _make_result(
        iterations=[
            TurnRecord(
                iteration=1,
                user_input="do it",
                agent_output="ok",
                commands=[
                    _make_command(
                        "Write",
                        0,
                        parameters={"file_path": "big.txt", "content": huge},
                        result_summary="ok",
                    )
                ],
            )
        ],
    )
    html = HTMLReportGenerator.generate_task_html(result)
    # Original string should be truncated — full 2000 X's must not appear.
    assert "X" * 2000 not in html
    # But truncation notice should appear.
    assert "more chars" in html


def test_task_html_command_stats_rendered():
    result = _make_result()
    result.command_stats = CommandStatistics(
        total_commands=5,
        successful_commands=4,
        failed_commands=1,
        commands_by_tool={"Bash": 3, "Read": 2},
        total_command_time_ms=1500.0,
        avg_command_time_ms=300.0,
        slowest_commands=[
            SlowestCommandInfo(tool="Bash", duration_ms=800.0, parameters={"cmd": "ls"}),
        ],
    )
    html = HTMLReportGenerator.generate_task_html(result)
    assert "Command Telemetry" in html
    assert "Bash" in html
    assert "Read" in html


def test_multiline_criterion_details_render_collapsibly():
    """Criteria with multi-line details (e.g. run_command capturing command
    + exit + stdout + stderr) should collapse into a <details> element with
    the first line as the summary so long output doesn't bloat the table."""
    result = _make_result(
        criteria=[
            CriterionResult(
                criterion_type="run_command",
                description="uip rpa get-errors",
                score=0.0,
                details=(
                    "Command: uip rpa get-errors --project-dir .\n"
                    "Exit code: 1 (expected: 0)\n"
                    "Stdout: (empty)\n"
                    "Stderr:\nProject not found: ."
                ),
                error=None,
            )
        ],
    )
    html = HTMLReportGenerator.generate_task_html(result)
    assert "<details><summary" in html
    # The summary shows the first line (command) so reviewers see at a
    # glance what was run without expanding.
    assert "Command: uip rpa get-errors --project-dir ." in html
    assert "Project not found" in html


def test_singleline_criterion_details_render_inline():
    """One-line details keep their current inline rendering — no <details>."""
    result = _make_result(
        criteria=[
            CriterionResult(
                criterion_type="file_exists",
                description="hello.txt exists",
                score=1.0,
                details="File 'hello.txt' exists",
                error=None,
            )
        ],
    )
    html = HTMLReportGenerator.generate_task_html(result)
    assert "File &#x27;hello.txt&#x27; exists" in html
    # Should not be wrapped in a <details> block for single-line details.
    row = html[html.index("hello.txt exists") :]
    row = row[: row.find("</tr>")]
    assert "<details>" not in row


def test_write_task_html_creates_file(tmp_path: Path):
    result = _make_result()
    out = tmp_path / "sub" / "task.html"
    written = write_task_html(result, out)
    assert written == out
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "sample-task" in content


def _variant_agg(
    variant_id: str = "v1",
    tasks_run: int = 3,
    tasks_succeeded: int = 2,
    tasks_failed: int = 1,
    tasks_error: int = 0,
    average_score: float = 0.65,
    average_duration: float = 12.0,
) -> VariantAggregate:
    return VariantAggregate(
        variant_id=variant_id,
        tasks_run=tasks_run,
        tasks_succeeded=tasks_succeeded,
        tasks_failed=tasks_failed,
        tasks_error=tasks_error,
        average_score=average_score,
        average_duration=average_duration,
    )


def test_generate_variant_html_basic():
    agg = _variant_agg()
    task_links: list[tuple[str, str, float | None, str]] = [
        ("t1", "t1/task.html", 1.0, "SUCCESS"),
        ("t2", "t2/task.html", 0.5, "FAILURE"),
        ("t3", "t3/task.html", 0.45, "FAILURE"),
    ]
    html = HTMLReportGenerator.generate_variant_html("v1", agg, task_links)

    assert "<!DOCTYPE html>" in html
    assert "Variant: v1" in html
    assert ">3<" in html  # tasks_run
    assert ">2<" in html  # tasks_succeeded
    assert "0.65" in html  # avg score pill
    for _, link, *_ in task_links:
        assert link in html


def test_generate_variant_html_with_zero_average_renders_digits():
    """average_score=0.0 must render as '0.00' — a phantom `or 0.0` fallback
    would have hidden legitimate zeros behind a placeholder."""
    agg = _variant_agg(average_score=0.0)
    html = HTMLReportGenerator.generate_variant_html("v1", agg, [])
    assert "0.00" in html


def _experiment_result(variant_ids: list[str]) -> ExperimentResult:
    aggregates = {vid: _variant_agg(variant_id=vid) for vid in variant_ids}
    return ExperimentResult(
        experiment_id="exp-1",
        description="test experiment",
        variant_ids=variant_ids,
        task_summaries=[],
        variant_aggregates=aggregates,
        total_duration_seconds=30.0,
    )


def _result_with_summaries(
    variant_ids: list[str],
    per_task: list[dict[str, list[float]]],
) -> ExperimentResult:
    """Build an ExperimentResult with task_summaries matching per_task scores."""
    summaries: list[TaskExperimentSummary] = []
    for i, entry in enumerate(per_task, start=1):
        variant_results: list[VariantResult] = []
        best_score = max(max(entry[vid]) for vid in variant_ids if entry.get(vid))
        best_variant = next(vid for vid in variant_ids if entry.get(vid) and max(entry[vid]) == best_score)
        for vid in variant_ids:
            scores = entry.get(vid, [0.0])
            for score in scores:
                variant_results.append(
                    VariantResult(
                        variant_id=vid,
                        task_id=f"t{i}",
                        weighted_score=score,
                        final_status=FinalStatus.SUCCESS if score >= 0.9 else FinalStatus.FAILURE,
                        duration_seconds=1.0,
                        total_tokens=100,
                        iteration_count=1,
                        total_assistant_turns=1,
                    )
                )
        per_variant_scores = [max(entry[vid]) for vid in variant_ids if entry.get(vid)]
        spread = max(per_variant_scores) - min(per_variant_scores) if per_variant_scores else 0.0
        is_tie = len({round(s, 6) for s in per_variant_scores}) == 1
        summaries.append(
            TaskExperimentSummary(
                task_id=f"t{i}",
                variant_results=variant_results,
                best_variant=best_variant,
                is_tie=is_tie,
                score_spread=spread,
            )
        )
    aggregates = {vid: _variant_agg(variant_id=vid) for vid in variant_ids}
    return ExperimentResult(
        experiment_id="exp-1",
        description="test experiment",
        variant_ids=variant_ids,
        task_summaries=summaries,
        variant_aggregates=aggregates,
        total_duration_seconds=30.0,
    )


def test_generate_experiment_html_basic():
    result = _experiment_result(["v1", "v2"])
    experiment = ExperimentDefinition(
        experiment_id="exp-1",
        description="d",
        variants=[ExperimentVariant(variant_id="v1"), ExperimentVariant(variant_id="v2")],
    )
    html = HTMLReportGenerator.generate_experiment_html(result, experiment)

    assert "Experiment: exp-1" in html
    assert "v1/variant.html" in html
    assert "v2/variant.html" in html


def test_generate_experiment_html_without_definition():
    """When experiment is None, title falls back to result.experiment_id and description is empty."""
    result = _experiment_result(["v1"])
    html = HTMLReportGenerator.generate_experiment_html(result, None)
    assert "Experiment: exp-1" in html


def test_variant_html_includes_stddev_when_result_provided():
    """Passing an ExperimentResult enables Score / Duration stddev rows."""
    result = _result_with_summaries(["v1"], [{"v1": [0.8]}, {"v1": [0.6]}, {"v1": [0.4]}])
    agg = result.variant_aggregates["v1"]
    html = HTMLReportGenerator.generate_variant_html("v1", agg, [], result=result)
    assert "Score Stddev" in html


def test_variant_html_loads_rich_sections_from_run_dir(tmp_path: Path):
    """When run_dir is provided, rich sections load from per-task JSON."""
    result = _result_with_summaries(["v1"], [{"v1": [0.8]}])

    # Write a realistic task.json to disk under <variant>/<task>/<NN>/
    task_json_dir = tmp_path / "v1" / "t1" / "00"
    task_json_dir.mkdir(parents=True)
    eval_result = _make_result()
    eval_result.task_id = "t1"
    eval_result.total_token_usage = TokenUsage(input_tokens=100, output_tokens=50, total_cost_usd=0.02)
    eval_result.sdk_options = {"permission_mode": "auto", "allowed_tools": ["Bash"], "model": "m"}
    eval_result.environment_info = {"framework_version": "0.1"}
    (task_json_dir / "task.json").write_text(eval_result.model_dump_json())

    agg = result.variant_aggregates["v1"]
    html = HTMLReportGenerator.generate_variant_html("v1", agg, [], result=result, run_dir=tmp_path)
    assert "Token Usage" in html
    assert "Agent Settings" in html


def test_variant_html_falls_back_without_run_dir():
    """Without run_dir, only the summary table renders — no rich sections."""
    result = _result_with_summaries(["v1"], [{"v1": [0.8]}])
    agg = result.variant_aggregates["v1"]
    html = HTMLReportGenerator.generate_variant_html("v1", agg, [], result=result)
    assert "<h2>Token Usage</h2>" not in html
    assert "<h2>Agent Settings</h2>" not in html


def test_experiment_html_aggregate_metrics_no_p_values_for_three_variants():
    """3-variant experiment omits the p-value column."""
    result = _result_with_summaries(
        ["v1", "v2", "v3"],
        [
            {"v1": [0.9], "v2": [0.85], "v3": [0.80]},
            {"v1": [0.80], "v2": [0.70], "v3": [0.60]},
            {"v1": [0.95], "v2": [0.90], "v3": [0.85]},
        ],
    )
    html = HTMLReportGenerator.generate_experiment_html(result, None)
    # Header row contains variant ids but not "p-value"
    agg_section = html[html.index("<h2>Aggregate Metrics</h2>") :]
    agg_section = agg_section[: agg_section.find("<h2>", 20)]
    assert "p-value" not in agg_section


def test_experiment_html_aggregate_metrics_with_p_value_for_two_variants():
    """2-variant experiment shows a p-value column."""
    result = _result_with_summaries(
        ["v1", "v2"],
        [
            {"v1": [0.9], "v2": [0.5]},
            {"v1": [0.85], "v2": [0.55]},
            {"v1": [0.95], "v2": [0.60]},
        ],
    )
    html = HTMLReportGenerator.generate_experiment_html(result, None)
    assert "p-value" in html


def test_experiment_html_win_rates_and_per_task_comparison():
    """Win Rates + Per-Task Comparison reflect best_variant per task."""
    result = _result_with_summaries(
        ["v1", "v2"],
        [
            {"v1": [1.0], "v2": [0.5]},
            {"v1": [0.9], "v2": [0.4]},
            {"v1": [0.3], "v2": [0.8]},
        ],
    )
    html = HTMLReportGenerator.generate_experiment_html(result, None)
    assert "Win Rates" in html
    # v1 wins 2/3
    assert ">v1<" in html
    assert "2/3" in html
    # v2 wins 1/3
    assert "1/3" in html
    # Per-Task table
    assert "Per-Task Comparison" in html
    # Each task row should appear
    assert ">t1<" in html
    assert ">t2<" in html
    assert ">t3<" in html


def test_experiment_html_most_divergent_tasks_omitted_when_zero_spread():
    """When all task spreads are 0, the Most Divergent section is omitted."""
    result = _result_with_summaries(
        ["v1", "v2"],
        [
            {"v1": [0.7], "v2": [0.7]},
            {"v1": [0.5], "v2": [0.5]},
        ],
    )
    html = HTMLReportGenerator.generate_experiment_html(result, None)
    assert "Most Divergent Tasks" not in html


def test_experiment_html_prompt_configuration_when_mutations_present():
    """Prompt Configuration section renders when a variant has mutations."""
    result = _experiment_result(["v1", "v2"])
    experiment = ExperimentDefinition(
        experiment_id="exp-1",
        description="d",
        variants=[
            ExperimentVariant(variant_id="v1"),
            ExperimentVariant(variant_id="v2", prompt_mutations=[PromptPrefix(content="hi")]),
        ],
    )
    html = HTMLReportGenerator.generate_experiment_html(result, experiment)
    assert "Prompt Configuration" in html
    assert "(1 mutations: prefix)" in html


def test_safe_write_returns_path_on_success(tmp_path: Path):
    target = tmp_path / "x.html"
    got = safe_write(lambda: "<html></html>", target, label="x")
    assert got == target
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "<html></html>"


def test_safe_write_returns_none_and_logs_on_failure(tmp_path: Path, caplog):
    def _bad() -> str:
        raise RuntimeError("boom")

    target = tmp_path / "nope.html"
    with caplog.at_level("ERROR"):
        got = safe_write(_bad, target, label="nope")

    assert got is None
    assert not target.exists()
    assert any("Failed to generate nope" in rec.message for rec in caplog.records)


def test_write_variant_html_uses_safe_write(tmp_path: Path):
    agg = _variant_agg()
    target = tmp_path / "sub" / "variant.html"
    got = write_variant_html("v1", agg, [], target)
    assert got == target
    assert target.exists()


def test_write_experiment_html_uses_safe_write(tmp_path: Path):
    result = _experiment_result(["v1"])
    target = tmp_path / "sub" / "experiment.html"
    got = write_experiment_html(result, None, None, target)
    assert got == target
    assert target.exists()


def test_write_task_html_returns_none_on_render_failure(tmp_path: Path, monkeypatch):
    """When the renderer blows up, write_task_html returns None rather than raising —
    so the orchestrator-side emission path cannot mask the run outcome."""
    from coder_eval import reports_html

    def _boom(result):
        raise RuntimeError("render failed")

    monkeypatch.setattr(reports_html.HTMLReportGenerator, "generate_task_html", staticmethod(_boom))

    result = _make_result()
    target = tmp_path / "task.html"
    assert write_task_html(result, target) is None
    assert not target.exists()


def test_task_html_renders_token_usage():
    result = _make_result()
    result.total_token_usage = TokenUsage(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=300,
        total_cost_usd=0.0123,
    )
    html = HTMLReportGenerator.generate_task_html(result)
    assert "Token Usage" in html
    assert "1,000" in html
    assert "500" in html
    assert "$0.0123" in html


def test_task_html_omits_token_usage_when_absent():
    result = _make_result()
    # Token usage is None by default
    html = HTMLReportGenerator.generate_task_html(result)
    assert "<h2>Token Usage</h2>" not in html


def test_task_html_renders_agent_settings_from_sdk_options():
    result = _make_result()
    long_prompt = "This is a long system prompt " * 50  # way over 200 chars
    result.sdk_options = {
        "permission_mode": "auto",
        "allowed_tools": ["Bash", "Read"],
        "model": "claude-sonnet-4-6",
        "max_turns": 30,
        "system_prompt": long_prompt,
    }
    html = HTMLReportGenerator.generate_task_html(result)
    assert "Agent Settings" in html
    assert "auto" in html
    assert "Bash, Read" in html
    assert "claude-sonnet-4-6" in html
    assert "Max Turns" in html
    assert ">30<" in html
    # system_prompt is truncated at 200 chars with '...' suffix
    assert "..." in html
    assert long_prompt not in html


def test_task_html_renders_agent_settings_from_agent_config_fallback():
    result = _make_result()
    result.agent_config = AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits", model="some-model")
    # No sdk_options — should fall back to agent_config
    html = HTMLReportGenerator.generate_task_html(result)
    assert "Agent Settings" in html
    assert "acceptEdits" in html
    assert "some-model" in html


def test_task_html_renders_environment_and_installed_tools():
    result = _make_result()
    result.environment_info = {
        "framework_version": "0.1.0",
        "api_routing": "proxy",
        "installed_tools": {"npm/@anthropic-ai/claude-code": "1.2.3"},
    }
    html = HTMLReportGenerator.generate_task_html(result)
    assert "<h2>Environment</h2>" in html
    assert "framework_version" in html
    assert "api_routing" in html
    assert "<h2>Installed Tools</h2>" in html
    assert "npm/@anthropic-ai/claude-code" in html
    assert "1.2.3" in html
    # installed_tools should NOT ALSO appear under Environment (it's excluded there)
    env_section = html[html.index("<h2>Environment</h2>") :]
    env_section = env_section[: env_section.find("<h2>")] if "<h2>" in env_section[20:] else env_section
    assert "installed_tools" not in env_section


def test_task_html_renders_commands_efficiency():
    result = _make_result()
    result.expected_commands = 10
    result.actual_commands = 12
    result.commands_efficiency = 0.833
    html = HTMLReportGenerator.generate_task_html(result)
    assert "Commands Efficiency" in html
    assert "83.3%" in html
    assert "10/12" in html


def test_task_html_omits_commands_efficiency_when_absent():
    result = _make_result()
    html = HTMLReportGenerator.generate_task_html(result)
    assert "<h2>Commands Efficiency</h2>" not in html


def test_task_html_renders_cost_badge_in_header():
    result = _make_result()
    result.total_token_usage = TokenUsage(input_tokens=100, output_tokens=50, total_cost_usd=0.5)
    html = HTMLReportGenerator.generate_task_html(result)
    # Header region ends at the </div> after ".header-bar"
    header = html[: html.index("<h2>") if "<h2>" in html else len(html)]
    assert "$0.5000" in header


def test_task_html_omits_cost_badge_when_cost_is_none():
    """Cost badge is only rendered when total_cost_usd is populated."""
    result = _make_result()
    result.total_token_usage = TokenUsage(input_tokens=100, output_tokens=50, total_cost_usd=None)
    html = HTMLReportGenerator.generate_task_html(result)
    header = html[: html.index("<h2>")]
    assert "$" not in header


def test_task_html_token_usage_cost_na_when_absent():
    """When tokens are recorded but cost is None, Token Usage cell renders N/A."""
    result = _make_result()
    result.total_token_usage = TokenUsage(input_tokens=100, output_tokens=50, total_cost_usd=None)
    html = HTMLReportGenerator.generate_task_html(result)
    token_section = html[html.index("<h2>Token Usage</h2>") :]
    # Next heading demarcates this section
    next_h2 = token_section.find("<h2>", 20)
    if next_h2 >= 0:
        token_section = token_section[:next_h2]
    assert "N/A" in token_section


def test_command_stats_skill_callout_and_pattern():
    result = _make_result()
    result.command_stats = CommandStatistics(
        total_commands=6,
        successful_commands=6,
        failed_commands=0,
        commands_by_tool={"Skill": 4, "Bash": 2},
        total_command_time_ms=600.0,
        avg_command_time_ms=100.0,
        slowest_commands=[],
        most_common_sequence="Bash → Read",
    )
    html = HTMLReportGenerator.generate_task_html(result)
    assert "Most Common Pattern" in html
    assert "Bash → Read" in html
    assert "Skill Tool Invoked" in html
    assert "4 time(s)" in html


def test_command_stats_slowest_shows_parameter_preview():
    result = _make_result()
    result.command_stats = CommandStatistics(
        total_commands=1,
        successful_commands=1,
        failed_commands=0,
        commands_by_tool={"Bash": 1},
        total_command_time_ms=800.0,
        avg_command_time_ms=800.0,
        slowest_commands=[SlowestCommandInfo(tool="Bash", duration_ms=800.0, parameters={"command": "ls -la /tmp"})],
    )
    html = HTMLReportGenerator.generate_task_html(result)
    # Slowest table now includes a Parameters column
    assert "ls -la /tmp" in html


def test_render_criteria_uses_per_criterion_pass_threshold():
    """Pass counter uses each CriterionResult's pass_threshold, not a fixed 1.0."""
    result = _make_result(
        criteria=[
            CriterionResult(
                criterion_type="pytest",
                description="Some tests pass",
                score=0.6,
                pass_threshold=0.5,  # passes: 0.6 >= 0.5
            ),
            CriterionResult(
                criterion_type="pytest",
                description="Strict threshold",
                score=0.6,
                pass_threshold=0.7,  # fails: 0.6 < 0.7
            ),
        ],
    )
    html = HTMLReportGenerator.generate_task_html(result)
    assert "(1/2 passed)" in html


@pytest.mark.parametrize(
    "scenario, turns, expectations",
    [
        (
            "clean",
            [
                TurnRecord(
                    iteration=1,
                    user_input="Create Main.cs",
                    agent_output="done",
                    commands=[_make_command("Write", 0)],
                )
            ],
            {
                "present": ["<h3>Iteration 1</h3>"],
                "absent": ["Attempt", "recovered after", "terminal failure"],
            },
        ),
        (
            "recovered",
            [
                TurnRecord(
                    iteration=1,
                    user_input="Do the thing",
                    agent_output="<partial>",
                    commands=[_make_command("Skill", 0, parameters={"skill": "x"})],
                    crashed=True,
                ),
                TurnRecord(
                    iteration=1,
                    user_input="Do the thing",
                    agent_output="all done",
                    commands=[_make_command("Write", 1)],
                ),
            ],
            {
                "present": [
                    "Iteration 1 — recovered after 1 crashed attempt",
                    ">Attempt 1 of 2<",
                    ">Attempt 2 of 2<",
                    "crashed (partial)",
                    "recovered</span>",
                    'class="iteration-group iteration-group--recovered"',
                ],
                "absent": ["iteration-group iteration-group--terminal", "terminal failure"],
            },
        ),
        (
            "terminal",
            [
                TurnRecord(
                    iteration=1,
                    user_input="Do the thing",
                    agent_output="<partial>",
                    commands=[_make_command("Bash", 0, parameters={"command": "x"})],
                    crashed=True,
                ),
                TurnRecord(
                    iteration=1,
                    user_input="Do the thing",
                    agent_output="<partial>",
                    commands=[_make_command("Bash", 1, parameters={"command": "y"})],
                    crashed=True,
                ),
            ],
            {
                "present": [
                    "Iteration 1 — terminal failure (2 crashed attempts)",
                    ">Attempt 1 of 2<",
                    ">Attempt 2 of 2<",
                    'class="iteration-group iteration-group--terminal"',
                ],
                "absent": [
                    "iteration-group iteration-group--recovered",
                    "recovered after",
                    "recovered</span>",
                ],
            },
        ),
    ],
)
def test_iteration_group_rendering(scenario, turns, expectations):
    """Single-attempt iterations keep the plain heading; multi-attempt iterations
    render a coloured banner (recovered vs terminal) wrapping per-attempt cards."""
    final_status = FinalStatus.ERROR if scenario == "terminal" else FinalStatus.SUCCESS
    result = _make_result(final_status=final_status, iterations=turns)
    html = HTMLReportGenerator.generate_task_html(result)
    for s in expectations["present"]:
        assert s in html, f"[{scenario}] expected {s!r} in html"
    for s in expectations["absent"]:
        assert s not in html, f"[{scenario}] expected {s!r} NOT in html"


def test_attempt_transition_marker_and_trace_count():
    """The transition marker between attempts uses crash_reason (with a generic
    fallback), the wording is "resuming" not "retrying", and the trailing
    transition is suppressed on terminal failure. The Conversation Trace
    heading counts iterations (and adds attempts when partials are present)."""
    # Recovered with a stamped reason: marker carries the reason and "resuming".
    res_reason = _make_result(
        iterations=[
            TurnRecord(
                iteration=1,
                user_input="p",
                agent_output="<partial>",
                crashed=True,
                crash_reason="CLI process failed (exit code 137)",
            ),
            TurnRecord(iteration=1, user_input="p", agent_output="ok"),
        ],
    )
    html_reason = HTMLReportGenerator.generate_task_html(res_reason)
    assert "CLI process failed (exit code 137)" in html_reason
    assert "resuming" in html_reason
    assert "retrying" not in html_reason
    assert "Conversation Trace (1 iteration, 2 attempts)" in html_reason

    # Backward-compat fallback: partial without crash_reason → "Agent crashed".
    res_fallback = _make_result(
        iterations=[
            TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True),
            TurnRecord(iteration=1, user_input="p", agent_output="ok"),
        ],
    )
    html_fallback = HTMLReportGenerator.generate_task_html(res_fallback)
    assert "Agent crashed" in html_fallback
    assert "resuming" in html_fallback

    # Terminal failure: divider belongs between attempts, so the last crash
    # gets no trailing marker (and exactly one "resuming" appears overall).
    res_terminal = _make_result(
        final_status=FinalStatus.ERROR,
        iterations=[
            TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True, crash_reason="boom 1"),
            TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True, crash_reason="boom 2"),
        ],
    )
    html_terminal = HTMLReportGenerator.generate_task_html(res_terminal)
    assert "boom 1" in html_terminal
    assert "boom 2" not in html_terminal
    assert html_terminal.count("resuming") == 1

    # Clean run: trace count uses iterations only (no "attempts" segment).
    res_clean = _make_result(
        iterations=[
            TurnRecord(iteration=1, user_input="p", agent_output="ok"),
            TurnRecord(iteration=2, user_input="p", agent_output="ok"),
        ],
    )
    html_clean = HTMLReportGenerator.generate_task_html(res_clean)
    assert "Conversation Trace (2 iterations)" in html_clean
    assert "attempts" not in html_clean.split("Conversation Trace")[1].split("</h2>")[0]


def test_generation_metrics_breaks_down_crashed_partials():
    """The "Crashed Partials" stat must split the count into recovered vs terminal
    so a reader can tell at a glance whether the agent recovered or aborted."""
    result = _make_result(
        iterations=[
            # Iteration 1: 1 crash + recovery.
            TurnRecord(iteration=1, user_input="p", agent_output="<partial>", crashed=True),
            TurnRecord(iteration=1, user_input="p", agent_output="ok"),
            # Iteration 2: 2 crashes, no recovery.
            TurnRecord(iteration=2, user_input="p", agent_output="<partial>", crashed=True),
            TurnRecord(iteration=2, user_input="p", agent_output="<partial>", crashed=True),
        ],
    )
    html = HTMLReportGenerator.generate_task_html(result)
    assert "Crashed Partials" in html
    assert "3 (1 recovered, 2 terminal)" in html
