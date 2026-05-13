"""Tests for run_limits 4-layer config resolution with field-merge semantics."""

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
        assert lineage["run_limits.max_usd"].source == "default"

    def test_resolve_max_turns_from_default(self):
        default_exp = _default_exp(RunLimits(max_turns=20))
        task = _make_task()
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.max_turns == 20
        assert lineage["run_limits.max_turns"].source == "default"

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
        assert lineage["run_limits.max_usd"].source == "experiment-defaults"

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
        assert lineage["run_limits.max_usd"].source == "task"

    def test_task_run_limits_overrides_default_per_key(self):
        """Task setting one key keeps the default for other keys (field merge)."""
        default_exp = _default_exp(RunLimits(max_turns=20, task_timeout=600))
        task = _make_task(run_limits={"max_turns": 5})
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        resolved, _, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.max_turns == 5
        assert resolved.run_limits.task_timeout == 600

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
        assert lineage["run_limits.max_usd"].source == "variant"

    def test_variant_run_limits_field_merges_with_task(self):
        """Variant override of one key leaves other task-set keys intact (field-merge tell-tale)."""
        default_exp = _default_exp()
        task = _make_task(run_limits={"max_turns": 5, "max_usd": 0.5})
        exp = ExperimentDefinition(
            experiment_id="e",
            variants=[ExperimentVariant(variant_id="v", run_limits=RunLimits(max_usd=1.0))],
        )
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        # Variant only overrides max_usd; task's max_turns survives.
        assert resolved.run_limits.max_turns == 5
        assert resolved.run_limits.max_usd == 1.0
        assert lineage["run_limits.max_turns"].source == "task"
        assert lineage["run_limits.max_usd"].source == "variant"

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

    def test_legacy_agent_max_turns_field_merges(self):
        """Legacy variant.agent.max_turns hoists into run_limits.max_turns and field-merges."""
        default_exp = _default_exp()
        task = _make_task()
        with pytest.warns(DeprecationWarning):
            exp = ExperimentDefinition(
                experiment_id="e",
                variants=[
                    ExperimentVariant(
                        variant_id="v",
                        agent={"type": "claude-code", "max_turns": 7},
                        run_limits=RunLimits(max_usd=0.5),
                    )
                ],
            )
            resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.max_turns == 7
        assert resolved.run_limits.max_usd == 0.5
        assert lineage["run_limits.max_turns"].source == "variant-agent-deprecated"

    def test_no_run_limits_anywhere(self):
        default_exp = _default_exp()
        task = _make_task()
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is None
        # No lineage entries under run_limits.* either.
        assert not any(k.startswith("run_limits") for k in lineage)

    def test_lineage_uses_dotted_keys(self):
        """Lineage keys are dotted (run_limits.max_turns), not the bare 'max_turns'."""
        default_exp = _default_exp()
        task = _make_task(run_limits={"max_turns": 5})
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        _, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert "run_limits.max_turns" in lineage
        assert "max_turns" not in lineage

    def test_lineage_value_is_serializable(self):
        """lineage.value must be JSON-serializable (scalar from dotted key)."""
        import json

        default_exp = _default_exp()
        task = _make_task(run_limits={"max_usd": 5.0})
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        _, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        json.dumps(lineage["run_limits.max_usd"].value)

    def test_unset_non_optional_field_does_not_clobber_lower_layer(self):
        """A variant patch must not wipe ``count_cached_input`` set by the task.

        Regression guard: ``RunLimits.count_cached_input`` is ``bool = False``
        (not Optional). ``model_dump(exclude_none=True)`` would always include
        the default ``False`` and overwrite a ``True`` set in a lower-precedence
        layer. The resolver uses ``exclude_unset=True`` precisely so a layer
        that didn't mention the field leaves earlier values intact.
        """
        default_exp = _default_exp()
        task = _make_task(run_limits={"count_cached_input": True, "max_turns": 10})
        # Variant overrides ONLY max_usd — must not touch count_cached_input.
        exp = ExperimentDefinition(
            experiment_id="e",
            variants=[ExperimentVariant(variant_id="v", run_limits=RunLimits(max_usd=1.0))],
        )
        resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.count_cached_input is True, (
            "variant's default count_cached_input=False clobbered the task-level True"
        )
        assert resolved.run_limits.max_turns == 10
        assert resolved.run_limits.max_usd == 1.0
        # Lineage should only credit fields the variant actually set.
        assert "run_limits.count_cached_input" not in lineage or (
            lineage["run_limits.count_cached_input"].source == "task"
        )

    def test_hoist_shim_preserves_unset_marker_on_runlimits_instance(self):
        """Programmatic ``TaskDefinition(run_limits=RunLimits())`` must not leak defaults.

        Regression guard: the hoist shim normalizes a ``RunLimits`` instance back
        into a dict so the three legacy-shape jobs can append into it. If that
        dump uses ``exclude_none=True`` instead of ``exclude_unset=True``, the
        default ``count_cached_input=False`` is written into the dict, then
        Pydantic re-validates and marks it as explicit on the rebuilt model.
        ``_merge_rl(task.run_limits, "task")`` then clobbers a True set in
        the default-experiment layer.
        """
        default_exp = _default_exp(RunLimits(count_cached_input=True))
        # Task constructed with an empty RunLimits instance — no user-set fields.
        task = _make_task(run_limits=RunLimits())
        exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="v")])
        resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, exp, exp.variants[0])
        assert resolved.run_limits is not None
        assert resolved.run_limits.count_cached_input is True, (
            "empty RunLimits() at the task layer clobbered default-experiment count_cached_input=True"
        )


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
        assert "incl." not in md
        assert "**Failed**: 1\n" in md


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
