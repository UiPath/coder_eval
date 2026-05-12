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
    def test_empty_block_rejected(self):
        with pytest.raises(ValidationError, match="run_limits requires at least one of"):
            RunLimits()

    def test_only_max_input_tokens_ok(self):
        assert RunLimits(max_input_tokens=1).max_input_tokens == 1

    def test_only_max_output_tokens_ok(self):
        assert RunLimits(max_output_tokens=1).max_output_tokens == 1

    def test_only_max_total_tokens_ok(self):
        assert RunLimits(max_total_tokens=1).max_total_tokens == 1

    def test_only_max_usd_ok(self):
        assert RunLimits(max_usd=0.01).max_usd == 0.01

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

    def test_empty_run_limits_on_task_rejected(self):
        with pytest.raises(ValidationError, match="run_limits requires at least one of"):
            _minimal_task(run_limits={})


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
