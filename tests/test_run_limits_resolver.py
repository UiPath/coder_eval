"""Tests for run_limits 4-layer config resolution (no CLI layer)."""

from __future__ import annotations

import pytest

from coder_eval.models import (
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    RunLimits,
    TaskDefinition,
)
from coder_eval.orchestration.experiment import resolve_task_for_variant


def _make_task(**kwargs) -> TaskDefinition:
    defaults = {
        "task_id": "t",
        "description": "t",
        "initial_prompt": "do",
        "agent": {"type": "claude-code"},
        "sandbox": {"driver": "tempdir"},
        "success_criteria": [{"type": "file_exists", "path": "x", "description": "x"}],
    }
    defaults.update(kwargs)
    return TaskDefinition(**defaults)


def _default_exp(run_limits: RunLimits | None = None) -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code"}, run_limits=run_limits),
        variants=[ExperimentVariant(variant_id="default")],
    )


class TestRunLimitsResolver:
    def test_default_experiment_provides_run_limits(self):
        default_exp = _default_exp(RunLimits(max_usd=1.0))
        task = _make_task()
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.max_usd == 1.0
        assert lineage["run_limits"].source == "default"

    def test_experiment_defaults_overrides_default(self):
        default_exp = _default_exp(RunLimits(max_usd=1.0))
        task = _make_task()
        exp = ExperimentDefinition(
            experiment_id="e",
            defaults=ExperimentDefaults(run_limits=RunLimits(max_usd=2.0)),
            variants=[ExperimentVariant(variant_id="v")],
        )
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.max_usd == 2.0
        assert lineage["run_limits"].source == "experiment-defaults"

    def test_task_overrides_experiment_defaults(self):
        default_exp = _default_exp(RunLimits(max_usd=1.0))
        task = _make_task(run_limits={"max_usd": 5.0})
        exp = ExperimentDefinition(
            experiment_id="e",
            defaults=ExperimentDefaults(run_limits=RunLimits(max_usd=2.0)),
            variants=[ExperimentVariant(variant_id="v")],
        )
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.max_usd == 5.0
        assert lineage["run_limits"].source == "task"

    def test_variant_overrides_task(self):
        default_exp = _default_exp()
        task = _make_task(run_limits={"max_usd": 5.0})
        exp = ExperimentDefinition(
            experiment_id="e",
            variants=[ExperimentVariant(variant_id="v", run_limits=RunLimits(max_usd=10.0))],
        )
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.max_usd == 10.0
        assert lineage["run_limits"].source == "variant"

    def test_variant_whole_object_replace(self):
        """Variant run_limits REPLACES task's block in full, not field-merge."""
        default_exp = _default_exp()
        task = _make_task(run_limits={"max_input_tokens": 1000, "max_usd": 5.0})
        exp = ExperimentDefinition(
            experiment_id="e",
            variants=[ExperimentVariant(variant_id="v", run_limits=RunLimits(max_output_tokens=500))],
        )
        resolved, _, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        # task's max_input_tokens and max_usd are gone; only variant's max_output_tokens remains.
        assert resolved.run_limits.max_input_tokens is None
        assert resolved.run_limits.max_usd is None
        assert resolved.run_limits.max_output_tokens == 500

    def test_variant_unset_does_not_clear_task_block(self):
        """When variant.run_limits is None (default), task's block is preserved."""
        default_exp = _default_exp()
        task = _make_task(run_limits={"max_usd": 5.0})
        exp = ExperimentDefinition(
            experiment_id="e",
            variants=[ExperimentVariant(variant_id="v")],
        )
        resolved, _, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.max_usd == 5.0

    def test_no_run_limits_anywhere(self):
        default_exp = _default_exp()
        task = _make_task()
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is None
        assert "run_limits" not in lineage

    def test_lineage_value_is_serializable(self):
        """lineage.value must be JSON-serializable (model_dump'd, not the Pydantic obj)."""
        import json

        default_exp = _default_exp()
        task = _make_task(run_limits={"max_usd": 5.0})
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        _, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        # Must be JSON-serializable.
        json.dumps(lineage["run_limits"].value)


class TestSubCounterAggregates:
    """Verify sub-counters are not in the task_count invariant sum."""

    def test_run_summary_invariant_holds_with_subcounters(self):
        from datetime import datetime

        from coder_eval.models import RunSummary

        rs = RunSummary(
            run_id="r",
            start_time=datetime.now(),
            end_time=datetime.now(),
            total_duration_seconds=1.0,
            tasks_run=5,
            tasks_succeeded=2,
            tasks_failed=3,
            tasks_error=0,
            tasks_token_budget_exceeded=2,
            tasks_cost_budget_exceeded=1,
            task_results=[],
            framework_version="0.1.0",
        )
        # Sub-counters are slices of tasks_failed (3), not additional buckets.
        # Invariant: 2 + 3 + 0 == 5 still holds even though sub-counters are 2 + 1.
        assert rs.tasks_token_budget_exceeded + rs.tasks_cost_budget_exceeded <= rs.tasks_failed

    def test_run_summary_subcounters_default_zero(self):
        from datetime import datetime

        from coder_eval.models import RunSummary

        rs = RunSummary(
            run_id="r",
            start_time=datetime.now(),
            end_time=datetime.now(),
            total_duration_seconds=1.0,
            tasks_run=1,
            tasks_succeeded=1,
            tasks_failed=0,
            tasks_error=0,
            task_results=[],
            framework_version="0.1.0",
        )
        assert rs.tasks_token_budget_exceeded == 0
        assert rs.tasks_cost_budget_exceeded == 0

    def test_variant_aggregate_subcounters_default_zero(self):
        from coder_eval.models import VariantAggregate

        agg = VariantAggregate(
            variant_id="v",
            tasks_run=1,
            tasks_succeeded=1,
            tasks_failed=0,
            tasks_error=0,
            average_score=1.0,
            average_duration=0.0,
        )
        assert agg.tasks_token_budget_exceeded == 0
        assert agg.tasks_cost_budget_exceeded == 0


class TestReportRendering:
    """Verify the markdown reports render the breakdown only when non-zero."""

    def test_failed_line_with_breakdown(self):
        from datetime import datetime

        from coder_eval.models import RunSummary
        from coder_eval.reports import ReportGenerator

        rs = RunSummary(
            run_id="r",
            start_time=datetime.now(),
            end_time=datetime.now(),
            total_duration_seconds=1.0,
            tasks_run=5,
            tasks_succeeded=2,
            tasks_failed=3,
            tasks_error=0,
            tasks_token_budget_exceeded=2,
            tasks_cost_budget_exceeded=1,
            task_results=[],
            framework_version="0.1.0",
        )
        md = ReportGenerator.generate_markdown(rs)
        assert "2 token budget" in md
        assert "1 cost budget exceeded" in md

    def test_failed_line_no_breakdown_when_zero(self):
        from datetime import datetime

        from coder_eval.models import RunSummary
        from coder_eval.reports import ReportGenerator

        rs = RunSummary(
            run_id="r",
            start_time=datetime.now(),
            end_time=datetime.now(),
            total_duration_seconds=1.0,
            tasks_run=2,
            tasks_succeeded=1,
            tasks_failed=1,
            tasks_error=0,
            task_results=[],
            framework_version="0.1.0",
        )
        md = ReportGenerator.generate_markdown(rs)
        # No parenthetical breakdown.
        assert "incl." not in md
        assert "**Failed**: 1\n" in md


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
