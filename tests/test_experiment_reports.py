"""Tests for experiment result models and report generation."""

import json

import pytest

from coder_eval.models import (
    ExperimentDefinition,
    ExperimentResult,
    ExperimentVariant,
    PromptPrefix,
    PromptSuffix,
    TaskExperimentSummary,
    VariantAggregate,
    VariantResult,
)
from coder_eval.reports_experiment import ExperimentReportGenerator, _describe_prompt_config


class TestResultModels:
    def test_variant_result(self):
        r = VariantResult(
            variant_id="sonnet",
            task_id="task-a",
            weighted_score=0.85,
            final_status="SUCCESS",
            duration_seconds=42.0,
            total_tokens=1500,
        )
        assert r.variant_id == "sonnet"
        assert r.weighted_score == 0.85

    def test_task_experiment_summary(self):
        s = TaskExperimentSummary(
            task_id="task-a",
            variant_results=[
                VariantResult(
                    variant_id="sonnet",
                    task_id="task-a",
                    weighted_score=0.9,
                    final_status="SUCCESS",
                    duration_seconds=30.0,
                ),
                VariantResult(
                    variant_id="opus",
                    task_id="task-a",
                    weighted_score=0.7,
                    final_status="FAILURE",
                    duration_seconds=60.0,
                ),
            ],
            best_variant="sonnet",
            score_spread=0.2,
        )
        assert s.best_variant == "sonnet"
        assert len(s.variant_results) == 2

    def test_experiment_result(self):
        r = ExperimentResult(
            experiment_id="test",
            description="Test experiment",
            variant_ids=["sonnet", "opus"],
            task_summaries=[],
            variant_aggregates={
                "sonnet": VariantAggregate(
                    variant_id="sonnet",
                    tasks_run=5,
                    tasks_succeeded=4,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.85,
                    average_duration=30.0,
                ),
                "opus": VariantAggregate(
                    variant_id="opus",
                    tasks_run=5,
                    tasks_succeeded=3,
                    tasks_failed=2,
                    tasks_error=0,
                    average_score=0.7,
                    average_duration=45.0,
                ),
            },
            total_duration_seconds=120.0,
        )
        assert r.variant_aggregates["sonnet"].tasks_succeeded == 4


