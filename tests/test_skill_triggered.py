"""Tests for SkillTriggeredCriterion + checker + the triggering task YAML."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from coder_eval.criteria.skill_triggered import SkillTriggeredChecker
from coder_eval.models import (
    ClassificationCriterionResult,
    CriterionResult,
    SkillTriggeredCriterion,
    TaskDefinition,
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


def _run_check(
    *, expected: str, commands: list[CommandTelemetry], skill_name: str | None = None
) -> ClassificationCriterionResult:
    criterion = SkillTriggeredCriterion(
        description="did agent invoke a skill?",
        expected=expected,
        skill_name=skill_name,
    )
    checker = SkillTriggeredChecker()
    result = checker.check(criterion, sandbox=None, turn_records=[_turn(commands)])  # type: ignore[arg-type]
    assert isinstance(result, ClassificationCriterionResult)
    return result


class TestSkillTriggeredChecker:
    def test_skill_invoked_expected_yes_passes(self) -> None:
        result = _run_check(
            expected="yes",
            commands=[_cmd("Skill", {"skill": "uipath-flow"})],
        )
        assert result.score == 1.0
        assert result.observed_label == "yes"
        assert result.expected_label == "yes"

    def test_no_skill_invoked_expected_no_passes(self) -> None:
        result = _run_check(expected="no", commands=[_cmd("Read", {"file_path": "x"})])
        assert result.score == 1.0
        assert result.observed_label == "no"
        assert result.expected_label == "no"

    def test_skill_invoked_expected_no_fails(self) -> None:
        # Agent triggered a skill on a prompt we wanted it to ignore.
        result = _run_check(expected="no", commands=[_cmd("Skill", {"skill": "uipath-flow"})])
        assert result.score == 0.0
        assert result.observed_label == "yes"

    def test_no_skill_invoked_expected_yes_fails(self) -> None:
        result = _run_check(expected="yes", commands=[_cmd("Read", {"file_path": "x"})])
        assert result.score == 0.0
        assert result.observed_label == "no"

    def test_other_tools_dont_count(self) -> None:
        # Several non-Skill tools, expected=no should still observe "no".
        result = _run_check(
            expected="no",
            commands=[_cmd("Bash", {"command": "ls"}), _cmd("Read", {"file_path": "x"}), _cmd("Write", {})],
        )
        assert result.score == 1.0
        assert result.observed_label == "no"

    def test_skill_name_filter_matches(self) -> None:
        result = _run_check(
            expected="yes",
            commands=[_cmd("Skill", {"skill": "uipath-flow"})],
            skill_name="uipath-flow",
        )
        assert result.observed_label == "yes"
        assert result.score == 1.0

    def test_skill_name_filter_rejects_other_skill(self) -> None:
        # A Skill call happened, but not the one we're filtering on.
        result = _run_check(
            expected="no",
            commands=[_cmd("Skill", {"skill": "rpa"})],
            skill_name="uipath-flow",
        )
        assert result.observed_label == "no"  # filter missed -> "no"
        assert result.score == 1.0

    def test_no_turn_records_returns_base_result_with_error(self) -> None:
        criterion = SkillTriggeredCriterion(description="d", expected="yes")
        checker = SkillTriggeredChecker()
        result = checker.check(criterion, sandbox=None, turn_records=None)  # type: ignore[arg-type]
        # No turn records -> base CriterionResult with error, NOT a
        # ClassificationCriterionResult. The aggregator skips these rows,
        # keeping the classification math unpolluted.
        assert not isinstance(result, ClassificationCriterionResult)
        assert result.score == 0.0
        assert result.error is not None


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
        criterion = SkillTriggeredCriterion(description="d", expected="yes")
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
        assert SkillTriggeredChecker().aggregate(SkillTriggeredCriterion(description="d", expected="yes"), []) is None

    def test_aggregate_non_classification_rows_returns_baseline_only(self) -> None:
        # Rows without labels (e.g., error path) -> baseline stats, no classification.
        per_rows: list[CriterionResult] = [
            CriterionResult(criterion_type="skill_triggered", description="d", score=0.0, error="boom"),
        ]
        checker = SkillTriggeredChecker()
        agg = checker.aggregate(SkillTriggeredCriterion(description="d", expected="yes"), per_rows)
        assert agg is not None
        assert "accuracy" not in agg.metrics  # no classification pairs
        assert agg.metrics["count"] == 1.0


class TestRegistryAndYaml:
    def test_registered(self) -> None:
        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=True)
        assert "skill_triggered" in CriterionRegistry.list_types()

    def test_triggering_yaml_loads_and_expands(self) -> None:
        from coder_eval.orchestration.task_loader import expand_dataset, load_task

        task_file = Path("tasks/uipath_flow/triggering/triggering.yaml")
        assert task_file.exists()
        task, _ = load_task(task_file)
        assert task.dataset is not None

        # The YAML ships with dataset.sample set as a cheap-smoke default, so
        # expansion without a CLI override should cap to that value. Asserting
        # via the dataset model rather than a hardcoded literal keeps this test
        # stable if the smoke default is retuned.
        expected_rows = task.dataset.sample or 247
        expanded = expand_dataset(task, task_file.parent)
        assert len(expanded) == expected_rows

        # CLI override (max_rows) wins over the task-level sample and returns
        # the full dataset; 247 is the row count the build script produces.
        full = expand_dataset(task, task_file.parent, max_rows=1000)
        assert len(full) == 247

        # Row ids are sequential r001.. and should_trigger is threaded into the criterion.
        first = full[0]
        assert first.row_id == "r001"
        crit = first.success_criteria[0]
        assert crit.type == "skill_triggered"
        assert crit.expected in {"yes", "no"}  # type: ignore[attr-defined]

    def test_triggering_jsonl_shape(self) -> None:
        jsonl = Path("tasks/uipath_flow/triggering/triggering.jsonl")
        assert jsonl.exists()
        rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
        assert len(rows) == 247
        # Every row has id / prompt / should_trigger, labels are yes/no only.
        for r in rows:
            assert set(r.keys()) == {"id", "prompt", "should_trigger"}
            assert r["should_trigger"] in {"yes", "no"}
            assert r["prompt"]

    def test_triggering_yaml_has_suite_thresholds(self) -> None:
        data = yaml.safe_load(Path("tasks/uipath_flow/triggering/triggering.yaml").read_text())
        task = TaskDefinition(**data)
        crit = task.success_criteria[0]
        thresholds = crit.suite_thresholds
        assert thresholds is not None
        assert "accuracy" in thresholds
        assert "recall.yes" in thresholds
        assert "recall.no" in thresholds
