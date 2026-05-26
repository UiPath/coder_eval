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

    def test_expected_turns_default_none(self):
        assert RunLimits().expected_turns is None

    def test_expected_turns_lower_bound(self):
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            RunLimits(expected_turns=0)
        assert RunLimits(expected_turns=1).expected_turns == 1

    def test_expected_turns_yaml_coercion(self):
        assert RunLimits.model_validate({"expected_turns": "10"}).expected_turns == 10

    def test_expected_turns_greater_than_max_turns_allowed(self):
        rl = RunLimits(max_turns=5, expected_turns=20)
        assert rl.max_turns == 5
        assert rl.expected_turns == 20

    def test_extra_forbid_still_rejects_unknowns(self):
        with pytest.raises(ValidationError):
            RunLimits.model_validate({"expected_turn": 5})


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

    def test_top_level_max_turns_hoisted_with_warning(self):
        """Top-level max_turns was removed in 2026-05-12 — hoist shim lifts it into run_limits."""
        with pytest.warns(DeprecationWarning, match=r"Top-level 'max_turns'.*2026-05-20"):
            task = _minimal_task(max_turns=20)
        assert task.run_limits is not None
        assert task.run_limits.max_turns == 20

    def test_top_level_task_timeout_hoisted_with_warning(self):
        with pytest.warns(DeprecationWarning, match=r"Top-level 'task_timeout'.*2026-05-20"):
            task = _minimal_task(task_timeout=600)
        assert task.run_limits is not None
        assert task.run_limits.task_timeout == 600

    def test_top_level_turn_timeout_hoisted_with_warning(self):
        with pytest.warns(DeprecationWarning, match=r"Top-level 'turn_timeout'.*2026-05-20"):
            task = _minimal_task(turn_timeout=120)
        assert task.run_limits is not None
        assert task.run_limits.turn_timeout == 120

    def test_top_level_and_run_limits_conflict_raises(self):
        """Setting same key both top-level and in run_limits is an error."""
        with pytest.raises(ValidationError, match=r"max_turns.*both at top level"):
            _minimal_task(max_turns=20, run_limits={"max_turns": 5})

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