class TestExperimentReportGenerator:
    @pytest.fixture
    def sample_result(self):
        return ExperimentResult(
            experiment_id="model-comparison",
            description="Compare Sonnet vs Opus",
            variant_ids=["sonnet", "opus"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-a",
                    variant_results=[
                        VariantResult(
                            variant_id="sonnet",
                            task_id="task-a",
                            weighted_score=0.9,
                            final_status="SUCCESS",
                            duration_seconds=30.0,
                            total_tokens=1000,
                        ),
                        VariantResult(
                            variant_id="opus",
                            task_id="task-a",
                            weighted_score=0.7,
                            final_status="FAILURE",
                            duration_seconds=60.0,
                            total_tokens=2000,
                        ),
                    ],
                    best_variant="sonnet",
                    score_spread=0.2,
                ),
                TaskExperimentSummary(
                    task_id="task-b",
                    variant_results=[
                        VariantResult(
                            variant_id="sonnet",
                            task_id="task-b",
                            weighted_score=0.5,
                            final_status="FAILURE",
                            duration_seconds=40.0,
                            total_tokens=1200,
                        ),
                        VariantResult(
                            variant_id="opus",
                            task_id="task-b",
                            weighted_score=0.95,
                            final_status="SUCCESS",
                            duration_seconds=50.0,
                            total_tokens=1800,
                        ),
                    ],
                    best_variant="opus",
                    score_spread=0.45,
                ),
            ],
            variant_aggregates={
                "sonnet": VariantAggregate(
                    variant_id="sonnet",
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.7,
                    average_duration=35.0,
                    total_tokens=2200,
                ),
                "opus": VariantAggregate(
                    variant_id="opus",
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.825,
                    average_duration=55.0,
                    total_tokens=3800,
                ),
            },
            total_duration_seconds=180.0,
        )

    def test_generate_task_report_md(self, sample_result):
        """variant report should contain cross-variant comparison table."""
        md = ExperimentReportGenerator.generate_task_report(sample_result.task_summaries[0])
        assert "sonnet" in md
        assert "opus" in md
        assert "0.9" in md  # sonnet score

    def test_generate_experiment_report_md(self, sample_result):
        """experiment report should contain aggregate stats."""
        md = ExperimentReportGenerator.generate_experiment_report(sample_result)
        assert "model-comparison" in md
        assert "sonnet" in md
        assert "opus" in md

    def test_experiment_report_shows_errors_row(self):
        """experiment report should show Errors row with non-zero counts."""
        result = ExperimentResult(
            experiment_id="error-test",
            description="Error test",
            variant_ids=["v"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="t1",
                    variant_results=[
                        VariantResult(
                            variant_id="v",
                            task_id="t1",
                            weighted_score=0.9,
                            final_status="SUCCESS",
                            duration_seconds=10.0,
                        ),
                    ],
                    best_variant="v",
                    score_spread=0.0,
                ),
                TaskExperimentSummary(
                    task_id="t2",
                    variant_results=[
                        VariantResult(
                            variant_id="v",
                            task_id="t2",
                            weighted_score=0.0,
                            final_status="ERROR",
                            duration_seconds=5.0,
                        ),
                    ],
                    best_variant="v",
                    score_spread=0.0,
                ),
            ],
            variant_aggregates={
                "v": VariantAggregate(
                    variant_id="v",
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=0,
                    tasks_error=1,
                    average_score=0.45,
                    average_duration=7.5,
                ),
            },
            total_duration_seconds=15.0,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "| Errors" in md
        assert "| 1 |" in md

    def test_experiment_report_variant_summary_shows_errors(self):
        """variant summary should display error count."""
        result = ExperimentResult(
            experiment_id="error-variant-test",
            description="Error variant test",
            variant_ids=["v"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="t1",
                    variant_results=[
                        VariantResult(
                            variant_id="v",
                            task_id="t1",
                            weighted_score=0.0,
                            final_status="ERROR",
                            duration_seconds=5.0,
                        ),
                    ],
                    best_variant="v",
                    score_spread=0.0,
                ),
            ],
            variant_aggregates={
                "v": VariantAggregate(
                    variant_id="v",
                    tasks_run=1,
                    tasks_succeeded=0,
                    tasks_failed=0,
                    tasks_error=1,
                    average_score=0.0,
                    average_duration=5.0,
                ),
            },
            total_duration_seconds=5.0,
        )
        md = ExperimentReportGenerator.generate_variant_report("v", result)
        assert "**Errors**: 1" in md

    def test_task_detail_table_shows_timeout_icon(self):
        """TIMEOUT and MAX_TURNS_EXHAUSTED should get distinct icons, not '?'."""
        result = ExperimentResult(
            experiment_id="icon-test",
            description="Icon test",
            variant_ids=["v"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="t-timeout",
                    variant_results=[
                        VariantResult(
                            variant_id="v",
                            task_id="t-timeout",
                            weighted_score=0.0,
                            final_status="TIMEOUT",
                            duration_seconds=60.0,
                        ),
                    ],
                    best_variant="v",
                    score_spread=0.0,
                ),
                TaskExperimentSummary(
                    task_id="t-exhausted",
                    variant_results=[
                        VariantResult(
                            variant_id="v",
                            task_id="t-exhausted",
                            weighted_score=0.0,
                            final_status="MAX_TURNS_EXHAUSTED",
                            duration_seconds=30.0,
                        ),
                    ],
                    best_variant="v",
                    score_spread=0.0,
                ),
            ],
            variant_aggregates={
                "v": VariantAggregate(
                    variant_id="v",
                    tasks_run=2,
                    tasks_succeeded=0,
                    tasks_failed=2,
                    tasks_error=0,
                    average_score=0.0,
                    average_duration=45.0,
                ),
            },
            total_duration_seconds=90.0,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        # TIMEOUT and MAX_TURNS_EXHAUSTED should NOT show "?" — they should have real icons
        assert "(?)" not in md

    def test_generate_task_summary_json(self, sample_result):
        """task summary json should be serializable."""
        summary = sample_result.task_summaries[0]
        json_str = summary.model_dump_json(indent=2)
        assert "sonnet" in json_str
        assert "task-a" in json_str

    def test_generate_experiment_summary_json(self, sample_result):
        """experiment summary json should be serializable."""
        json_str = sample_result.model_dump_json(indent=2)
        assert "model-comparison" in json_str

    def test_experiment_report_shows_ties(self):
        """experiment report should count ties separately from wins."""
        result = ExperimentResult(
            experiment_id="tie-test",
            description="Tie test",
            variant_ids=["a", "b"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-tied",
                    variant_results=[
                        VariantResult(
                            variant_id="a",
                            task_id="task-tied",
                            weighted_score=0.8,
                            final_status="SUCCESS",
                            duration_seconds=10.0,
                        ),
                        VariantResult(
                            variant_id="b",
                            task_id="task-tied",
                            weighted_score=0.8,
                            final_status="SUCCESS",
                            duration_seconds=10.0,
                        ),
                    ],
                    best_variant="b",
                    is_tie=True,
                    score_spread=0.0,
                ),
                TaskExperimentSummary(
                    task_id="task-clear",
                    variant_results=[
                        VariantResult(
                            variant_id="a",
                            task_id="task-clear",
                            weighted_score=0.9,
                            final_status="SUCCESS",
                            duration_seconds=10.0,
                        ),
                        VariantResult(
                            variant_id="b",
                            task_id="task-clear",
                            weighted_score=0.5,
                            final_status="FAILURE",
                            duration_seconds=10.0,
                        ),
                    ],
                    best_variant="a",
                    is_tie=False,
                    score_spread=0.4,
                ),
            ],
            variant_aggregates={
                "a": VariantAggregate(
                    variant_id="a",
                    tasks_run=2,
                    tasks_succeeded=2,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.85,
                    average_duration=10.0,
                ),
                "b": VariantAggregate(
                    variant_id="b",
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.65,
                    average_duration=10.0,
                ),
            },
            total_duration_seconds=20.0,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        # variant a wins 1 clear task, variant b wins 0, 1 tie
        assert "**a**: 1/2" in md
        assert "**b**: 0/2" in md
        assert "**Ties**: 1/2" in md


