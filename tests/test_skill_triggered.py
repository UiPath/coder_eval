"""Tests for SkillTriggeredCriterion + checker."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from coder_eval.criteria.skill_triggered import SkillTriggeredChecker
from coder_eval.models import (
    ClassificationCriterionResult,
    CriterionResult,
    SkillTriggeredCriterion,
)
from coder_eval.models.results import TurnRecord
from coder_eval.models.telemetry import CommandTelemetry


def _cmd(tool_name: str, parameters: dict[str, Any] | None = None, tool_id: str = "t1") -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id,
        timestamp=datetime.now(),
        parameters=parameters or {},
        result_status="success",
    )


def _turn(commands: list[CommandTelemetry]) -> TurnRecord:
    return TurnRecord(iteration=1, user_input="p", agent_output="o", commands=commands)


def _check(*, expected_skill: str, skill_name: str, commands: list[CommandTelemetry]) -> ClassificationCriterionResult:
    criterion = SkillTriggeredCriterion(
        description="did agent invoke a skill?",
        expected_skill=expected_skill,
        skill_name=skill_name,
    )
    checker = SkillTriggeredChecker()
    result = checker.check(criterion, sandbox=None, turn_records=[_turn(commands)])  # type: ignore[arg-type]
    assert isinstance(result, ClassificationCriterionResult)
    return result


class TestSkillTriggeredChecker:
    def test_skill_invoked_tp(self) -> None:
        result = _check(
            expected_skill="uipath-flow", skill_name="uipath-flow", commands=[_cmd("Skill", {"skill": "uipath-flow"})]
        )
        assert result.score == 1.0 and result.observed_label == "yes" and result.expected_label == "yes"

    def test_no_skill_tn(self) -> None:
        result = _check(expected_skill="", skill_name="uipath-flow", commands=[_cmd("Read", {"file_path": "x"})])
        assert result.score == 1.0 and result.observed_label == "no" and result.expected_label == "no"

    def test_skill_invoked_fp(self) -> None:
        result = _check(expected_skill="", skill_name="uipath-flow", commands=[_cmd("Skill", {"skill": "uipath-flow"})])
        assert result.score == 0.0 and result.observed_label == "yes"

    def test_no_skill_fn(self) -> None:
        result = _check(
            expected_skill="uipath-flow", skill_name="uipath-flow", commands=[_cmd("Read", {"file_path": "x"})]
        )
        assert result.score == 0.0 and result.observed_label == "no"

    def test_other_tools_dont_count(self) -> None:
        result = _check(
            expected_skill="",
            skill_name="uipath-flow",
            commands=[_cmd("Bash", {"command": "ls"}), _cmd("Read", {"file_path": "x"}), _cmd("Write", {})],
        )
        assert result.score == 1.0 and result.observed_label == "no"

    def test_wrong_skill_name_not_counted(self) -> None:
        # Agent invoked "uipath-rpa", but criterion filters on "uipath-flow" -> observed="no".
        result = _check(expected_skill="", skill_name="uipath-flow", commands=[_cmd("Skill", {"skill": "uipath-rpa"})])
        assert result.observed_label == "no" and result.score == 1.0

    def test_namespaced_skill_param_matches(self) -> None:
        # Agent emits "uipath:uipath-flow" (plugin:skill prefix); strip before comparing.
        result = _check(
            expected_skill="uipath-flow",
            skill_name="uipath-flow",
            commands=[_cmd("Skill", {"skill": "uipath:uipath-flow"})],
        )
        assert result.observed_label == "yes" and result.score == 1.0

    def test_no_turn_records_returns_base_result_with_error(self) -> None:
        criterion = SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow")
        checker = SkillTriggeredChecker()
        result = checker.check(criterion, sandbox=None, turn_records=None)  # type: ignore[arg-type]
        # No turn records -> base CriterionResult with error, NOT a
        # ClassificationCriterionResult. The aggregator skips these rows,
        # keeping the classification math unpolluted.
        assert not isinstance(result, ClassificationCriterionResult)
        assert result.score == 0.0
        assert result.error is not None


class TestSkillTriggeredCriterionValidation:
    def test_requires_expected_skill_and_skill_name(self) -> None:
        with pytest.raises(ValueError):
            SkillTriggeredCriterion(description="d")  # type: ignore[call-arg]

    def test_valid_construction(self) -> None:
        c = SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow")
        assert c.expected_skill == "uipath-flow"
        assert c.skill_name == "uipath-flow"


class TestSkillTriggeredAggregate:
    def _row(self, observed: str, expected: str) -> ClassificationCriterionResult:
        return ClassificationCriterionResult(
            criterion_type="skill_triggered",
            description="d",
            score=1.0 if observed == expected else 0.0,
            observed_label=observed,
            expected_label=expected,
        )

    def test_aggregate_emits_classification_metrics(self) -> None:
        # 2 TP(yes), 1 TN(no), 1 FN(yes predicted as no), 1 FP(no predicted as yes).
        # yes class: TP=2, FP=1, FN=1 -> precision=2/3, recall=2/3, f1=2/3
        # no class:  TP=1, FP=1, FN=1 -> precision=1/2, recall=1/2, f1=1/2
        per_rows = [
            self._row("yes", "yes"),
            self._row("yes", "yes"),
            self._row("no", "no"),
            self._row("no", "yes"),
            self._row("yes", "no"),
        ]
        checker = SkillTriggeredChecker()
        criterion = SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow")
        agg = checker.aggregate(criterion, per_rows)
        assert agg is not None
        m = agg.metrics
        assert m["accuracy"] == pytest.approx(3 / 5)
        assert m["precision.yes"] == pytest.approx(2 / 3)
        assert m["recall.yes"] == pytest.approx(2 / 3)
        assert m["f1.yes"] == pytest.approx(2 / 3)
        assert m["precision.no"] == pytest.approx(1 / 2)
        assert m["recall.no"] == pytest.approx(1 / 2)
        # Baseline stats inherited
        assert m["count"] == 5.0
        assert m["mean"] == pytest.approx(3 / 5)

    def test_aggregate_empty_returns_none(self) -> None:
        assert (
            SkillTriggeredChecker().aggregate(
                SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow"), []
            )
            is None
        )

    def test_aggregate_non_classification_rows_returns_baseline_only(self) -> None:
        # Rows without labels (e.g., error path) -> baseline stats, no classification.
        per_rows: list[CriterionResult] = [
            CriterionResult(criterion_type="skill_triggered", description="d", score=0.0, error="boom"),
        ]
        checker = SkillTriggeredChecker()
        agg = checker.aggregate(
            SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow"), per_rows
        )
        assert agg is not None
        assert "accuracy" not in agg.metrics  # no classification pairs
        assert agg.metrics["count"] == 1.0


class TestRegistry:
    def test_registered(self) -> None:
        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=True)
        assert "skill_triggered" in CriterionRegistry.list_types()
