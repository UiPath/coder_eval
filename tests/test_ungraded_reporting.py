"""How an ungraded row renders on every reporting surface.

`coder-eval execute` leaves rows `NOT_GRADED`, and each generator had to learn a
fourth category. Only the HTML badge got an assertion when that landed, so the
JUnit `<skipped>` element, the Markdown "Not Graded" bullets, and — most
importantly — the switched pass-rate DENOMINATOR were all shipped untested. The
denominator is the "gate that turns a gap into a score" shape: a printed rate
whose divisor changed, with nothing tripping it.

`VariantAggregate` is here too, mirroring the four `RunSummary` cases in
`test_execute_command.py`: the two models now carry the same formula, and only
one of them was tested.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from coder_eval.models import FinalStatus, RunSummary, VariantAggregate, VariantResult
from coder_eval.orchestration.experiment import _mean_graded_score, _pick_worst_status


def _summary(**kwargs: object) -> RunSummary:
    base: dict[str, object] = {
        "run_id": "r",
        "start_time": datetime(2026, 1, 1),
        "end_time": datetime(2026, 1, 1, 0, 1),
        "total_duration_seconds": 60.0,
        "tasks_run": 2,
        "tasks_succeeded": 1,
        "tasks_failed": 0,
        "tasks_error": 0,
        "tasks_not_graded": 1,
        "task_results": [],
        "framework_version": "test",
    }
    base.update(kwargs)
    return RunSummary(**base)  # type: ignore[arg-type]


def _aggregate(**kwargs: object) -> VariantAggregate:
    base: dict[str, object] = {
        "variant_id": "v",
        "tasks_run": 2,
        "tasks_succeeded": 1,
        "tasks_failed": 0,
        "tasks_error": 0,
        "tasks_not_graded": 1,
        "average_score": 1.0,
        "average_duration": 1.0,
    }
    base.update(kwargs)
    return VariantAggregate(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# VariantAggregate — the twin of the RunSummary cases in test_execute_command
# --------------------------------------------------------------------------


def test_variant_aggregate_counts_the_ungraded_bucket_in_its_invariant() -> None:
    with pytest.raises(ValueError, match="Task count invariant violated"):
        _aggregate(tasks_run=3)  # 1 + 0 + 0 + 1 != 3


def test_variant_aggregate_pass_rate_divides_by_graded_not_run() -> None:
    """One pass out of one GRADED task is 100%, even beside an ungraded one."""
    assert _aggregate().tasks_graded == 1
    assert _aggregate().pass_rate == 1.0


def test_variant_aggregate_has_no_pass_rate_when_nothing_was_graded() -> None:
    agg = _aggregate(tasks_run=2, tasks_succeeded=0, tasks_not_graded=2)
    assert agg.pass_rate is None, "0/0 is unknown, not 0%"


def test_variant_aggregate_serializes_its_denominator() -> None:
    """A consumer that cannot read `tasks_graded` re-derives the rate and drifts."""
    import json

    assert json.loads(_aggregate().model_dump_json())["tasks_graded"] == 1


def test_mean_graded_score_ignores_ungraded_rows() -> None:
    def _vr(score: float | None, status: FinalStatus) -> VariantResult:
        return VariantResult(
            variant_id="v", task_id="t", weighted_score=score, final_status=status, duration_seconds=1.0
        )

    rows = [_vr(1.0, FinalStatus.SUCCESS), _vr(None, FinalStatus.NOT_GRADED), _vr(None, FinalStatus.NOT_GRADED)]
    assert _mean_graded_score(rows) == 1.0
    # Nothing graded -> no mean at all. 0.0 would read as "measured and scored zero".
    assert _mean_graded_score(rows[1:]) is None


def test_ungraded_loses_to_every_real_outcome_when_picking_the_worst_status() -> None:
    """`_pick_worst_status` reports the replicate set's worst outcome. An ungraded
    replicate is not an outcome, so it must never mask a real one."""
    assert _pick_worst_status([FinalStatus.NOT_GRADED, FinalStatus.SUCCESS]) is FinalStatus.SUCCESS
    assert _pick_worst_status([FinalStatus.NOT_GRADED, FinalStatus.FAILURE]) is FinalStatus.FAILURE
    assert _pick_worst_status([FinalStatus.NOT_GRADED]) is FinalStatus.NOT_GRADED


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def test_markdown_pass_rate_uses_the_graded_denominator() -> None:
    from coder_eval.reports import _pass_rate_lines

    text = "\n".join(_pass_rate_lines(_summary()))

    assert "1/1" in text, "the denominator is tasks_graded, not tasks_run"
    assert "1/2" not in text


def test_markdown_reports_no_rate_at_all_for_a_fully_ungraded_run() -> None:
    from coder_eval.reports import _pass_rate_lines

    text = "\n".join(_pass_rate_lines(_summary(tasks_succeeded=0, tasks_not_graded=2)))

    assert "n/a" in text
    assert "0.0%" not in text, "a run that was never measured has no rate, not a 0% one"


def test_markdown_reports_no_rate_change_for_an_ordinary_graded_run() -> None:
    """The regression guard: adding the fourth bucket must not alter any surface
    of a run that has none."""
    from coder_eval.reports import _pass_rate_lines

    text = "\n".join(_pass_rate_lines(_summary(tasks_run=2, tasks_succeeded=1, tasks_failed=1, tasks_not_graded=0)))

    assert "1/2" in text
    assert "n/a" not in text


# --------------------------------------------------------------------------
# JUnit
# --------------------------------------------------------------------------


def _junit_for(status: FinalStatus, tmp_path: Path) -> Any:
    import json

    # defusedxml on the test side, matching tests/test_reports_junit.py.
    from defusedxml.ElementTree import parse as parse_xml

    from coder_eval.reports_junit import write_junit_xml

    run_dir = tmp_path / "run"
    task_dir = run_dir / "default" / "t" / "00"
    task_dir.mkdir(parents=True)
    row = {
        "task_id": "t",
        "task_description": "d",
        "variant_id": "default",
        "agent_type": "claude-code",
        "started_at": "2026-01-01T00:00:00",
        "final_status": status.value,
        "iteration_count": 1,
        "duration_seconds": 1.0,
    }
    (task_dir / "task.json").write_text(json.dumps(row), encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": "r",
                "start_time": "2026-01-01T00:00:00",
                "end_time": "2026-01-01T00:01:00",
                "total_duration_seconds": 60.0,
                "tasks_run": 1,
                "tasks_succeeded": 1 if status is FinalStatus.SUCCESS else 0,
                "tasks_failed": 0,
                "tasks_error": 0,
                "tasks_not_graded": 1 if status is FinalStatus.NOT_GRADED else 0,
                "task_results": [{"task_id": "t", "variant_id": "default", "status": status.value, "duration": 1.0}],
                "framework_version": "test",
            }
        ),
        encoding="utf-8",
    )
    written = write_junit_xml(run_dir, tmp_path / "junit.xml")
    return parse_xml(written).getroot()


def test_junit_marks_an_ungraded_row_skipped_not_failed(tmp_path: Path) -> None:
    root = _junit_for(FinalStatus.NOT_GRADED, tmp_path)

    skipped = root.findall(".//testcase/skipped")
    assert len(skipped) == 1, "an ungraded row is not a verdict, so it is <skipped>"
    assert "not graded" in (skipped[0].get("message") or "")
    assert not root.findall(".//testcase/failure"), "an ungraded row must not read as a failure"


def test_junit_counts_are_derived_from_the_children(tmp_path: Path) -> None:
    root = _junit_for(FinalStatus.NOT_GRADED, tmp_path)
    suite = root.find(".//testsuite")
    assert suite is not None
    assert suite.get("skipped") == "1"
    assert suite.get("failures") == "0"
    assert suite.get("errors") == "0"


def test_junit_still_passes_an_ordinary_graded_row(tmp_path: Path) -> None:
    root = _junit_for(FinalStatus.SUCCESS, tmp_path)
    assert not root.findall(".//testcase/skipped")
    assert not root.findall(".//testcase/failure")


# --------------------------------------------------------------------------
# The end-of-run console summary
# --------------------------------------------------------------------------


def _summary_output(summary: RunSummary, tmp_path: Path) -> str:
    from coder_eval.cli.console import console
    from coder_eval.cli.run_helpers import print_execution_summary

    with console.capture() as captured:
        print_execution_summary(tmp_path, summary)
    return captured.get()


def test_summary_reports_an_ungraded_run_as_executed_not_failed(tmp_path: Path) -> None:
    text = _summary_output(_summary(tasks_succeeded=0, tasks_not_graded=2), tmp_path)
    assert "2/2 executed, not graded" in text
    assert "0/0 succeeded" not in text, "a fully ungraded run has no succeeded ratio to report"


def test_summary_points_at_the_grading_form_that_keeps_the_trajectory(tmp_path: Path) -> None:
    """`evaluate <task.yaml> <workspace>` grades a bare directory with NO
    trajectory, so command_executed / skill_triggered / trajectory judges score
    differently from what `run` would have produced."""
    text = _summary_output(_summary(tasks_succeeded=0, tasks_not_graded=2), tmp_path)
    assert "--resume" in text or "<run_dir>" in text
    assert "evaluate <task.yaml> <workspace>" not in text


def test_summary_still_reports_a_ratio_for_an_empty_run(tmp_path: Path) -> None:
    """Both counters are falsy for tasks_run == 0; independent `if`s would print
    no Results line at all, where it previously printed 0/0."""
    empty = _summary(tasks_run=0, tasks_succeeded=0, tasks_not_graded=0)
    assert "0/0 succeeded" in _summary_output(empty, tmp_path)


def test_summary_is_unchanged_for_an_ordinary_graded_run(tmp_path: Path) -> None:
    text = _summary_output(_summary(tasks_run=2, tasks_succeeded=1, tasks_failed=1, tasks_not_graded=0), tmp_path)
    assert "1/2 succeeded" in text
    assert "not graded" not in text