class TestReportFileWriting:
    @pytest.fixture
    def sample_result(self):
        return ExperimentResult(
            experiment_id="model-comparison",
            description="Compare Sonnet vs Opus",
            variant_ids=["sonnet", "opus"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-a",
                    variant_results=[
                        VariantResult(
                            variant_id="sonnet",
                            task_id="task-a",
                            weighted_score=0.9,
                            final_status="SUCCESS",
                            duration_seconds=30.0,
                            total_tokens=1000,
                        ),
                        VariantResult(
                            variant_id="opus",
                            task_id="task-a",
                            weighted_score=0.7,
                            final_status="FAILURE",
                            duration_seconds=60.0,
                            total_tokens=2000,
                        ),
                    ],
                    best_variant="sonnet",
                    score_spread=0.2,
                ),
                TaskExperimentSummary(
                    task_id="task-b",
                    variant_results=[
                        VariantResult(
                            variant_id="sonnet",
                            task_id="task-b",
                            weighted_score=0.5,
                            final_status="FAILURE",
                            duration_seconds=40.0,
                            total_tokens=1200,
                        ),
                        VariantResult(
                            variant_id="opus",
                            task_id="task-b",
                            weighted_score=0.95,
                            final_status="SUCCESS",
                            duration_seconds=50.0,
                            total_tokens=1800,
                        ),
                    ],
                    best_variant="opus",
                    score_spread=0.45,
                ),
            ],
            variant_aggregates={
                "sonnet": VariantAggregate(
                    variant_id="sonnet",
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.7,
                    average_duration=35.0,
                    total_tokens=2200,
                ),
                "opus": VariantAggregate(
                    variant_id="opus",
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.825,
                    average_duration=55.0,
                    total_tokens=3800,
                ),
            },
            total_duration_seconds=180.0,
        )

    def test_write_variant_reports(self, tmp_path, sample_result):
        """Should write variant.md and variant.json per variant."""
        run_dir = tmp_path / "runs" / "test"
        ExperimentReportGenerator.write_reports(sample_result, run_dir)

        # Check variant-level files
        assert (run_dir / "sonnet" / "variant.md").exists()
        assert (run_dir / "sonnet" / "variant.json").exists()
        assert (run_dir / "opus" / "variant.md").exists()
        assert (run_dir / "opus" / "variant.json").exists()

        # Check experiment-level files
        assert (run_dir / "experiment.md").exists()
        assert (run_dir / "experiment.json").exists()

        # Verify JSON is valid
        summary = json.loads((run_dir / "experiment.json").read_text())
        assert summary["experiment_id"] == "model-comparison"

    def test_variant_report_content(self, tmp_path, sample_result):
        """variant report should contain variant comparison table."""
        run_dir = tmp_path / "runs" / "test"
        ExperimentReportGenerator.write_reports(sample_result, run_dir)

        content = (run_dir / "sonnet" / "variant.md").read_text()
        assert "sonnet" in content
        assert "Task Details" in content
        # New: variant report should include experiment context and summary
        assert "**Experiment**: model-comparison" in content
        assert "## Summary" in content
        assert "Success Rate" in content


