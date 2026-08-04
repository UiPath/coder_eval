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
from coder_eval.reports_experiment import ExperimentReportGenerator
from coder_eval.reports_stats import describe_prompt_config
from tests._fixtures.report_snapshots import assert_matches_snapshot


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
        assert "Pass Rate" in content

    def test_variant_html_task_links_include_replicate_segment(self, tmp_path, sample_result):
        """variant.html task links must include the /00/ replicate segment so they
        resolve to task.html under the new `<task_id>/<NN>/` layout."""
        run_dir = tmp_path / "runs" / "test"
        ExperimentReportGenerator.write_reports(sample_result, run_dir)

        html = (run_dir / "sonnet" / "variant.html").read_text()
        # Every per-task link in the variant HTML must traverse the replicate dir.
        assert 'href="task-a/00/task.html"' in html
        assert 'href="task-b/00/task.html"' in html
        # Guard against the pre-fix flat shape slipping back in.
        assert 'href="task-a/task.html"' not in html
        assert 'href="task-b/task.html"' not in html


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
        assert "| Avg Duration (s) |" in md
        assert "| Tokens |" in md
        assert "| Tasks Run |" in md
        assert "| Pass Rate |" in md

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
        from coder_eval.reports_stats import welch_t_test

        p = welch_t_test([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert p is not None
        assert p == 1.0

    def test_welch_t_test_different_groups(self):
        """Very different groups should produce low p-value."""
        from coder_eval.reports_stats import welch_t_test

        p = welch_t_test([1.0, 1.1, 0.9, 1.0, 1.05], [5.0, 5.1, 4.9, 5.0, 5.05])
        assert p is not None
        assert p < 0.001

    def test_welch_t_test_insufficient_data(self):
        """Single observation per group should return None."""
        from coder_eval.reports_stats import welch_t_test

        assert welch_t_test([1.0], [2.0]) is None

    def test_welch_t_test_exact_reference_value(self):
        """Exact Student-t p-value, cross-checked against scipy ttest_ind(equal_var=False)."""
        from coder_eval.reports_stats import welch_t_test

        # Equal variances 2.5, n=5 each ⇒ t=1.0, Welch-Satterthwaite df=8.
        p = welch_t_test([1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 3.0, 4.0, 5.0, 6.0])
        assert p is not None
        assert abs(p - 0.34659) < 5e-4

    def test_welch_t_test_zero_variance_different_means(self):
        """Zero variance in both groups: deterministic difference ⇒ 0.0, identical ⇒ 1.0."""
        from coder_eval.reports_stats import welch_t_test

        assert welch_t_test([1.0, 1.0], [2.0, 2.0]) == 0.0
        assert welch_t_test([1.0, 1.0], [1.0, 1.0]) == 1.0

    def test_student_t_two_tailed_p_small_df_not_normal(self):
        """Small df must use t tails, not normal ones — the regression sensor for the old bug."""
        from coder_eval.reports_stats import student_t_two_tailed_p

        p = student_t_two_tailed_p(2.5, 4.0)
        assert abs(p - 0.06677) < 5e-4
        # The old normal approximation reported 0.0124 here — falsely significant.
        assert p > 0.05

    def test_student_t_two_tailed_p_critical_values(self):
        """Round-trip the standard t-table critical values."""
        from coder_eval.reports_stats import student_t_two_tailed_p

        assert abs(student_t_two_tailed_p(2.776, 4.0) - 0.05) < 1e-3
        assert abs(student_t_two_tailed_p(2.228, 10.0) - 0.05) < 1e-3
        assert abs(student_t_two_tailed_p(0.0, 7.0) - 1.0) < 1e-12
        # Degenerate df is not reachable through the t-tests, but the helper stays total.
        assert student_t_two_tailed_p(1.0, 0.0) == 1.0

    def test_student_t_two_tailed_p_large_df_matches_normal(self):
        """At huge df the t distribution converges to the normal."""
        import statistics

        from coder_eval.reports_stats import student_t_two_tailed_p

        normal_p = 2.0 * statistics.NormalDist().cdf(-1.96)
        assert abs(student_t_two_tailed_p(1.96, 1e6) - normal_p) < 1e-5

    def test_regularized_incomplete_beta_closed_forms(self):
        """I_x(a,b) against the closed forms it has for b=1, a=1, and the symmetric point."""
        from coder_eval.reports_stats import regularized_incomplete_beta

        assert abs(regularized_incomplete_beta(2.0, 1.0, 0.3) - 0.3**2) < 1e-10
        assert abs(regularized_incomplete_beta(1.0, 3.0, 0.4) - (1.0 - 0.6**3)) < 1e-10
        assert abs(regularized_incomplete_beta(5.0, 5.0, 0.5) - 0.5) < 1e-10
        assert regularized_incomplete_beta(2.0, 3.0, 0.0) == 0.0
        assert regularized_incomplete_beta(2.0, 3.0, 1.0) == 1.0

    def test_paired_t_test_exact_reference_value(self):
        """Exact paired-t p-value, cross-checked against scipy ttest_rel."""
        from coder_eval.reports_stats import paired_t_test, welch_t_test

        a = [0.9, 0.5, 0.8, 0.7, 0.95]
        b = [0.7, 0.55, 0.6, 0.72, 0.8]
        p_paired = paired_t_test(a, b)
        assert p_paired is not None
        assert abs(p_paired - 0.15273) < 5e-4
        # On positively correlated pairs like these, pairing is the sharper test —
        # but that is a property of this data, not a guarantee (it loses df).
        p_welch = welch_t_test(a, b)
        assert p_welch is not None
        assert p_paired < p_welch

    def test_paired_t_test_constant_shift_and_identical(self):
        """Zero-sd differences: deterministic shift ⇒ 0.0, identical lists ⇒ 1.0."""
        from coder_eval.reports_stats import paired_t_test

        assert paired_t_test([1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 3.0, 4.0, 5.0, 6.0]) == 0.0
        assert paired_t_test([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0

    def test_paired_t_test_invalid_input(self):
        """Length mismatch or fewer than 2 pairs returns None."""
        from coder_eval.reports_stats import paired_t_test

        assert paired_t_test([1.0, 2.0], [1.0]) is None
        assert paired_t_test([1.0], [2.0]) is None

    def test_non_finite_inputs_never_report_significance(self):
        """NaN/inf must not produce a fabricated p-value — fail closed, never significant."""
        from coder_eval.reports_stats import fmt_p, paired_t_test, student_t_two_tailed_p, welch_t_test

        nan, inf = float("nan"), float("inf")
        assert student_t_two_tailed_p(nan, 4.0) == 1.0
        assert student_t_two_tailed_p(2.5, nan) == 1.0
        assert student_t_two_tailed_p(inf, 4.0) == 1.0
        # The list-taking helpers can say "no result", which renders as an em dash.
        assert welch_t_test([1.0, nan], [2.0, 3.0]) is None
        assert paired_t_test([1.0, nan], [2.0, 3.0]) is None
        assert fmt_p(welch_t_test([1.0, inf], [2.0, 3.0])) == "—"

    def test_mean_and_stddev(self):
        """Basic mean and stddev calculations."""
        from coder_eval.reports_stats import mean, stddev

        assert mean([1.0, 2.0, 3.0]) == 2.0
        assert abs(stddev([1.0, 2.0, 3.0]) - 1.0) < 1e-10
        assert stddev([5.0]) == 0.0  # Single value

    def test_fmt_mean_sd(self):
        """Format mean ± stddev string."""
        from coder_eval.reports_stats import fmt_mean_sd

        result = fmt_mean_sd([1.0, 2.0, 3.0])
        assert "2.000" in result
        assert "±" in result
        assert fmt_mean_sd([]) == "N/A"


class TestComprehensiveVariantReport:
    """Tests for the enriched variant report (matching run-report.md)."""

    def test_variant_report_with_run_dir(self, tmp_path):
        """variant report should include rich sections when run_dir has task.json files."""
        from datetime import datetime

        from coder_eval.models import AgentKind, CriterionResult, EvaluationResult, TurnRecord

        # Set up run_dir with task.json files under <variant>/<task>/<NN>/
        variant_dir = tmp_path / "my-variant"
        task_dir = variant_dir / "task-a" / "00"
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
            iterations=[
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
    """Tests for describe_prompt_config helper."""

    def test_no_mutations(self):
        variant = ExperimentVariant(variant_id="baseline")
        assert describe_prompt_config(variant) == "(base prompt)"

    def test_with_override(self):
        variant = ExperimentVariant(variant_id="override", initial_prompt="custom")
        assert describe_prompt_config(variant) == "(prompt override)"

    def test_with_mutations(self):
        variant = ExperimentVariant(
            variant_id="mutated",
            prompt_mutations=[PromptPrefix(content="pre"), PromptSuffix(content="suf")],
        )
        result = describe_prompt_config(variant)
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


class TestReplicateStatistics:
    """Tests for the '## Replicate Statistics' section in experiment reports."""

    def _make_result(
        self,
        *,
        replicate_count: int = 1,
        per_replicate_scores: dict | None = None,
        per_replicate_voided: dict | None = None,
        variant_ids: list[str] | None = None,
    ):
        vids = variant_ids or ["a", "b"]
        task_summaries = [
            TaskExperimentSummary(
                task_id="task-1",
                variant_results=[
                    VariantResult(
                        variant_id=vid,
                        task_id="task-1",
                        weighted_score=0.8,
                        final_status="SUCCESS",
                        duration_seconds=10.0,
                        replicate_count=replicate_count,
                    )
                    for vid in vids
                ],
                best_variant=vids[0],
                score_spread=0.0,
                replicate_count=replicate_count,
            ),
        ]
        aggs = {
            vid: VariantAggregate(
                variant_id=vid,
                tasks_run=1,
                tasks_succeeded=1,
                tasks_failed=0,
                tasks_error=0,
                average_score=0.8,
                average_duration=10.0,
                replicate_count=replicate_count,
            )
            for vid in vids
        }
        return ExperimentResult(
            experiment_id="rep-test",
            description="Replicate test",
            variant_ids=vids,
            task_summaries=task_summaries,
            variant_aggregates=aggs,
            total_duration_seconds=20.0,
            per_replicate_scores=per_replicate_scores or {},
            per_replicate_voided=per_replicate_voided or {},
        )

    def test_no_section_when_replicate_count_is_one(self):
        result = self._make_result(replicate_count=1)
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "## Replicate Statistics" not in md

    def test_section_renders_when_replicate_count_gt_one(self):
        per_rep = {
            "a": {"task-1": [0.7, 0.8, 0.9]},
            "b": {"task-1": [0.6, 0.75, 0.85]},
        }
        result = self._make_result(replicate_count=3, per_replicate_scores=per_rep)
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "## Replicate Statistics" in md
        assert "| Variant |" in md
        assert "| a |" in md
        assert "| b |" in md

    def test_section_contains_per_variant_ci_columns(self):
        per_rep = {
            "a": {"task-1": [0.7, 0.8, 0.9]},
            "b": {"task-1": [0.6, 0.75, 0.85]},
        }
        result = self._make_result(replicate_count=3, per_replicate_scores=per_rep)
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "95% CI" in md
        assert "Pass-rate" in md

    def test_voided_replicates_are_excluded_from_the_pass_rate(self):
        """The gate leaves a voided row's score intact, so a score-based pass rate
        counts it as a pass unless the void flag is honored."""
        per_rep = {"a": {"task-1": [1.0, 1.0, 1.0]}, "b": {"task-1": [1.0, 1.0, 1.0]}}
        voided = {"a": {"task-1": [True, True, True]}, "b": {"task-1": [False, False, False]}}
        result = self._make_result(replicate_count=3, per_replicate_scores=per_rep, per_replicate_voided=voided)

        md = ExperimentReportGenerator.generate_experiment_report(result)

        assert "| a | 3 | 1.000 | [1.000, 1.000] | 0/0 [0.00, 0.00] | 3 |" in md
        assert "| b | 3 | 1.000 | [1.000, 1.000] | 3/3 [0.44, 1.00] | 0 |" in md

    def test_a_partly_voided_variant_keeps_its_honest_replicates(self):
        per_rep = {"a": {"task-1": [1.0, 1.0, 0.2]}, "b": {"task-1": [1.0, 1.0, 1.0]}}
        voided = {"a": {"task-1": [True, False, False]}}
        result = self._make_result(replicate_count=3, per_replicate_scores=per_rep, per_replicate_voided=voided)

        md = ExperimentReportGenerator.generate_experiment_report(result)

        assert "| a | 3 | 0.733" in md  # the mean still reports every score
        assert "| 1/2 [" in md  # the pass rate counts only the two that measured the agent
        assert md.count("| Voided |") == 1

    def test_a_result_without_void_flags_reports_none_voided(self):
        """A run.json written before the flag existed must still render."""
        per_rep = {"a": {"task-1": [1.0, 1.0, 1.0]}, "b": {"task-1": [0.5, 0.5, 0.5]}}
        result = self._make_result(replicate_count=3, per_replicate_scores=per_rep)

        md = ExperimentReportGenerator.generate_experiment_report(result)

        assert "| a | 3 | 1.000 | [1.000, 1.000] | 3/3 [0.44, 1.00] | 0 |" in md

    def test_paired_diff_line_for_two_variants_equal_counts(self):
        # Two tasks: the paired section pairs their per-task means (n = 2 tasks).
        per_rep = {
            "a": {"task-1": [0.9, 0.85, 0.95], "task-2": [0.7, 0.75, 0.8]},
            "b": {"task-1": [0.6, 0.65, 0.7], "task-2": [0.5, 0.55, 0.6]},
        }
        result = self._make_result(replicate_count=3, per_replicate_scores=per_rep)
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "Paired mean diff" in md
        assert "Cohen's d" in md

    def test_paired_diff_needs_two_common_tasks(self):
        # Unequal replicate counts no longer exclude a task, but one task is still
        # a single pair — too few for a paired comparison.
        per_rep = {
            "a": {"task-1": [0.9, 0.85, 0.95]},
            "b": {"task-1": [0.6, 0.65, 0.7, 0.75, 0.8]},
        }
        result = self._make_result(replicate_count=3, per_replicate_scores=per_rep)
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "needs at least 2 tasks" in md
        assert "Paired mean diff" not in md

    def test_no_paired_diff_for_single_variant(self):
        per_rep = {"only": {"task-1": [0.7, 0.8, 0.9]}}
        result = self._make_result(
            replicate_count=3,
            per_replicate_scores=per_rep,
            variant_ids=["only"],
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "## Replicate Statistics" in md
        assert "Paired mean diff" not in md

    def test_variant_report_shows_ci_when_replicate_count_gt_one(self):
        per_rep = {
            "a": {"task-1": [0.7, 0.8, 0.9]},
        }
        result = self._make_result(
            replicate_count=3,
            per_replicate_scores=per_rep,
            variant_ids=["a"],
        )
        md = ExperimentReportGenerator.generate_variant_report("a", result)
        assert "Score 95% CI" in md
        assert "Replicates/task" in md

    def test_variant_report_no_ci_when_replicate_count_is_one(self):
        result = self._make_result(replicate_count=1, variant_ids=["a"])
        md = ExperimentReportGenerator.generate_variant_report("a", result)
        assert "Score 95% CI" not in md


class TestCollectVariantSeries:
    """The one series collector both reporters share."""

    @staticmethod
    def _result_with_replicates() -> ExperimentResult:
        """One task per variant, 3 replicates, 90s summed ⇒ 30s per run."""
        return ExperimentResult(
            experiment_id="exp",
            description="d",
            variant_ids=["a"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="t1",
                    variant_results=[
                        VariantResult(
                            variant_id="a",
                            task_id="t1",
                            weighted_score=0.8,
                            final_status="SUCCESS",
                            duration_seconds=90.0,
                            replicate_count=3,
                        )
                    ],
                    best_variant="a",
                    score_spread=0.0,
                )
            ],
            variant_aggregates={
                "a": VariantAggregate(
                    variant_id="a",
                    tasks_run=1,
                    tasks_succeeded=1,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.8,
                    average_duration=30.0,
                    replicate_count=3,
                )
            },
            total_duration_seconds=90.0,
        )

    def test_duration_is_per_run_not_replicate_inflated(self):
        """duration_seconds is summed across replicates, so it must be divided out."""
        from coder_eval.reports_stats import collect_variant_series

        series = collect_variant_series(self._result_with_replicates())
        assert series["a"].durations == [30.0]

    def test_html_and_markdown_report_the_same_duration(self):
        """Regression: the HTML copy of this collector used the raw summed duration."""
        from coder_eval.reports_html import _experiment_aggregate_metrics

        result = self._result_with_replicates()
        md = ExperimentReportGenerator.generate_experiment_report(result)
        html = _experiment_aggregate_metrics(result)
        assert "| Avg Duration (s) | 30.0 |" in md
        assert "<td>30.0</td>" in html
        assert "90.0" not in html

    def test_result_for_unknown_variant_is_ignored(self):
        """A task result naming a variant outside variant_ids must not raise."""
        from coder_eval.reports_stats import collect_variant_series

        result = self._result_with_replicates()
        result.task_summaries[0].variant_results.append(
            VariantResult(
                variant_id="ghost",
                task_id="t1",
                weighted_score=0.1,
                final_status="SUCCESS",
                duration_seconds=1.0,
            )
        )
        series = collect_variant_series(result)
        assert set(series) == {"a"}


class TestPairedComparisonSection:
    """Tests for the ``## Paired Comparison`` markdown section."""

    @staticmethod
    def _make_result(
        variant_ids: list[str],
        per_replicate_scores: dict[str, dict[str, list[float]]],
    ) -> ExperimentResult:
        task_ids = sorted({tid for per_task in per_replicate_scores.values() for tid in per_task})
        return ExperimentResult(
            experiment_id="exp",
            description="d",
            variant_ids=variant_ids,
            task_summaries=[
                TaskExperimentSummary(
                    task_id=task_id,
                    variant_results=[
                        VariantResult(
                            variant_id=vid,
                            task_id=task_id,
                            weighted_score=next(iter(per_replicate_scores.get(vid, {}).get(task_id, [])), 0.0),
                            final_status="SUCCESS",
                            duration_seconds=1.0,
                        )
                        for vid in variant_ids
                    ],
                    best_variant=variant_ids[0],
                    score_spread=0.1,
                )
                for task_id in task_ids
            ],
            variant_aggregates={
                vid: VariantAggregate(
                    variant_id=vid,
                    tasks_run=len(task_ids),
                    tasks_succeeded=len(task_ids),
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=0.5,
                    average_duration=1.0,
                )
                for vid in variant_ids
            },
            total_duration_seconds=10.0,
            per_replicate_scores=per_replicate_scores,
        )

    def test_paired_comparison_section_two_variants_single_replicate(self):
        """A single-replicate 2-variant experiment gets the paired section (no replicate gate)."""
        result = self._make_result(
            ["a", "b"],
            {
                "a": {"t1": [0.9], "t2": [0.5], "t3": [0.8]},
                "b": {"t1": [0.7], "t2": [0.55], "t3": [0.6]},
            },
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "## Paired Comparison" in md
        assert "**Paired mean diff (a - b)**" in md
        assert ", p = " in md
        assert "Cohen's d" in md
        # Single replicate ⇒ no Replicate Statistics section, but paired still renders.
        assert "## Replicate Statistics" not in md

    def test_paired_comparison_section_absent(self):
        """3 variants, or an empty per_replicate_scores, render no paired section."""
        three = self._make_result(
            ["a", "b", "c"],
            {"a": {"t1": [0.9]}, "b": {"t1": [0.7]}, "c": {"t1": [0.6]}},
        )
        assert "## Paired Comparison" not in ExperimentReportGenerator.generate_experiment_report(three)

        empty = self._make_result(["a", "b"], {})
        empty.task_summaries = [
            TaskExperimentSummary(
                task_id="t1",
                variant_results=[
                    VariantResult(
                        variant_id=vid,
                        task_id="t1",
                        weighted_score=0.5,
                        final_status="SUCCESS",
                        duration_seconds=1.0,
                    )
                    for vid in ("a", "b")
                ],
                best_variant="a",
                score_spread=0.0,
            )
        ]
        assert "## Paired Comparison" not in ExperimentReportGenerator.generate_experiment_report(empty)

    def test_paired_comparison_averages_replicates_per_task(self):
        """Replicates are averaged per task, so n is the task count — not the slot count."""
        result = self._make_result(
            ["a", "b"],
            {
                "a": {"t1": [0.8, 1.0], "t2": [0.4, 0.6]},
                "b": {"t1": [0.5, 0.7], "t2": [0.2, 0.4]},
            },
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "per-task mean score of 2 task(s) common to both variants" in md
        # Task means: a = [0.9, 0.5], b = [0.6, 0.3] ⇒ diffs [+0.3, +0.2], mean +0.250.
        assert "**Paired mean diff (a - b)**: +0.250" in md

    def test_paired_comparison_single_common_task(self):
        """One common task ⇒ the section explains why there is no paired result."""
        result = self._make_result(
            ["a", "b"],
            {"a": {"t1": [0.9, 0.85, 0.95]}, "b": {"t1": [0.6, 0.65, 0.7]}},
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "## Paired Comparison" in md
        assert "*A paired comparison needs at least 2 tasks common to a and b; found 1.*" in md
        assert "Paired mean diff" not in md

    def test_paired_comparison_keeps_tasks_with_unequal_replicate_counts(self):
        """Pairing per-task means needs no equal replicate counts — no task is dropped."""
        result = self._make_result(
            ["a", "b"],
            {
                "a": {"t1": [0.5, 0.7], "t2": [0.4], "t3": [0.8]},
                "b": {"t1": [0.5], "t2": [0.45], "t3": [0.7]},
            },
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "per-task mean score of 3 task(s) common to both variants" in md
        assert "excluded" not in md
        # Task means: a = [0.6, 0.4, 0.8], b = [0.5, 0.45, 0.7] ⇒ diffs [+0.1, -0.05, +0.1].
        assert "**Paired mean diff (a - b)**: +0.050" in md

    def test_html_renders_the_same_paired_numbers_as_markdown(self):
        """Both reporters render one shared computation, so they cannot disagree."""
        from coder_eval.reports_html import _experiment_paired_comparison

        result = self._make_result(
            ["a", "b"],
            {"a": {"t1": [0.9], "t2": [0.5], "t3": [0.8]}, "b": {"t1": [0.7], "t2": [0.55], "t3": [0.6]}},
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        html = _experiment_paired_comparison(result)
        assert "<h2>Paired Comparison</h2>" in html
        # Cohen's apostrophe is HTML-escaped, so match the numbers either side of it.
        for fragment in ("Paired mean diff (a - b): +0.117", "d = 0.81", "p = 0.296"):
            assert fragment in html
        assert "**Paired mean diff (a - b)**: +0.117 [95% CI -0.242, +0.475], Cohen's d = 0.81, p = 0.296" in md

    def test_html_paired_section_absent_for_three_variants(self):
        from coder_eval.reports_html import _experiment_paired_comparison

        result = self._make_result(
            ["a", "b", "c"],
            {"a": {"t1": [0.9]}, "b": {"t1": [0.7]}, "c": {"t1": [0.6]}},
        )
        assert _experiment_paired_comparison(result) == ""

    def test_paired_ci_agrees_with_p_value(self):
        """The interval and the p-value share a distribution, so they agree about 0."""
        result = self._make_result(
            ["a", "b"],
            {
                "a": {"t1": [0.9], "t2": [0.85], "t3": [0.95], "t4": [0.9]},
                "b": {"t1": [0.2], "t2": [0.25], "t3": [0.15], "t4": [0.3]},
            },
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        # A large, consistent gap ⇒ p well below 0.05 and a CI clear of zero.
        assert "p = <0.001" in md
        assert "[95% CI +0." in md

    def test_paired_comparison_ignores_tasks_with_no_scores(self):
        """A task with an empty replicate list on either side is not a pair, and the
        exclusion is surfaced in the rendered note."""
        result = self._make_result(
            ["a", "b"],
            {"a": {"t1": [0.9], "t2": [0.5], "t3": []}, "b": {"t1": [0.7], "t2": [0.6], "t3": [0.4]}},
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert "per-task mean score of 2 task(s) common to both variants" in md
        assert "(1 task(s) excluded — not scored by both variants)" in md


class TestExperimentReportSnapshots:
    """Byte-identical characterization snapshots for generate_experiment_report — the
    safety net for its decomposition. Output is deterministic: bootstrap_mean_ci in
    reports_stats uses a fixed default seed, so no scrubbing is needed (do not pass a
    varying seed); the paired section is closed-form Student-t with no randomness."""

    def test_experiment_report_snapshot_2variant(self):
        """2 variants → p-value column shown; plus prompt config, budget sub-rows,
        Assistant Turns + Tokens rows, Win Rates, Per-Task, Most Divergent."""
        experiment = ExperimentDefinition(
            experiment_id="model-comparison",
            variants=[
                ExperimentVariant(variant_id="baseline"),
                ExperimentVariant(
                    variant_id="mutated",
                    prompt_mutations=[PromptPrefix(content="Think step by step.")],
                ),
            ],
        )
        result = ExperimentResult(
            experiment_id="model-comparison",
            description="Compare baseline vs mutated",
            variant_ids=["baseline", "mutated"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-a",
                    variant_results=[
                        VariantResult(
                            variant_id="baseline",
                            task_id="task-a",
                            weighted_score=0.9,
                            final_status="SUCCESS",
                            duration_seconds=30.0,
                            total_tokens=1000,
                            total_assistant_turns=5,
                        ),
                        VariantResult(
                            variant_id="mutated",
                            task_id="task-a",
                            weighted_score=0.7,
                            final_status="FAILURE",
                            duration_seconds=60.0,
                            total_tokens=2000,
                            total_assistant_turns=8,
                        ),
                    ],
                    best_variant="baseline",
                    score_spread=0.2,
                ),
                TaskExperimentSummary(
                    task_id="task-b",
                    variant_results=[
                        VariantResult(
                            variant_id="baseline",
                            task_id="task-b",
                            weighted_score=0.5,
                            final_status="FAILURE",
                            duration_seconds=40.0,
                            total_tokens=1200,
                            total_assistant_turns=6,
                        ),
                        VariantResult(
                            variant_id="mutated",
                            task_id="task-b",
                            weighted_score=0.95,
                            final_status="SUCCESS",
                            duration_seconds=50.0,
                            total_tokens=1800,
                            total_assistant_turns=7,
                        ),
                    ],
                    best_variant="mutated",
                    score_spread=0.45,
                ),
            ],
            variant_aggregates={
                "baseline": VariantAggregate(
                    variant_id="baseline",
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.7,
                    average_duration=35.0,
                    total_tokens=2200,
                    tasks_token_budget_exceeded=1,
                ),
                "mutated": VariantAggregate(
                    variant_id="mutated",
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=0.825,
                    average_duration=55.0,
                    total_tokens=3800,
                    tasks_cost_budget_exceeded=1,
                ),
            },
            total_duration_seconds=180.0,
            per_replicate_scores={
                "baseline": {"task-a": [0.9], "task-b": [0.5]},
                "mutated": {"task-a": [0.7], "task-b": [0.95]},
            },
        )
        md = ExperimentReportGenerator.generate_experiment_report(result, experiment=experiment)
        assert_matches_snapshot(md, "experiment_2variant.md")

    def test_experiment_report_snapshot_3variant(self):
        """3 variants → p-value column hidden; no prompt config; Win Rates over 3
        variants, Per-Task with 3 columns, Most Divergent."""
        result = ExperimentResult(
            experiment_id="three-way",
            description="Three-way comparison",
            variant_ids=["a", "b", "c"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-1",
                    variant_results=[
                        VariantResult(
                            variant_id=vid,
                            task_id="task-1",
                            weighted_score=score,
                            final_status="SUCCESS" if score >= 0.9 else "FAILURE",
                            duration_seconds=dur,
                            total_tokens=tok,
                            total_assistant_turns=turns,
                        )
                        for vid, score, dur, tok, turns in [
                            ("a", 0.9, 20.0, 900, 4),
                            ("b", 0.6, 25.0, 1100, 5),
                            ("c", 0.8, 30.0, 1300, 6),
                        ]
                    ],
                    best_variant="a",
                    score_spread=0.3,
                ),
                TaskExperimentSummary(
                    task_id="task-2",
                    variant_results=[
                        VariantResult(
                            variant_id=vid,
                            task_id="task-2",
                            weighted_score=score,
                            final_status="SUCCESS" if score >= 0.9 else "FAILURE",
                            duration_seconds=dur,
                            total_tokens=tok,
                            total_assistant_turns=turns,
                        )
                        for vid, score, dur, tok, turns in [
                            ("a", 0.4, 22.0, 950, 3),
                            ("b", 0.95, 28.0, 1150, 7),
                            ("c", 0.5, 33.0, 1350, 5),
                        ]
                    ],
                    best_variant="b",
                    score_spread=0.55,
                ),
            ],
            variant_aggregates={
                vid: VariantAggregate(
                    variant_id=vid,
                    tasks_run=2,
                    tasks_succeeded=1,
                    tasks_failed=1,
                    tasks_error=0,
                    average_score=avg,
                    average_duration=dur,
                    total_tokens=tok,
                )
                for vid, avg, dur, tok in [
                    ("a", 0.65, 21.0, 1850),
                    ("b", 0.775, 26.5, 2250),
                    ("c", 0.65, 31.5, 2650),
                ]
            },
            total_duration_seconds=240.0,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert_matches_snapshot(md, "experiment_3variant.md")

    def test_experiment_report_snapshot_replicates(self):
        """A variant ran >1 replicate → Replicate Statistics section present, with
        the bootstrap CI / Wilson table, the Replicates/task aggregate row, the
        Per-Task Reps column, and the 2-variant paired-bootstrap diff line."""
        per_rep = {
            "a": {"task-1": [0.9, 0.85, 0.95]},
            "b": {"task-1": [0.6, 0.65, 0.7]},
        }
        result = ExperimentResult(
            experiment_id="rep-test",
            description="Replicate comparison",
            variant_ids=["a", "b"],
            task_summaries=[
                TaskExperimentSummary(
                    task_id="task-1",
                    variant_results=[
                        VariantResult(
                            variant_id=vid,
                            task_id="task-1",
                            weighted_score=score,
                            final_status="SUCCESS",
                            duration_seconds=10.0,
                            total_tokens=1000,
                            replicate_count=3,
                        )
                        for vid, score in [("a", 0.9), ("b", 0.65)]
                    ],
                    best_variant="a",
                    score_spread=0.25,
                    replicate_count=3,
                ),
            ],
            variant_aggregates={
                vid: VariantAggregate(
                    variant_id=vid,
                    tasks_run=1,
                    tasks_succeeded=1,
                    tasks_failed=0,
                    tasks_error=0,
                    average_score=score,
                    average_duration=10.0,
                    total_tokens=1000,
                    replicate_count=3,
                )
                for vid, score in [("a", 0.9), ("b", 0.65)]
            },
            total_duration_seconds=60.0,
            per_replicate_scores=per_rep,
        )
        md = ExperimentReportGenerator.generate_experiment_report(result)
        assert_matches_snapshot(md, "experiment_replicates.md")
