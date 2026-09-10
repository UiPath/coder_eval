"""Tests for Phase 4 Tier 1: per-suite pass-rate rollup writer."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.models import (
    AgentKind,
    ClassificationCriterionResult,
    CriterionAggregate,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    SkillTriggeredCriterion,
    SuiteRollup,
    TaskResult,
)
from coder_eval.reports import (
    _attach_row_accounting,
    _compute_suite_rollup,
    _render_criterion_aggregate,
    _render_suite_markdown,
    write_suite_rollups,
)


def _make_result(
    *,
    task_id: str,
    final_status: FinalStatus,
    weighted_score: float | None,
    criteria: list[tuple[str, float, str | None]],
    error_message: str | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        task_id=task_id,
        task_description="t",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=final_status,
        weighted_score=weighted_score,
        iteration_count=1,
        success_criteria_results=[
            CriterionResult(criterion_type=ctype, description="c", score=score, error=err)
            for ctype, score, err in criteria
        ],
        error_message=error_message,
    )


def _make_row(
    *,
    suite_id: str,
    row_id: str,
    final_status: FinalStatus,
    weighted_score: float | None,
    criteria: list[tuple[str, float, str | None]],
    variant_id: str = "v1",
    error_message: str | None = None,
    replicate_index: int = 0,
) -> TaskResult:
    task_id = f"{suite_id}/{row_id}"
    return TaskResult(
        task_id=task_id,
        variant_id=variant_id,
        duration=1.0,
        suite_id=suite_id,
        row_id=row_id,
        replicate_index=replicate_index,
        result=_make_result(
            task_id=task_id,
            final_status=final_status,
            weighted_score=weighted_score,
            criteria=criteria,
            error_message=error_message,
        ),
    )


class TestComputeSuiteRollup:
    def test_all_passed(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id=f"r{i}",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None)],
            )
            for i in range(3)
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        assert rollup.rows_total == 3
        assert rollup.rows_passed == 3
        assert rollup.rows_failed == 0
        assert rollup.rows_error == 0
        assert rollup.pass_rate == 1.0
        assert rollup.average_weighted_score == 1.0
        assert rollup.failed_samples == []

    def test_mixed_outcomes(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id="r1",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None)],
            ),
            _make_row(
                suite_id="s",
                row_id="r2",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.4,
                criteria=[("file_exists", 0.0, None)],
            ),
            _make_row(
                suite_id="s",
                row_id="r3",
                final_status=FinalStatus.ERROR,
                weighted_score=None,
                criteria=[],
                error_message="boom",
            ),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        assert rollup.rows_total == 3
        assert rollup.rows_passed == 1
        assert rollup.rows_failed == 1
        assert rollup.rows_error == 1
        assert abs(rollup.pass_rate - 1 / 3) < 1e-9
        # average only over rows with weighted_score
        assert rollup.average_weighted_score is not None
        assert abs(rollup.average_weighted_score - 0.7) < 1e-9
        # failed samples cover the failed and error rows
        ids = sorted(s.row_id for s in rollup.failed_samples if s.row_id is not None)
        assert ids == ["r2", "r3"]

    def test_informational_criterion_is_not_a_failure_reason(self, tmp_path: Path) -> None:
        """A weight-0 criterion cannot fail a row, so it must not explain one.

        The row here fails on its gating criterion; the informational one also
        scored 0. Only the gating miss may appear in failure_reasons — otherwise
        the sample tells a reviewer to go fix a criterion that never gated.
        """
        row = _make_row(
            suite_id="s",
            row_id="r1",
            final_status=FinalStatus.FAILURE,
            weighted_score=0.0,
            criteria=[("file_exists", 0.0, None), ("skill_triggered", 0.0, None)],
        )
        row.result.success_criteria_results[1].gating = False
        row.result.success_criteria_results[0].details = "gating miss"
        row.result.success_criteria_results[1].details = "informational miss"

        rollup = _compute_suite_rollup("s", "v1", [row], tmp_path)

        reasons = rollup.failed_samples[0].failure_reasons
        assert reasons == ["gating miss"]

    def test_per_criterion_stats_averaging(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id="r1",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.5,
                criteria=[("file_exists", 1.0, None), ("run_command", 0.0, None)],
            ),
            _make_row(
                suite_id="s",
                row_id="r2",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None), ("run_command", 1.0, None)],
            ),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        by_type = {cs.criterion_type: cs for cs in rollup.criterion_stats}
        assert by_type["file_exists"].average_score == 1.0
        assert by_type["file_exists"].rows_evaluated == 2
        assert by_type["run_command"].average_score == 0.5
        assert by_type["run_command"].rows_evaluated == 2

    def test_error_count_per_criterion(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id="r1",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.0,
                criteria=[("run_command", 0.0, "checker blew up")],
            ),
            _make_row(
                suite_id="s",
                row_id="r2",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("run_command", 1.0, None)],
            ),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        stat = next(cs for cs in rollup.criterion_stats if cs.criterion_type == "run_command")
        assert stat.error_count == 1

    def test_empty_suite(self, tmp_path: Path) -> None:
        rollup = _compute_suite_rollup("s", "v1", [], tmp_path)
        assert rollup.rows_total == 0
        # None, not 0.0. A suite that measured nothing has no pass rate, and
        # 0.0 publishes "0.0%" — indistinguishable from a suite where every row
        # failed. Same rule as RunSummary.pass_rate and VariantAggregate.
        assert rollup.pass_rate is None
        assert rollup.rows_graded == 0
        assert rollup.average_weighted_score is None


class TestWriteSuiteRollups:
    def test_writes_suite_json_and_md(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="suite",
                row_id=f"r{i}",
                final_status=FinalStatus.SUCCESS if i == 0 else FinalStatus.FAILURE,
                weighted_score=1.0 if i == 0 else 0.2,
                criteria=[("file_exists", 1.0 if i == 0 else 0.0, None)],
            )
            for i in range(3)
        ]
        rollups = write_suite_rollups(tmp_path, rows)
        assert len(rollups) == 1
        assert rollups[0].suite_id == "suite"
        assert rollups[0].variant_id == "v1"
        suite_dir = tmp_path / "v1" / "suite"
        assert (suite_dir / "suite.json").exists()

        parsed = SuiteRollup.model_validate_json((suite_dir / "suite.json").read_text())
        assert parsed.suite_id == "suite"
        assert parsed.variant_id == "v1"
        assert parsed.rows_total == 3
        assert parsed.rows_passed == 1

        suite_md = (suite_dir / "suite.md").read_text()
        assert "Suite Rollup: suite" in suite_md
        assert "Variant" in suite_md
        assert "Pass rate" in suite_md
        assert "task.json" in suite_md

    def test_groups_by_variant_and_suite(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s1",
                row_id="r1",
                variant_id="vA",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None)],
            ),
            _make_row(
                suite_id="s1",
                row_id="r1",
                variant_id="vB",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None)],
            ),
            _make_row(
                suite_id="s2",
                row_id="r1",
                variant_id="vA",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None)],
            ),
        ]
        rollups = write_suite_rollups(tmp_path, rows)
        # Three distinct groups: (vA, s1), (vB, s1), (vA, s2)
        assert len(rollups) == 3
        expected = {("vA", "s1"), ("vB", "s1"), ("vA", "s2")}
        assert {(r.variant_id, r.suite_id) for r in rollups} == expected
        for r in rollups:
            assert (tmp_path / r.variant_id / r.suite_id / "suite.json").exists()

    def test_skips_non_dataset_tasks(self, tmp_path: Path) -> None:
        # A plain (non-dataset) task has suite_id=None; it must not produce a rollup.
        tr = TaskResult(
            task_id="plain",
            variant_id="v1",
            duration=1.0,
            suite_id=None,
            row_id=None,
            result=_make_result(
                task_id="plain",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None)],
            ),
        )
        rollups = write_suite_rollups(tmp_path, [tr])
        assert rollups == []
        # No files created under run_dir
        assert not any(tmp_path.iterdir())

    def test_failed_samples_capped(self, tmp_path: Path) -> None:
        # Generate more failed rows than the cap to confirm truncation.
        from coder_eval.reports import _FAILED_SAMPLE_LIMIT

        rows = [
            _make_row(
                suite_id="big",
                row_id=f"r{i:03d}",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.0,
                criteria=[("file_exists", 0.0, None)],
            )
            for i in range(_FAILED_SAMPLE_LIMIT + 5)
        ]
        write_suite_rollups(tmp_path, rows)
        parsed = SuiteRollup.model_validate_json((tmp_path / "v1" / "big" / "suite.json").read_text())
        assert len(parsed.failed_samples) == _FAILED_SAMPLE_LIMIT

    def test_task_json_relpath_points_under_run_dir(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="suite",
                row_id="r1",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.0,
                criteria=[("file_exists", 0.0, None)],
            ),
        ]
        write_suite_rollups(tmp_path, rows)
        data = json.loads((tmp_path / "v1" / "suite" / "suite.json").read_text())
        assert data["failed_samples"][0]["task_json_relpath"] == "v1/suite/r1/00/task.json"


class TestRenderSuiteMarkdown:
    def test_contains_headline_and_summary(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="my-suite",
                row_id=f"r{i}",
                final_status=FinalStatus.SUCCESS if i < 2 else FinalStatus.FAILURE,
                weighted_score=1.0 if i < 2 else 0.0,
                criteria=[("file_exists", 1.0 if i < 2 else 0.0, None)],
                variant_id="v-prod",
            )
            for i in range(3)
        ]
        rollup = _compute_suite_rollup("my-suite", "v-prod", rows, tmp_path)
        md = _render_suite_markdown(rollup)

        assert md.startswith("# Suite Rollup: my-suite")
        assert "**Variant**: `v-prod`" in md
        assert "3 total" in md
        assert "2 passed" in md
        assert "1 failed" in md
        assert "0 errored" in md
        assert "Pass rate**: 66.7%" in md
        assert "Average weighted score**:" in md

    def test_criterion_table_rendered(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id="r1",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None), ("run_command", 1.0, None)],
            ),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        md = _render_suite_markdown(rollup)
        assert "## Criterion stats" in md
        assert "| `file_exists` | 1 |" in md
        assert "| `run_command` | 1 |" in md

    def test_failed_samples_section_rendered(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id="r1",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.2,
                criteria=[("file_exists", 0.0, None)],
            ),
            _make_row(
                suite_id="s",
                row_id="r2",
                final_status=FinalStatus.ERROR,
                weighted_score=None,
                criteria=[],
                error_message="the sandbox exploded",
            ),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        md = _render_suite_markdown(rollup)
        assert "## Failed/errored samples" in md
        assert "### `s/r1` — FAILURE" in md
        assert "### `s/r2` — ERROR" in md
        assert "the sandbox exploded" in md
        # Row-level task.json link, relative to the suite dir (strips the variant prefix)
        assert "[task.json](./s/r1/00/task.json)" in md

    def test_no_failed_section_when_all_pass(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id=f"r{i}",
                final_status=FinalStatus.SUCCESS,
                weighted_score=1.0,
                criteria=[("file_exists", 1.0, None)],
            )
            for i in range(2)
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        md = _render_suite_markdown(rollup)
        assert "Failed/errored samples" not in md

    def test_renders_when_relpath_not_under_variant(self, tmp_path: Path) -> None:
        """Guard against crash when a FailedRowSummary carries a relpath that
        doesn't start with variant_id (e.g. an absolute fallback path).

        The field is typed as `str`, so a caller can legitimately hand us a
        value that `PurePosixPath.relative_to(variant_id)` cannot strip.
        The renderer must degrade to a best-effort link rather than raising.
        """
        from coder_eval.models import FailedRowSummary, SuiteRollup

        rollup = SuiteRollup(
            suite_id="s",
            variant_id="v1",
            rows_total=1,
            rows_passed=0,
            rows_failed=1,
            rows_error=0,
            pass_rate=0.0,
            average_weighted_score=0.0,
            criterion_stats=[],
            failed_samples=[
                FailedRowSummary(
                    row_id="r1",
                    task_id="s/r1",
                    final_status=FinalStatus.FAILURE,
                    weighted_score=0.0,
                    failure_reasons=["file_exists: score=0.00"],
                    error_message=None,
                    task_json_relpath="/abs/path/s/r1/00/task.json",
                ),
            ],
            criterion_aggregates=[],
            passed=False,
        )
        md = _render_suite_markdown(rollup)
        # Rendering must succeed; link should be best-effort (full relpath).
        assert "[task.json]" in md
        assert "/abs/path/s/r1/00/task.json" in md

    def test_omits_avg_score_when_none(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id="r1",
                final_status=FinalStatus.ERROR,
                weighted_score=None,
                criteria=[],
                error_message="boom",
            ),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        md = _render_suite_markdown(rollup)
        assert "Average weighted score" not in md


class TestReplicateIndexInSuiteRollup:
    def test_failed_row_summary_carries_replicate_index(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id="r1",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.0,
                criteria=[("file_exists", 0.0, None)],
                replicate_index=2,
            ),
            _make_row(
                suite_id="s",
                row_id="r2",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.0,
                criteria=[("file_exists", 0.0, None)],
                replicate_index=0,
            ),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        by_row = {s.row_id: s for s in rollup.failed_samples}
        assert by_row["r1"].replicate_index == 2
        assert by_row["r2"].replicate_index == 0

    def test_task_json_relpath_uses_replicate_index(self, tmp_path: Path) -> None:
        rows = [
            _make_row(
                suite_id="s",
                row_id="r1",
                final_status=FinalStatus.FAILURE,
                weighted_score=0.0,
                criteria=[("file_exists", 0.0, None)],
                replicate_index=3,
            ),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)
        assert rollup.failed_samples[0].task_json_relpath == "v1/s/r1/03/task.json"


class TestStackedSameTypeAggregation:
    """A task can stack multiple criteria of the SAME type (e.g. activation's
    per-skill skill_triggered) and each gets its OWN across-row aggregate, sliced
    by position and keyed by description — not one type-pooled number repeated per
    instance (the pre-fix behavior). This is what makes per-skill recall real."""

    @staticmethod
    def _row(row_id: str, scores: tuple[float, float]) -> TaskResult:
        # Two criteria of the same type at index 0/1, with distinct descriptions.
        result = EvaluationResult(
            task_id=f"s/{row_id}",
            task_description="t",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status=FinalStatus.SUCCESS,
            weighted_score=sum(scores) / len(scores),
            iteration_count=1,
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="crit-A", score=scores[0]),
                CriterionResult(criterion_type="file_exists", description="crit-B", score=scores[1]),
            ],
        )
        return TaskResult(
            task_id=f"s/{row_id}",
            variant_id="v1",
            duration=1.0,
            suite_id="s",
            row_id=row_id,
            replicate_index=0,
            result=result,
        )

    def test_each_instance_aggregates_its_own_column(self, tmp_path: Path) -> None:
        rows = [self._row("r1", (1.0, 0.0)), self._row("r2", (1.0, 0.0))]
        criteria = [
            FileExistsCriterion(description="crit-A", path="a.txt"),
            FileExistsCriterion(description="crit-B", path="b.txt"),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path, task_criteria=criteria)
        aggs = rollup.criterion_aggregates
        assert len(aggs) == 2
        by_desc = {a.description: a for a in aggs}
        assert set(by_desc) == {"crit-A", "crit-B"}
        # crit-A saw column [1.0, 1.0] -> mean 1.0; crit-B saw [0.0, 0.0] -> mean 0.0.
        # Pooling by type (the old bug) would feed BOTH the combined [1,1,0,0] -> 0.5.
        assert by_desc["crit-A"].metrics["mean"] == 1.0
        assert by_desc["crit-B"].metrics["mean"] == 0.0


class TestRowAccounting:
    """Phase 1: CriterionAggregate row-accounting fields + _attach_row_accounting."""

    def test_aggregate_defaults_are_zero(self) -> None:
        # Existing call sites construct without the new fields — they must default to 0.
        agg = CriterionAggregate(criterion_type="file_exists", passed=True)
        assert agg.rows_total == 0
        assert agg.rows_excluded == 0

    def test_attach_row_accounting_with_excluded_rows(self) -> None:
        agg = CriterionAggregate(criterion_type="skill_triggered", metrics={"mean": 1.0}, passed=True)
        out = _attach_row_accounting(agg, rows_total=4, rows_aggregated=3)
        assert out.rows_total == 4
        assert out.rows_excluded == 1
        assert out.metrics["completion_rate"] == 0.75
        # Base metrics preserved.
        assert out.metrics["mean"] == 1.0

    def test_attach_row_accounting_zero_total_no_div_error(self) -> None:
        agg = CriterionAggregate(criterion_type="file_exists", passed=True)
        out = _attach_row_accounting(agg, rows_total=0, rows_aggregated=0)
        assert out.rows_total == 0
        assert out.rows_excluded == 0
        assert out.metrics["completion_rate"] == 0.0

    def test_render_includes_denominator_line_when_excluded(self) -> None:
        agg = CriterionAggregate(
            criterion_type="skill_triggered",
            metrics={"completion_rate": 0.75},
            passed=True,
            rows_total=4,
            rows_excluded=1,
        )
        text = "\n".join(_render_criterion_aggregate(agg))
        assert "Denominator: 3/4 rows (1 excluded" in text

    def test_render_omits_denominator_line_when_no_exclusions(self) -> None:
        agg = CriterionAggregate(
            criterion_type="file_exists",
            metrics={"completion_rate": 1.0},
            passed=True,
            rows_total=4,
            rows_excluded=0,
        )
        text = "\n".join(_render_criterion_aggregate(agg))
        assert "Denominator:" not in text


class TestErrorRowDenominator:
    """Phase 2: an ERROR row (success_criteria_results=[]) is silently dropped from a
    classification criterion's per-row slice, shrinking the recall denominator. The
    row-accounting fields make that drop auditable, and completion_rate makes it
    gateable. Rows carry ClassificationCriterionResult so the skill_triggered overlay
    (accuracy / recall / F1) actually runs."""

    @staticmethod
    def _labelled_row(row_id: str, expected: str, observed: str) -> TaskResult:
        score = 1.0 if expected == observed else 0.0
        status = FinalStatus.SUCCESS if score == 1.0 else FinalStatus.FAILURE
        result = EvaluationResult(
            task_id=f"s/{row_id}",
            task_description="t",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status=status,
            weighted_score=score,
            iteration_count=1,
            success_criteria_results=[
                ClassificationCriterionResult(
                    criterion_type="skill_triggered",
                    description="foo activation",
                    score=score,
                    observed_label=observed,
                    expected_label=expected,
                )
            ],
        )
        return TaskResult(
            task_id=f"s/{row_id}",
            variant_id="v1",
            duration=1.0,
            suite_id="s",
            row_id=row_id,
            replicate_index=0,
            result=result,
        )

    @staticmethod
    def _error_row(row_id: str) -> TaskResult:
        result = EvaluationResult(
            task_id=f"s/{row_id}",
            task_description="t",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status=FinalStatus.ERROR,
            weighted_score=None,
            iteration_count=1,
            success_criteria_results=[],
            error_message="boom",
        )
        return TaskResult(
            task_id=f"s/{row_id}",
            variant_id="v1",
            duration=1.0,
            suite_id="s",
            row_id=row_id,
            replicate_index=0,
            result=result,
        )

    def _rows(self) -> list[TaskResult]:
        # 3 labelled rows (2 expected 'yes', 1 expected 'no') + 1 ERROR row.
        return [
            self._labelled_row("r1", expected="yes", observed="yes"),
            self._labelled_row("r2", expected="yes", observed="no"),
            self._labelled_row("r3", expected="no", observed="no"),
            self._error_row("r4"),
        ]

    def _criterion(self, suite_thresholds: dict[str, float] | None = None) -> SkillTriggeredCriterion:
        return SkillTriggeredCriterion(
            description="foo activation",
            skill_name="foo",
            expected_skill="foo",
            suite_thresholds=suite_thresholds,
        )

    def test_error_row_excluded_from_recall_denominator(self, tmp_path: Path) -> None:
        rollup = _compute_suite_rollup("s", "v1", self._rows(), tmp_path, task_criteria=[self._criterion()])
        assert len(rollup.criterion_aggregates) == 1
        agg = rollup.criterion_aggregates[0]
        # The classification overlay only saw the 3 labelled rows — the ERROR row dropped.
        assert agg.details["total_pairs"] == 3
        # recall.yes denominator = the 2 expected-'yes' rows (r1 hit, r2 miss) → 0.5,
        # NOT diluted by the 4th (errored) row.
        assert agg.metrics["recall.yes"] == 0.5
        # Row accounting captures the drop.
        assert agg.rows_total == 4
        assert agg.rows_excluded == 1
        assert agg.metrics["completion_rate"] == 0.75

    def test_completion_rate_gate_fails_when_too_many_errored(self, tmp_path: Path) -> None:
        rollup = _compute_suite_rollup(
            "s",
            "v1",
            self._rows(),
            tmp_path,
            task_criteria=[self._criterion(suite_thresholds={"completion_rate": 0.95})],
        )
        assert rollup.passed is False

    def test_completion_rate_gate_passes_when_few_errored(self, tmp_path: Path) -> None:
        rollup = _compute_suite_rollup(
            "s",
            "v1",
            self._rows(),
            tmp_path,
            task_criteria=[self._criterion(suite_thresholds={"completion_rate": 0.5})],
        )
        assert rollup.passed is True

    def test_missing_aggregator_threshold_check_matches_injected_metric(self, tmp_path: Path) -> None:
        # All rows errored → aggregate() returns None → missing-aggregator path.
        # The injected completion_rate (0.0 here) must be reflected in BOTH metrics
        # AND its threshold check (no contradiction), while the stub still fails
        # loudly because the real metrics could not be computed.
        rows = [self._error_row("r1"), self._error_row("r2")]
        rollup = _compute_suite_rollup(
            "s",
            "v1",
            rows,
            tmp_path,
            task_criteria=[self._criterion(suite_thresholds={"completion_rate": 0.95})],
        )
        assert len(rollup.criterion_aggregates) == 1
        agg = rollup.criterion_aggregates[0]
        assert agg.error is not None  # fail-loudly semantics preserved
        assert agg.passed is False
        assert agg.rows_total == 2
        assert agg.rows_excluded == 2
        assert agg.metrics["completion_rate"] == 0.0
        cr_check = next(c for c in agg.threshold_checks if c.metric == "completion_rate")
        # Threshold table and metrics table must not disagree on the same metric.
        assert cr_check.actual_value == agg.metrics["completion_rate"]
        assert cr_check.passed is False
        assert rollup.passed is False


def _row(row_id: str, final_status: FinalStatus, weighted_score: float | None) -> TaskResult:
    """A one-criterion suite row. An ungraded row carries NO criteria results —
    that is what `execute` leaves behind, and grading them is the whole point."""
    criteria = [] if weighted_score is None else [("file_exists", weighted_score, None)]
    return _make_row(
        suite_id="s",
        row_id=row_id,
        final_status=final_status,
        weighted_score=weighted_score,
        criteria=criteria,
    )


class TestSuiteRollupUngradedBucket:
    """The fourth bucket on the suite surface, which shipped with no coverage.

    An ungraded row (`coder-eval execute`) leaves BOTH sides of the suite pass
    rate — it is not a pass and not a failure — and it carries no failure reason,
    so it must not appear in `failed_samples` either. The row-count invariant
    counts it, so a row landing outside all four buckets fails loudly instead of
    silently vanishing from the rollup.
    """

    def test_ungraded_rows_leave_both_sides_of_the_pass_rate(self, tmp_path: Path) -> None:
        rows = [
            _row("r1", FinalStatus.SUCCESS, 1.0),
            _row("r2", FinalStatus.NOT_GRADED, None),
            _row("r3", FinalStatus.NOT_GRADED, None),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)

        assert rollup.rows_total == 3
        assert rollup.rows_not_graded == 2
        assert rollup.rows_graded == 1
        # 1 of 1 graded, not 1 of 3.
        assert rollup.pass_rate == 1.0

    def test_a_fully_ungraded_suite_has_no_pass_rate(self, tmp_path: Path) -> None:
        rows = [_row(f"r{i}", FinalStatus.NOT_GRADED, None) for i in range(3)]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)

        assert rollup.rows_graded == 0
        assert rollup.pass_rate is None
        assert "n/a" in _render_suite_markdown(rollup)

    def test_ungraded_rows_are_not_collected_as_failed_samples(self, tmp_path: Path) -> None:
        """`failed_samples` is documented as failed/errored rows. An ungraded row
        has no failure reasons to show, so listing it contradicts the same
        function's pass-rate rule two blocks up."""
        rows = [
            _row("r1", FinalStatus.FAILURE, 0.0),
            _row("r2", FinalStatus.NOT_GRADED, None),
        ]
        rollup = _compute_suite_rollup("s", "v1", rows, tmp_path)

        assert [s.row_id for s in rollup.failed_samples] == ["r1"]

    def test_a_row_outside_every_bucket_fails_the_invariant(self) -> None:
        with pytest.raises(ValueError, match="Suite row count invariant violated"):
            SuiteRollup(
                suite_id="s",
                variant_id="v",
                rows_total=3,
                rows_passed=1,
                rows_failed=0,
                rows_error=0,
                rows_not_graded=1,  # 1+0+0+1 != 3
                pass_rate=1.0,
            )