class TestAggregateMetrics:
    """Tests for the new Aggregate Metrics section in experiment reports."""

    def test_experiment_report_has_aggregate_metrics(self):
        """experiment report should contain vertical Aggregate Metrics table."""
        result = ExperimentResult(
            experiment_id="test",
            description="Test",
            variant_ids=["a", "b"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-1",
                    variant_results=[
                        VariantResult(
                            variant_id="a",
                            task_id="task-1",
                            weighted_score=0.9,
                            final_status="SUCCESS",
                            duration_seconds=30.0,
                            total_tokens=1000,
                            iteration_count=2,
                        ),
                        VariantResult(
                            variant_id="b",
                            task_id="task-1",
                            weighted_score=0.7,
                            final_status="FAILURE",
                            duration_seconds=50.0,
                            total_tokens=1500,
                            iteration_count=3,
                        ),
                    ],
                    best_variant="a",
                    score_spread=0.2,
                ),
                TaskExperimentSummary(
                    task_id="task-2",
                    variant_results=[
                        VariantResult(
                            variant_id="a",
                            task_id="task-2",
                            weighted_score=0.8,
                            final_status="SUCCESS",
                            duration_seconds=40.0,
                            total_tokens=1200,
                            iteration_count=1,
                        ),
                        VariantResult(
                            variant_id="b",
                            task_id="task-2",
                            weighted_score=0.6,
                            final_status="FAILURE",
                            duration_seconds=60.0,
                            total_tokens=1800,
                            iteration_count=4,
                        ),
                    ],
                    best_variant="a",
                    score_spread=0.2,
                ),
            ],
            variant_aggregates={
                "a": VariantAggregate(
                    variant_id="a",
                    tasks_run=2,
                    tasks_succeeded=2,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.85,
                    average_duration=35.0,
                    total_tokens=2200,
                ),
                "b": VariantAggregate(
                    variant_id="b",
                    tasks_run=2,
                    tasks_succeeded=0,
                    tasks_failed=2,
                    tasks_error=0,
                    average_score=0.65,
                    average_duration=55.0,
                    total_tokens=3300,
                ),
            },
            total_duration_seconds=100.0,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)

        # Should have Aggregate Metrics section
        assert "## Aggregate Metrics" in md

        # Metrics should appear as rows (vertical layout)
        assert "| Score |" in md
        assert "| Duration (s) |" in md
        assert "| Iterations |" in md
        assert "| Tokens |" in md
        assert "| Tasks Run |" in md
        assert "| Success Rate |" in md

        # Two-variant comparison should show p-value column
        assert "p-value" in md

        # Should contain ± for stddev
        assert "±" in md

    def test_experiment_report_no_p_values_with_three_variants(self):
        """p-value column should not appear with >2 variants."""
        result = ExperimentResult(
            experiment_id="multi",
            description="Multi-variant",
            variant_ids=["a", "b", "c"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-1",
                    variant_results=[
                        VariantResult(
                            variant_id="a",
                            task_id="task-1",
                            weighted_score=0.9,
                            final_status="SUCCESS",
                            duration_seconds=30.0,
                        ),
                        VariantResult(
                            variant_id="b",
                            task_id="task-1",
                            weighted_score=0.8,
                            final_status="SUCCESS",
                            duration_seconds=40.0,
                        ),
                        VariantResult(
                            variant_id="c",
                            task_id="task-1",
                            weighted_score=0.7,
                            final_status="FAILURE",
                            duration_seconds=50.0,
                        ),
                    ],
                    best_variant="a",
                    score_spread=0.2,
                ),
            ],
            variant_aggregates={
                "a": VariantAggregate(
                    variant_id="a",
                    tasks_run=1,
                    tasks_succeeded=1,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.9,
                    average_duration=30.0,
                ),
                "b": VariantAggregate(
                    variant_id="b",
                    tasks_run=1,
                    tasks_succeeded=1,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.8,
                    average_duration=40.0,
                ),
                "c": VariantAggregate(
                    variant_id="c",
                    tasks_run=1,
                    tasks_succeeded=0,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.7,
                    average_duration=50.0,
                ),
            },
            total_duration_seconds=120.0,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)

        # Should have Aggregate Metrics section but no p-value column
        assert "## Aggregate Metrics" in md
        assert "p-value" not in md


