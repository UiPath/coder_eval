"""Tests for commands_efficiency criterion and compute function."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from coder_eval.criteria.commands_efficiency import CommandsEfficiencyChecker, compute_commands_efficiency
from coder_eval.models import CommandsEfficiencyCriterion
from coder_eval.sandbox import Sandbox


def _mock_turn(num_commands: int) -> MagicMock:
    """Create a mock TurnRecord with N commands."""
    turn = MagicMock()
    turn.commands = [MagicMock() for _ in range(num_commands)]
    return turn


class TestComputeCommandsEfficiency:
    def test_zero_actual_returns_zero(self):
        assert compute_commands_efficiency(0, 10) == 0.0

    def test_at_budget(self):
        assert compute_commands_efficiency(10, 10) == 1.0

    def test_under_budget(self):
        assert compute_commands_efficiency(5, 10) == 1.0

    def test_one_over(self):
        assert compute_commands_efficiency(6, 5) == pytest.approx(5 / 6)

    def test_double_budget(self):
        assert compute_commands_efficiency(20, 10) == 0.5

    def test_ten_x_over(self):
        assert compute_commands_efficiency(100, 10) == 0.1

    def test_range_always_0_to_1(self):
        for actual in range(0, 21):
            score = compute_commands_efficiency(actual, 10)
            assert 0.0 <= score <= 1.0

    def test_expected_zero_raises(self):
        with pytest.raises(ValueError, match="expected must be >= 1"):
            compute_commands_efficiency(5, 0)

    def test_expected_negative_raises(self):
        with pytest.raises(ValueError, match="expected must be >= 1"):
            compute_commands_efficiency(5, -1)


class TestCommandsEfficiencyCriterionModel:
    def test_expected_commands_ge_1(self):
        with pytest.raises(ValidationError):
            CommandsEfficiencyCriterion(expected_commands=0, description="test")

    def test_valid(self):
        c = CommandsEfficiencyCriterion(expected_commands=5, description="test")
        assert c.expected_commands == 5

    def test_requires_agent_is_true(self):
        assert CommandsEfficiencyCriterion.requires_agent is True


class TestCommandsEfficiencyChecker:
    def test_no_turn_records(self):
        checker = CommandsEfficiencyChecker()
        c = CommandsEfficiencyCriterion(expected_commands=10, description="test")
        result = checker._check_impl(c, sandbox=MagicMock(spec=Sandbox), turn_records=None)
        assert result.score == 0.0
        assert result.error is not None

    def test_empty_turns(self):
        checker = CommandsEfficiencyChecker()
        c = CommandsEfficiencyCriterion(expected_commands=10, description="test")
        result = checker._check_impl(c, sandbox=MagicMock(spec=Sandbox), turn_records=[])
        assert result.score == 0.0
        assert "No commands" in result.details

    def test_turns_with_no_commands(self):
        checker = CommandsEfficiencyChecker()
        c = CommandsEfficiencyCriterion(expected_commands=10, description="test")
        result = checker._check_impl(c, sandbox=MagicMock(spec=Sandbox), turn_records=[_mock_turn(0), _mock_turn(0)])
        assert result.score == 0.0

    def test_at_budget(self):
        checker = CommandsEfficiencyChecker()
        c = CommandsEfficiencyCriterion(expected_commands=10, description="test")
        result = checker._check_impl(c, sandbox=MagicMock(spec=Sandbox), turn_records=[_mock_turn(10)])
        assert result.score == 1.0
        assert "at or under budget" in result.details

    def test_under_budget(self):
        checker = CommandsEfficiencyChecker()
        c = CommandsEfficiencyCriterion(expected_commands=10, description="test")
        result = checker._check_impl(c, sandbox=MagicMock(spec=Sandbox), turn_records=[_mock_turn(5)])
        assert result.score == 1.0

    def test_over_budget(self):
        checker = CommandsEfficiencyChecker()
        c = CommandsEfficiencyCriterion(expected_commands=10, description="test")
        result = checker._check_impl(c, sandbox=MagicMock(spec=Sandbox), turn_records=[_mock_turn(20)])
        assert result.score == 0.5
        assert "over budget" in result.details

    def test_multi_turn(self):
        checker = CommandsEfficiencyChecker()
        c = CommandsEfficiencyCriterion(expected_commands=10, description="test")
        turns = [_mock_turn(4), _mock_turn(3), _mock_turn(5)]  # total=12
        result = checker._check_impl(c, sandbox=MagicMock(spec=Sandbox), turn_records=turns)
        assert result.score == pytest.approx(10 / 12)

    def test_result_fields(self):
        checker = CommandsEfficiencyChecker()
        c = CommandsEfficiencyCriterion(expected_commands=10, description="my desc")
        result = checker._check_impl(c, sandbox=MagicMock(spec=Sandbox), turn_records=[_mock_turn(10)])
        assert result.criterion_type == "commands_efficiency"
        assert result.description == "my desc"
        assert result.error is None
