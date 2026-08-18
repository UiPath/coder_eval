"""Tests for the RunLimits model and its integration with task/experiment models."""

import pytest
from pydantic import ValidationError

from coder_eval.errors import BudgetExceededError
from coder_eval.models import (
    ExperimentDefaults,
    ExperimentVariant,
    FinalStatus,
    RunLimits,
    TaskDefinition,
)
from coder_eval.orchestration.run_limits import INEFFECTIVE_TASK_TIMEOUT_WARNING, validate_run_limits


def _minimal_task(**overrides) -> TaskDefinition:
    defaults = {
        "task_id": "test",
        "description": "test task",
        "initial_prompt": "do something",
        "agent": {"type": "claude-code"},
        "sandbox": {"driver": "tempdir"},
        "success_criteria": [{"type": "file_exists", "path": "x", "description": "x must exist"}],
    }
    defaults.update(overrides)
    return TaskDefinition(**defaults)


class TestRunLimitsValidation:
    def test_empty_block_is_valid(self):
        """Empty run_limits constructs (all-None fields)."""
        rl = RunLimits()
        assert rl.max_turns is None
        assert rl.task_timeout is None
        assert rl.turn_timeout is None
        assert rl.max_input_tokens is None
        assert rl.max_output_tokens is None
        assert rl.max_total_tokens is None
        assert rl.max_usd is None

    def test_only_max_input_tokens_ok(self):
        assert RunLimits(max_input_tokens=1).max_input_tokens == 1

    def test_only_max_output_tokens_ok(self):
        assert RunLimits(max_output_tokens=1).max_output_tokens == 1

    def test_only_max_total_tokens_ok(self):
        assert RunLimits(max_total_tokens=1).max_total_tokens == 1

    def test_only_max_usd_ok(self):
        assert RunLimits(max_usd=0.01).max_usd == 0.01

    def test_only_max_turns_ok(self):
        assert RunLimits(max_turns=20).max_turns == 20

    def test_only_task_timeout_ok(self):
        assert RunLimits(task_timeout=600).task_timeout == 600

    def test_only_turn_timeout_ok(self):
        assert RunLimits(turn_timeout=120).turn_timeout == 120

    def test_max_turns_validation(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            RunLimits(max_turns=0)
        RunLimits(max_turns=1)

    def test_task_timeout_validation(self):
        with pytest.raises(ValidationError, match="greater than or equal to 30"):
            RunLimits(task_timeout=29)
        RunLimits(task_timeout=30)

    def test_turn_timeout_validation(self):
        with pytest.raises(ValidationError, match="greater than or equal to 10"):
            RunLimits(turn_timeout=9)
        RunLimits(turn_timeout=10)

    def test_token_lower_bound(self):
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            RunLimits(max_input_tokens=0)
        # 1 is valid
        RunLimits(max_input_tokens=1)

    def test_cost_lower_bound(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            RunLimits(max_usd=0.0)
        RunLimits(max_usd=0.0001)

    def test_count_cached_input_default_false(self):
        assert RunLimits(max_input_tokens=10).count_cached_input is False

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            RunLimits.model_validate({"max_input_tokens": 10, "unknown_field": 1})

    def test_all_fields_roundtrip(self):
        rl = RunLimits(
            max_turns=20,
            expected_turns=15,
            task_timeout=600,
            turn_timeout=120,
            max_input_tokens=1000,
            max_output_tokens=2000,
            max_total_tokens=3000,
            max_usd=1.5,
            count_cached_input=True,
        )
        dumped = rl.model_dump()
        rebuilt = RunLimits.model_validate(dumped)
        assert rebuilt == rl

    def test_deprecated_expected_turns_still_resolves(self):
        """The retired turn budget is accepted and ignored, not rejected.

        Nothing reads it any more (efficiency is scored in wall-clock seconds
        against a line derived from run history), but thousands of task YAMLs
        still declare it and RunLimits forbids extras — so dropping the field
        outright would fail every one of those tasks at resolution time.
        """
        assert RunLimits().expected_turns is None
        assert RunLimits(expected_turns=1).expected_turns == 1
        assert RunLimits.model_validate({"expected_turns": "10"}).expected_turns == 10
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            RunLimits(expected_turns=0)

    def test_deprecated_expected_turns_is_marked_deprecated(self):
        assert RunLimits.model_fields["expected_turns"].deprecated

    def test_extra_forbid_still_rejects_unknowns(self):
        with pytest.raises(ValidationError):
            RunLimits.model_validate({"expected_turn": 5})


class TestRunLimitsCrossFieldWarnings:
    def test_warning_wording_states_the_single_iteration_semantic(self):
        assert INEFFECTIVE_TASK_TIMEOUT_WARNING == (
            "A larger task_timeout cannot extend the agent's single iteration; the agent budget is turn_timeout."
        )

    @pytest.mark.parametrize(
        ("task_timeout", "turn_timeout", "warns"),
        [
            (121, 120, True),
            (120, 120, False),
            (119, 120, False),
            (None, 120, False),
            (120, None, False),
        ],
    )
    def test_warns_only_when_task_timeout_exceeds_turn_timeout(self, task_timeout, turn_timeout, warns):
        task = _minimal_task(run_limits={"task_timeout": task_timeout, "turn_timeout": turn_timeout})

        messages = validate_run_limits(task)

        assert bool(messages) is warns
        if warns:
            assert INEFFECTIVE_TASK_TIMEOUT_WARNING in messages[0]

    def test_dialog_simulation_still_warns_for_each_agent_call(self):
        task = _minimal_task(
            run_limits={"task_timeout": 121, "turn_timeout": 120},
            simulation={"enabled": True, "persona": "user", "goal": "finish"},
        )

        assert INEFFECTIVE_TASK_TIMEOUT_WARNING in validate_run_limits(task)[0]


class TestRunLimitsOnTaskDefinition:
    def test_default_is_none(self):
        assert _minimal_task().run_limits is None

    def test_run_limits_round_trip(self):
        task = _minimal_task(run_limits={"max_total_tokens": 5000, "max_usd": 0.5})
        assert task.run_limits is not None
        assert task.run_limits.max_total_tokens == 5000
        assert task.run_limits.max_usd == 0.5
        # round-trip
        dumped = task.model_dump()
        rebuilt = TaskDefinition.model_validate(dumped)
        assert rebuilt.run_limits == task.run_limits

    def test_empty_run_limits_on_task_is_valid(self):
        task = _minimal_task(run_limits={})
        assert task.run_limits is not None
        assert task.run_limits.max_turns is None

    def test_top_level_max_turns_now_dropped_with_unknown_field_warning(self):
        """The hoist shim is gone: top-level max_turns is now an unknown top-level
        field — warned and dropped (NOT hoisted into run_limits)."""
        with pytest.warns(DeprecationWarning, match=r"unknown top-level field 'max_turns'"):
            task = _minimal_task(max_turns=20)
        assert task.run_limits is None

    def test_top_level_task_timeout_now_dropped_with_unknown_field_warning(self):
        with pytest.warns(DeprecationWarning, match=r"unknown top-level field 'task_timeout'"):
            task = _minimal_task(task_timeout=600)
        assert task.run_limits is None

    def test_top_level_turn_timeout_now_dropped_with_unknown_field_warning(self):
        with pytest.warns(DeprecationWarning, match=r"unknown top-level field 'turn_timeout'"):
            task = _minimal_task(turn_timeout=120)
        assert task.run_limits is None

    def test_top_level_timing_alongside_run_limits_keeps_canonical_block(self):
        """Top-level timing is dropped (unknown field); the canonical run_limits block stands."""
        with pytest.warns(DeprecationWarning, match=r"unknown top-level field 'max_turns'"):
            task = _minimal_task(max_turns=20, run_limits={"max_turns": 5})
        assert task.run_limits is not None
        assert task.run_limits.max_turns == 5

    def test_max_iterations_dropped_with_warning(self):
        """max_iterations was removed in PR #191; the soft-launch hook flags it."""
        with pytest.warns(DeprecationWarning, match=r"unknown top-level field 'max_iterations'"):
            task = _minimal_task(max_iterations=2)
        assert not hasattr(task, "max_iterations")

    def test_llm_reviewer_dropped_with_warning(self):
        """llm_reviewer was removed in PR #191; the soft-launch hook flags it."""
        with pytest.warns(DeprecationWarning, match=r"unknown top-level field 'llm_reviewer'"):
            task = _minimal_task(llm_reviewer={"enabled": False})
        assert not hasattr(task, "llm_reviewer")


class TestRunLimitsOnExperimentLayers:
    def test_defaults_accepts_block(self):
        defaults = ExperimentDefaults(run_limits=RunLimits(max_total_tokens=1000))
        assert defaults.run_limits is not None
        assert defaults.run_limits.max_total_tokens == 1000

    def test_variant_accepts_block(self):
        variant = ExperimentVariant(variant_id="v1", run_limits=RunLimits(max_usd=1.0))
        assert variant.run_limits is not None
        assert variant.run_limits.max_usd == 1.0


class TestFinalStatusBudget:
    def test_categories(self):
        assert FinalStatus.TOKEN_BUDGET_EXCEEDED.category == "failed"
        assert FinalStatus.COST_BUDGET_EXCEEDED.category == "failed"

    def test_icons(self):
        assert FinalStatus.TOKEN_BUDGET_EXCEEDED.icon == "#"
        assert FinalStatus.COST_BUDGET_EXCEEDED.icon == "$"


class TestBudgetExceededError:
    def test_message_format(self):
        err = BudgetExceededError("usd", actual=0.2, limit=0.1, task_id="t", iteration=3)
        assert err.budget_name == "usd"
        assert err.actual == 0.2
        assert err.limit == 0.1
        assert "usd budget exceeded" in str(err)
        assert "iteration 3" in str(err)

    def test_no_iteration_suffix(self):
        err = BudgetExceededError("input_tokens", actual=100, limit=50)
        assert err.iteration is None
        assert "iteration" not in str(err)

    def test_categorization(self):
        """BudgetExceededError must categorise as BUDGET_EXCEEDED, not UNKNOWN."""
        from coder_eval.errors.categories import ErrorCategory
        from coder_eval.errors.categorization import categorize_error

        err = BudgetExceededError("input_tokens", actual=10, limit=1)
        assert categorize_error(err, {"component": "orchestrator.run_limits.tokens"}) == (ErrorCategory.BUDGET_EXCEEDED)

    def test_categorization_not_retryable(self):
        """BUDGET_EXCEEDED must NOT have a retry config (retrying compounds the breach)."""
        from coder_eval.errors.categories import RETRY_CONFIG, ErrorCategory

        assert ErrorCategory.BUDGET_EXCEEDED not in RETRY_CONFIG