class TestStatisticalHelpers:
    """Tests for the statistical helper functions."""

    def test_welch_t_test_identical_groups(self):
        """Identical groups should produce p-value of 1.0."""
        from coder_eval.reports_experiment import _welch_t_test

        p = _welch_t_test([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert p is not None
        assert p == 1.0

    def test_welch_t_test_different_groups(self):
        """Very different groups should produce low p-value."""
        from coder_eval.reports_experiment import _welch_t_test

        p = _welch_t_test([1.0, 1.1, 0.9, 1.0, 1.05], [5.0, 5.1, 4.9, 5.0, 5.05])
        assert p is not None
        assert p < 0.001

    def test_welch_t_test_insufficient_data(self):
        """Single observation per group should return None."""
        from coder_eval.reports_experiment import _welch_t_test

        assert _welch_t_test([1.0], [2.0]) is None

    def test_mean_and_stddev(self):
        """Basic mean and stddev calculations."""
        from coder_eval.reports_experiment import _mean, _stddev

        assert _mean([1.0, 2.0, 3.0]) == 2.0
        assert abs(_stddev([1.0, 2.0, 3.0]) - 1.0) < 1e-10
        assert _stddev([5.0]) == 0.0  # Single value

    def test_fmt_mean_sd(self):
        """Format mean ± stddev string."""
        from coder_eval.reports_experiment import _fmt_mean_sd

        result = _fmt_mean_sd([1.0, 2.0, 3.0])
        assert "2.000" in result
        assert "±" in result
        assert _fmt_mean_sd([]) == "N/A"


class TestComprehensiveVariantReport:
    """Tests for the enriched variant report (matching run-report.md)."""

    def test_variant_report_with_run_dir(self, tmp_path):
        """variant report should include rich sections when run_dir has task.json files."""
        from datetime import datetime

        from coder_eval.models import AgentKind, CriterionResult, EvaluationResult, TurnRecord

        # Set up run_dir with task.json files
        variant_dir = tmp_path / "my-variant"
        task_dir = variant_dir / "task-a"
        task_dir.mkdir(parents=True)

        eval_result = EvaluationResult(
            task_id="task-a",
            task_description="Test task A",
            variant_id="my-variant",
            agent_type=AgentKind.CLAUDE_CODE,
            model_used="claude-sonnet-4-5-20250514",
            started_at=datetime(2025, 1, 1, 12, 0, 0),
            completed_at=datetime(2025, 1, 1, 12, 1, 0),
            duration_seconds=60.0,
            final_status="SUCCESS",
            weighted_score=0.9,
            iteration_count=2,
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="check", score=1.0),
            ],
            turns=[
                TurnRecord(iteration=1, user_input="do it", agent_output="done", duration_seconds=30.0),
                TurnRecord(iteration=2, user_input="fix it", agent_output="fixed", duration_seconds=30.0),
            ],
            environment_info={"coder_eval": "0.1.0", "python": "3.13.3"},
            sdk_options={
                "permission_mode": "bypassPermissions",
                "allowed_tools": ["Read", "Write"],
                "model": "claude-sonnet-4-5-20250514",
            },
        )
        (task_dir / "task.json").write_text(eval_result.model_dump_json(indent=2))

        result = ExperimentResult(
            experiment_id="test",
            description="Test experiment",
            variant_ids=["my-variant"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-a",
                    variant_results=[
                        VariantResult(
                            variant_id="my-variant",
                            task_id="task-a",
                            weighted_score=0.9,
                            final_status="SUCCESS",
                            duration_seconds=60.0,
                            iteration_count=2,
                        ),
                    ],
                    best_variant="my-variant",
                    score_spread=0.0,
                ),
            ],
            variant_aggregates={
                "my-variant": VariantAggregate(
                    variant_id="my-variant",
                    tasks_run=1,
                    tasks_succeeded=1,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.9,
                    average_duration=60.0,
                ),
            },
            total_duration_seconds=60.0,
        )

        md = ExperimentReportGenerator.generate_variant_report("my-variant", result, run_dir=tmp_path)

        # Should have rich sections from EvaluationResult data
        assert "## Summary" in md
        assert "## Task Details" in md
        assert "## Generation Metrics" in md
        assert "## Agent Settings" in md
        assert "**Permission Mode**: bypassPermissions" in md
        assert "## Environment" in md
        assert "coder_eval**: 0.1.0" in md

    def test_variant_report_without_run_dir(self):
        """variant report without run_dir should still work (basic mode)."""
        result = ExperimentResult(
            experiment_id="test",
            description="Test",
            variant_ids=["v"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="t",
                    variant_results=[
                        VariantResult(
                            variant_id="v",
                            task_id="t",
                            weighted_score=0.5,
                            final_status="FAILURE",
                            duration_seconds=10.0,
                        ),
                    ],
                    best_variant="v",
                    score_spread=0.0,
                ),
            ],
            variant_aggregates={
                "v": VariantAggregate(
                    variant_id="v",
                    tasks_run=1,
                    tasks_succeeded=0,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.5,
                    average_duration=10.0,
                ),
            },
            total_duration_seconds=10.0,
        )

        md = ExperimentReportGenerator.generate_variant_report("v", result)

        assert "## Summary" in md
        assert "## Task Details" in md
        # Should NOT have rich sections without run_dir
        assert "## Generation Metrics" not in md
        assert "## Agent Settings" not in md


