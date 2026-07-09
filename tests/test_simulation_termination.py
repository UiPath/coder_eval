"""Tests for the dialog-termination predicate."""

from __future__ import annotations

from coder_eval.models import SimulationConfig
from coder_eval.simulation.termination import DialogStopReason, evaluate_stop, strip_stop_token


def _cfg(**overrides):
    base = {
        "enabled": True,
        "persona": "p",
        "goal": "g",
        "max_turns": 5,
        "stop_on_criteria_pass": False,
        "check_criteria": "end_of_dialog",
    }
    base.update(overrides)
    return SimulationConfig(**base)


class TestEvaluateStop:
    """``evaluate_stop`` covers criteria-pass, max-turns, and budget.

    Stop-token detection lives at the call site (``SimulatorResult.stop_requested``),
    not in the predicate — these tests do not exercise it.
    """

    def test_keeps_going_when_nothing_triggers(self):
        d = evaluate_stop(
            config=_cfg(),
            turns_completed=1,
            total_tokens_used=0,
            criteria_all_passed=False,
        )
        assert d.stop is False
        assert d.reason is None

    def test_max_turns_hit(self):
        d = evaluate_stop(
            config=_cfg(max_turns=3),
            turns_completed=3,
            total_tokens_used=0,
            criteria_all_passed=False,
        )
        assert d.stop is True
        assert d.reason == DialogStopReason.MAX_TURNS

    def test_criteria_pass_with_early_stop(self):
        d = evaluate_stop(
            config=_cfg(stop_on_criteria_pass=True, check_criteria="every_turn"),
            turns_completed=1,
            total_tokens_used=0,
            criteria_all_passed=True,
        )
        assert d.stop is True
        assert d.reason == DialogStopReason.CRITERIA_PASSED

    def test_criteria_pass_ignored_when_early_stop_disabled(self):
        d = evaluate_stop(
            config=_cfg(stop_on_criteria_pass=False),
            turns_completed=1,
            total_tokens_used=0,
            criteria_all_passed=True,
        )
        assert d.stop is False

    def test_budget_exhausted(self):
        d = evaluate_stop(
            config=_cfg(max_total_tokens=100),
            turns_completed=1,
            total_tokens_used=150,
            criteria_all_passed=False,
        )
        assert d.stop is True
        assert d.reason == DialogStopReason.BUDGET

    def test_precedence_criteria_over_max_turns(self):
        """When both criteria-pass and max-turns would fire, criteria wins."""
        d = evaluate_stop(
            config=_cfg(stop_on_criteria_pass=True, check_criteria="every_turn", max_turns=3),
            turns_completed=3,
            total_tokens_used=0,
            criteria_all_passed=True,
        )
        assert d.reason == DialogStopReason.CRITERIA_PASSED

    def test_precedence_max_turns_over_budget(self):
        """When both max-turns and budget would fire, max-turns wins."""
        d = evaluate_stop(
            config=_cfg(max_turns=2, max_total_tokens=100),
            turns_completed=2,
            total_tokens_used=200,
            criteria_all_passed=False,
        )
        assert d.reason == DialogStopReason.MAX_TURNS


class TestStripStopToken:
    def test_removes_token(self):
        assert strip_stop_token("Thanks. <<<END>>>", "<<<END>>>") == "Thanks."

    def test_no_token_returns_unchanged(self):
        assert strip_stop_token("Thanks.", "<<<END>>>") == "Thanks."

    def test_strips_surrounding_whitespace(self):
        assert strip_stop_token("   <<<END>>>   ", "<<<END>>>") == ""
