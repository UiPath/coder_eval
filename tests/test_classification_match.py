"""Tests for ClassificationMatchCriterion checker + its aggregate() (accuracy/recall/F1)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from coder_eval.criteria.classification_match import ClassificationMatchChecker
from coder_eval.models import (
    AgentKind,
    ClassificationCriterionResult,
    ClassificationMatchCriterion,
    CriterionResult,
    EvaluationResult,
    FinalStatus,
    TaskDefinition,
    TaskResult,
)
from coder_eval.reports import _compute_suite_rollup, _render_suite_markdown


class _FakeSandbox:
    """Minimal sandbox surface needed by ClassificationMatchChecker.get_file_content."""

    def __init__(self, files: dict[str, str]):
        self._files = files

    def get_file_content(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]


def _check(
    *,
    path: str,
    expected_label: str,
    allowed_labels: list[str],
    files: dict[str, str],
    case_sensitive: bool = False,
) -> ClassificationCriterionResult:
    crit = ClassificationMatchCriterion(
        description="label match",
        path=path,
        expected_label=expected_label,
        allowed_labels=allowed_labels,
        case_sensitive=case_sensitive,
    )
    checker = ClassificationMatchChecker()
    sandbox = _FakeSandbox(files)
    result = checker.check(crit, sandbox)  # type: ignore[arg-type]
    assert isinstance(result, ClassificationCriterionResult), (
        f"classification_match checker should return ClassificationCriterionResult; got {type(result).__name__}"
    )
    return result


def _result(ctype: str, score: float, observed: str | None = None, expected: str | None = None) -> CriterionResult:
    """Build a per-row CriterionResult for aggregator tests.

    When both labels are given, returns a ClassificationCriterionResult; otherwise a bare CriterionResult
    (simulates non-classification criteria for the filter path in aggregate()).
    """
    if observed is not None and expected is not None:
        return ClassificationCriterionResult(
            criterion_type=ctype,
            description="c",
            score=score,
            observed_label=observed,
            expected_label=expected,
        )
    return CriterionResult(criterion_type=ctype, description="c", score=score)


class TestClassificationMatchChecker:
    def test_exact_match(self) -> None:
        result = _check(
            path="result.txt",
            expected_label="positive",
            allowed_labels=["positive", "negative"],
            files={"result.txt": "positive\n"},
        )
        assert result.score == 1.0
        assert result.observed_label == "positive"
        assert result.expected_label == "positive"

    def test_case_insensitive_canonicalization(self) -> None:
        result = _check(
            path="result.txt",
            expected_label="positive",
            allowed_labels=["positive", "negative"],
            files={"result.txt": "POSITIVE"},
        )
        assert result.score == 1.0
        assert result.observed_label == "positive"

    def test_case_sensitive_mismatch(self) -> None:
        result = _check(
            path="result.txt",
            expected_label="positive",
            allowed_labels=["positive", "negative"],
            files={"result.txt": "POSITIVE"},
            case_sensitive=True,
        )
        assert result.score == 0.0
        assert result.observed_label == "(other)"

    def test_wrong_label(self) -> None:
        result = _check(
            path="result.txt",
            expected_label="positive",
            allowed_labels=["positive", "negative"],
            files={"result.txt": "negative"},
        )
        assert result.score == 0.0
        assert result.observed_label == "negative"
        assert result.expected_label == "positive"

    def test_unknown_label_becomes_other_sentinel(self) -> None:
        result = _check(
            path="result.txt",
            expected_label="positive",
            allowed_labels=["positive", "negative"],
            files={"result.txt": "maybe"},
        )
        assert result.score == 0.0
        assert result.observed_label == "(other)"

    def test_missing_file_becomes_none_sentinel(self) -> None:
        result = _check(
            path="result.txt",
            expected_label="positive",
            allowed_labels=["positive", "negative"],
            files={},
        )
        assert result.score == 0.0
        assert result.observed_label == "(none)"

    def test_empty_file_becomes_none_sentinel(self) -> None:
        result = _check(
            path="result.txt",
            expected_label="positive",
            allowed_labels=["positive", "negative"],
            files={"result.txt": "   \n"},
        )
        assert result.score == 0.0
        assert result.observed_label == "(none)"


class TestClassificationMatchAggregate:
    """Across-row aggregate: accuracy / recall / F1 / confusion."""

    def _criterion(self, allowed: list[str]) -> ClassificationMatchCriterion:
        return ClassificationMatchCriterion(
            description="c",
            path="result.txt",
            expected_label="unused",  # per-row field; aggregate reads labels off CriterionResults
            allowed_labels=allowed,
        )

    def test_three_row_mislabeled_example(self) -> None:
        # Mirrors tasks/sentiment_classification.yaml.
        # r1: expected=positive, observed=positive → TP(positive)
        # r2: expected=negative, observed=negative → TP(negative)
        # r3: expected=negative (false), observed=positive → FP(positive), FN(negative)
        per_rows = [
            _result("classification_match", 1.0, observed="positive", expected="positive"),
            _result("classification_match", 1.0, observed="negative", expected="negative"),
            _result("classification_match", 0.0, observed="positive", expected="negative"),
        ]
        checker = ClassificationMatchChecker()
        aggregate = checker.aggregate(self._criterion(["positive", "negative"]), per_rows)
        assert aggregate is not None

        m = aggregate.metrics
        assert m["accuracy"] == pytest.approx(2 / 3)
        assert m["recall.positive"] == pytest.approx(1.0)
        assert m["recall.negative"] == pytest.approx(0.5)
        assert m["precision.positive"] == pytest.approx(0.5)
        assert m["precision.negative"] == pytest.approx(1.0)
        assert m["f1.positive"] == pytest.approx(2 / 3)
        assert m["f1.negative"] == pytest.approx(2 / 3)
        assert m["macro_f1"] == pytest.approx(2 / 3)
        assert m["weighted_f1"] == pytest.approx(2 / 3)
        # micro_f1 == accuracy for single-label multiclass
        assert m["micro_f1"] == pytest.approx(m["accuracy"])

        # Confusion pairs stashed in details (as dicts, for serialisation).
        conf = {(c["expected"], c["observed"]): c["count"] for c in aggregate.details["confusion"]}
        assert conf == {
            ("negative", "negative"): 1,
            ("negative", "positive"): 1,
            ("positive", "positive"): 1,
        }
        assert sorted(aggregate.details["labels"]) == ["negative", "positive"]
        assert aggregate.details["total_pairs"] == 3

    def test_no_labels_returns_baseline_stats_only(self) -> None:
        # Rows without observed/expected labels: classification metrics skipped,
        # but baseline stats inherited from BaseCriterion.aggregate() still land.
        per_rows = [_result("classification_match", 1.0)]
        checker = ClassificationMatchChecker()
        agg = checker.aggregate(self._criterion(["a", "b"]), per_rows)
        assert agg is not None
        assert agg.metrics == {
            "count": 1.0,
            "mean": 1.0,
            "median": 1.0,
            "std": 0.0,
            "min": 1.0,
            "max": 1.0,
        }
        assert "accuracy" not in agg.metrics  # no classification pairs

    def test_empty_per_rows_returns_none(self) -> None:
        checker = ClassificationMatchChecker()
        assert checker.aggregate(self._criterion(["a", "b"]), []) is None

    def test_merges_baseline_with_classification(self) -> None:
        per_rows = [
            _result("classification_match", 1.0, observed="a", expected="a"),
            _result("classification_match", 0.0, observed="b", expected="a"),
        ]
        checker = ClassificationMatchChecker()
        agg = checker.aggregate(self._criterion(["a", "b"]), per_rows)
        assert agg is not None
        # Baseline stats
        assert agg.metrics["count"] == 2.0
        assert agg.metrics["mean"] == pytest.approx(0.5)
        assert agg.metrics["min"] == 0.0
        assert agg.metrics["max"] == 1.0
        # Classification metrics alongside
        assert "accuracy" in agg.metrics
        assert "macro_f1" in agg.metrics

    def test_perfect_predictions(self) -> None:
        per_rows = [
            _result("classification_match", 1.0, observed="a", expected="a"),
            _result("classification_match", 1.0, observed="b", expected="b"),
        ]
        checker = ClassificationMatchChecker()
        aggregate = checker.aggregate(self._criterion(["a", "b"]), per_rows)
        assert aggregate is not None
        m = aggregate.metrics
        assert m["accuracy"] == 1.0
        assert m["macro_f1"] == 1.0
        assert m["weighted_f1"] == 1.0
        assert m["micro_f1"] == 1.0

    def test_class_with_no_support(self) -> None:
        # Model predicted "c" but no row's ground truth is "c" — support=0, recall=0.
        per_rows = [
            _result("classification_match", 1.0, observed="a", expected="a"),
            _result("classification_match", 0.0, observed="c", expected="a"),
            _result("classification_match", 1.0, observed="b", expected="b"),
        ]
        checker = ClassificationMatchChecker()
        aggregate = checker.aggregate(self._criterion(["a", "b", "c"]), per_rows)
        assert aggregate is not None
        assert aggregate.metrics["recall.c"] == 0.0
        assert aggregate.metrics["precision.c"] == 0.0

    def test_sentinel_labels_appear(self) -> None:
        per_rows = [
            _result("classification_match", 0.0, observed="(none)", expected="positive"),
            _result("classification_match", 0.0, observed="(other)", expected="positive"),
        ]
        checker = ClassificationMatchChecker()
        aggregate = checker.aggregate(self._criterion(["positive", "negative"]), per_rows)
        assert aggregate is not None
        labels = set(aggregate.details["labels"])
        assert "(none)" in labels
        assert "(other)" in labels


class TestSuiteRollupWithAggregate:
    """Integration: _compute_suite_rollup runs aggregate() and applies thresholds."""

    def _row(self, row_id: str, expected: str, observed: str) -> TaskResult:
        score = 1.0 if expected == observed else 0.0
        final = FinalStatus.SUCCESS if score == 1.0 else FinalStatus.FAILURE
        cr = _result("classification_match", score, observed=observed, expected=expected)
        return TaskResult(
            task_id=f"s/{row_id}",
            variant_id="v1",
            duration=1.0,
            suite_id="s",
            row_id=row_id,
            result=EvaluationResult(
                task_id=f"s/{row_id}",
                task_description="t",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status=final,
                weighted_score=score,
                iteration_count=1,
                success_criteria_results=[cr],
            ),
        )

    def _criterion(self, *, suite_thresholds: dict[str, float] | None = None) -> ClassificationMatchCriterion:
        return ClassificationMatchCriterion(
            description="c",
            path="result.txt",
            expected_label="positive",
            allowed_labels=["positive", "negative"],
            suite_thresholds=suite_thresholds,
        )

    def _mislabeled_rows(self) -> list[TaskResult]:
        return [
            self._row("r1", "positive", "positive"),
            self._row("r2", "negative", "negative"),
            self._row("r3", "negative", "positive"),
        ]

    def test_aggregate_populated_from_checker(self, tmp_path: Path) -> None:
        rows = self._mislabeled_rows()
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=[self._criterion()])
        assert len(rollup.criterion_aggregates) == 1
        agg = rollup.criterion_aggregates[0]
        assert agg.criterion_type == "classification_match"
        assert agg.metrics["accuracy"] == pytest.approx(2 / 3)
        # No thresholds configured → aggregate passes trivially.
        assert agg.threshold_checks == []
        assert agg.passed is True
        assert rollup.passed is True

    def test_thresholds_all_pass(self, tmp_path: Path) -> None:
        rows = self._mislabeled_rows()
        crit = self._criterion(suite_thresholds={"accuracy": 0.5, "f1.positive": 0.5})
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=[crit])
        agg = rollup.criterion_aggregates[0]
        assert agg.passed is True
        assert rollup.passed is True
        assert all(c.passed for c in agg.threshold_checks)

    def test_thresholds_one_fails(self, tmp_path: Path) -> None:
        rows = self._mislabeled_rows()
        # accuracy (0.667) meets 0.5; recall.negative (0.5) fails 0.8.
        crit = self._criterion(suite_thresholds={"accuracy": 0.5, "recall.negative": 0.8})
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=[crit])
        agg = rollup.criterion_aggregates[0]
        assert agg.passed is False
        assert rollup.passed is False
        by_metric = {c.metric: c for c in agg.threshold_checks}
        assert by_metric["accuracy"].passed is True
        assert by_metric["recall.negative"].passed is False
        assert by_metric["recall.negative"].actual_value == pytest.approx(0.5)

    def test_threshold_on_missing_metric_fails(self, tmp_path: Path) -> None:
        rows = self._mislabeled_rows()
        crit = self._criterion(suite_thresholds={"does_not_exist": 0.5})
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=[crit])
        agg = rollup.criterion_aggregates[0]
        check = next(c for c in agg.threshold_checks if c.metric == "does_not_exist")
        assert check.actual_value is None
        assert check.passed is False
        assert agg.passed is False
        assert rollup.passed is False

    def test_default_aggregator_applies_to_any_criterion(self, tmp_path: Path) -> None:
        # Default BaseCriterion.aggregate() emits baseline stats for *every*
        # criterion — not just classification. file_exists here gets
        # count/mean/... and is thresholdable on them.
        from coder_eval.models import FileExistsCriterion

        def _row(row_id: str, score: float) -> TaskResult:
            return TaskResult(
                task_id=f"s/{row_id}",
                variant_id="v1",
                duration=1.0,
                suite_id="s",
                row_id=row_id,
                result=EvaluationResult(
                    task_id=f"s/{row_id}",
                    task_description="t",
                    agent_type=AgentKind.CLAUDE_CODE,
                    started_at=datetime.now(),
                    final_status=FinalStatus.SUCCESS if score == 1.0 else FinalStatus.FAILURE,
                    weighted_score=score,
                    iteration_count=1,
                    success_criteria_results=[
                        CriterionResult(criterion_type="file_exists", description="c", score=score)
                    ],
                ),
            )

        # Two rows: scores [1.0, 0.0]. mean = 0.5.
        rows = [_row("r1", 1.0), _row("r2", 0.0)]
        crit = FileExistsCriterion(description="c", path="out.txt", suite_thresholds={"mean": 0.4, "min": 0.0})
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=[crit])
        assert len(rollup.criterion_aggregates) == 1
        agg = rollup.criterion_aggregates[0]
        # Baseline stats present
        assert agg.metrics["count"] == 2.0
        assert agg.metrics["mean"] == pytest.approx(0.5)
        assert agg.metrics["min"] == 0.0
        assert agg.metrics["max"] == 1.0
        # Both thresholds pass
        assert agg.passed is True
        assert rollup.passed is True

    def test_threshold_on_baseline_mean_fails(self, tmp_path: Path) -> None:
        # file_exists with mean=0.5 fails a mean>=0.9 threshold.
        from coder_eval.models import FileExistsCriterion

        def _row(row_id: str, score: float) -> TaskResult:
            return TaskResult(
                task_id=f"s/{row_id}",
                variant_id="v1",
                duration=1.0,
                suite_id="s",
                row_id=row_id,
                result=EvaluationResult(
                    task_id=f"s/{row_id}",
                    task_description="t",
                    agent_type=AgentKind.CLAUDE_CODE,
                    started_at=datetime.now(),
                    final_status=FinalStatus.SUCCESS if score == 1.0 else FinalStatus.FAILURE,
                    weighted_score=score,
                    iteration_count=1,
                    success_criteria_results=[
                        CriterionResult(criterion_type="file_exists", description="c", score=score)
                    ],
                ),
            )

        rows = [_row("r1", 1.0), _row("r2", 0.0)]
        crit = FileExistsCriterion(description="c", path="out.txt", suite_thresholds={"mean": 0.9})
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=[crit])
        agg = rollup.criterion_aggregates[0]
        check = next(c for c in agg.threshold_checks if c.metric == "mean")
        assert check.actual_value == pytest.approx(0.5)
        assert check.passed is False
        assert rollup.passed is False

    def test_threshold_on_unknown_metric_fails_for_any_criterion(self, tmp_path: Path) -> None:
        # Threshold on a metric the default aggregator doesn't produce →
        # actual_value=None, threshold fails.
        from coder_eval.models import FileExistsCriterion

        row = TaskResult(
            task_id="s/r1",
            variant_id="v1",
            duration=1.0,
            suite_id="s",
            row_id="r1",
            result=EvaluationResult(
                task_id="s/r1",
                task_description="t",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                iteration_count=1,
                success_criteria_results=[CriterionResult(criterion_type="file_exists", description="c", score=1.0)],
            ),
        )
        crit = FileExistsCriterion(description="c", path="out.txt", suite_thresholds={"accuracy": 0.8})
        rollup = _compute_suite_rollup("s", "v1", [row], tmp_path, task_criteria=[crit])
        assert len(rollup.criterion_aggregates) == 1
        agg = rollup.criterion_aggregates[0]
        check = next(c for c in agg.threshold_checks if c.metric == "accuracy")
        assert check.actual_value is None
        assert check.passed is False
        assert agg.passed is False
        assert rollup.passed is False

    def test_missing_aggregator_explicit_none(self, tmp_path: Path) -> None:
        # An override that explicitly returns None still hits the
        # _build_missing_aggregator path — rare but possible.
        from coder_eval.reports import _build_missing_aggregator

        fallback = _build_missing_aggregator("custom", {"accuracy": 0.5})
        assert fallback.error is not None
        assert fallback.passed is False
        assert fallback.threshold_checks[0].actual_value is None

    def test_no_task_criteria_skips_aggregates(self, tmp_path: Path) -> None:
        rows = self._mislabeled_rows()
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=None)
        assert rollup.criterion_aggregates == []
        assert rollup.passed is True  # no gates configured

    def test_markdown_renders_aggregate_and_thresholds(self, tmp_path: Path) -> None:
        rows = self._mislabeled_rows()
        crit = self._criterion(suite_thresholds={"accuracy": 0.5, "recall.negative": 0.8})
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=[crit])
        md = _render_suite_markdown(rollup)

        assert "## Aggregate metrics — `classification_match`" in md
        assert "FAILED" in md  # recall.negative threshold missed
        assert "| metric | value |" in md
        assert "`accuracy`" in md
        assert "### Thresholds" in md
        assert "### Per-label breakdown" in md
        assert "### Confusion matrix" in md
        assert "**Suite gate**: FAILED" in md


class TestTaskLevelValidator:
    """suite_thresholds requires dataset: block."""

    def _base_data(self) -> dict[str, Any]:
        return {
            "task_id": "test",
            "description": "d",
            "initial_prompt": "hi",
            "sandbox": {"driver": "tempdir"},
            "success_criteria": [
                {
                    "type": "classification_match",
                    "description": "c",
                    "path": "r.txt",
                    "expected_label": "a",
                    "allowed_labels": ["a", "b"],
                    "suite_thresholds": {"accuracy": 0.9},
                }
            ],
        }

    def test_thresholds_without_dataset_errors(self) -> None:
        with pytest.raises(ValueError, match="suite_thresholds requires a dataset"):
            TaskDefinition(**self._base_data())

    def test_thresholds_with_dataset_ok(self) -> None:
        data = self._base_data()
        data["dataset"] = {"rows": [{"id": "r1"}]}
        task = TaskDefinition(**data)
        assert task.success_criteria[0].suite_thresholds == {"accuracy": 0.9}  # type: ignore[attr-defined]

    def test_no_thresholds_no_dataset_ok(self) -> None:
        data = self._base_data()
        data["success_criteria"][0].pop("suite_thresholds")
        task = TaskDefinition(**data)
        assert task.success_criteria[0].suite_thresholds is None  # type: ignore[attr-defined]


class TestClassificationMatchRegistered:
    def test_registered_in_criterion_registry(self) -> None:
        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=True)
        assert "classification_match" in CriterionRegistry.list_types()


class TestSentimentClassificationYaml:
    def test_task_yaml_parses(self) -> None:
        from coder_eval.orchestration.task_loader import expand_dataset, load_task

        task_file = Path("tasks/sentiment_classification.yaml")
        assert task_file.exists()
        task, _ = load_task(task_file)
        assert task.dataset is not None
        expanded = expand_dataset(task, task_file.parent)
        assert len(expanded) == 3
        assert [t.row_id for t in expanded] == ["r1", "r2", "r3"]
        assert expanded[2].task_id == "sentiment-classification/r3"

    def test_jsonl_dataset_contents(self) -> None:
        import json as _json

        jsonl = Path("tasks/datasets/sentiment.jsonl")
        assert jsonl.exists()
        rows = [_json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
        assert len(rows) == 3
        assert rows[2]["id"] == "r3"
        assert rows[2]["expected"] == "negative"


def test_example_yaml_loads_via_pydantic() -> None:
    data = yaml.safe_load(Path("tasks/sentiment_classification.yaml").read_text())
    task = TaskDefinition(**data)
    assert task.task_id == "sentiment-classification"
    assert task.dataset is not None
    assert task.dataset.paths == ["datasets/sentiment.jsonl"]