class TestDescribePromptConfig:
    """Tests for _describe_prompt_config helper."""

    def test_no_mutations(self):
        variant = ExperimentVariant(variant_id="baseline")
        assert _describe_prompt_config(variant) == "(base prompt)"

    def test_with_override(self):
        variant = ExperimentVariant(variant_id="override", initial_prompt="custom")
        assert _describe_prompt_config(variant) == "(prompt override)"

    def test_with_mutations(self):
        variant = ExperimentVariant(
            variant_id="mutated",
            prompt_mutations=[PromptPrefix(content="pre"), PromptSuffix(content="suf")],
        )
        result = _describe_prompt_config(variant)
        assert result == "(2 mutations: prefix, suffix)"

    def test_prompt_config_in_experiment_report(self):
        """When experiment is passed, report includes prompt config section."""
        experiment = ExperimentDefinition(
            experiment_id="prompt-test",
            variants=[
                ExperimentVariant(variant_id="baseline"),
                ExperimentVariant(
                    variant_id="mutated",
                    prompt_mutations=[PromptPrefix(content="Think step by step.")],
                ),
            ],
        )
        result = ExperimentResult(
            experiment_id="prompt-test",
            description="Test prompt mutations",
            variant_ids=["baseline", "mutated"],
            task_summaries=[],
            variant_aggregates={
                "baseline": VariantAggregate(
                    variant_id="baseline",
                    tasks_run=0,
                    tasks_succeeded=0,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.0,
                    average_duration=0.0,
                ),
                "mutated": VariantAggregate(
                    variant_id="mutated",
                    tasks_run=0,
                    tasks_succeeded=0,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.0,
                    average_duration=0.0,
                ),
            },
            total_duration_seconds=0.0,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result, experiment=experiment)
        assert "## Prompt Configuration" in md
        assert "(base prompt)" in md
        assert "(1 mutations: prefix)" in md

    def test_no_prompt_config_without_experiment(self):
        """Without experiment definition, no prompt config section."""
        result = ExperimentResult(
            experiment_id="test",
            description="Test",
            variant_ids=["v1"],
            task_summaries=[],
            variant_aggregates={
                "v1": VariantAggregate(
                    variant_id="v1",
                    tasks_run=0,
                    tasks_succeeded=0,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.0,
                    average_duration=0.0,
                ),
            },
            total_duration_seconds=0.0,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "## Prompt Configuration" not in md
