"""Tests for Phase 4 Tier 1: per-suite pass-rate rollup writer."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from coder_eval.models import (
    AgentKind,
    CriterionResult,
    EvaluationResult,
    FinalStatus,
    SuiteRollup,
    TaskResult,
)
from coder_eval.reports import _compute_suite_rollup, _render_suite_markdown, write_suite_rollups


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
        assert rollup.pass_rate == 0.0
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
