"""The `execute` -> `evaluate` -> `aggregate` loop.

`coder-eval execute` withholds the verdict; `coder-eval evaluate <run_dir>`
supplies it later. The pair only earns its keep if it ends up where a single
`coder-eval run` would have: same status, same score, same criteria, and a
`run.json` the rest of the toolchain can read.

Everything here runs against the agentless task — deterministic, no API key.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.models import FinalStatus


runner = CliRunner()

AGENTLESS_TASK = Path("tasks/agentless_smoke_test.yaml")

pytestmark = pytest.mark.skipif(
    not AGENTLESS_TASK.is_file(), reason="needs a source checkout (tasks/ is not in the wheel)"
)


def _task_dir(run_dir: Path) -> Path:
    matches = sorted(p.parent for p in run_dir.glob("**/task.json"))
    assert len(matches) == 1, f"expected exactly one task.json under {run_dir}, got {matches}"
    return matches[0]


def _row(task_dir: Path, name: str = "task.json") -> dict[str, Any]:
    return json.loads((task_dir / name).read_text(encoding="utf-8"))


def _invoke(args: list[str]) -> Any:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, f"{args} failed:\n{result.output}"
    return result


def test_execute_then_evaluate_reaches_the_same_verdict_as_run(tmp_path: Path) -> None:
    """The headline guarantee, asserted against a real `run` rather than a
    hardcoded expectation — so a change that breaks BOTH paths still fails."""
    direct = tmp_path / "direct"
    _invoke(["run", str(AGENTLESS_TASK), "--run-dir", str(direct)])
    expected = _row(_task_dir(direct))

    split = tmp_path / "split"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(split)])
    _invoke(["evaluate", str(_task_dir(split))])
    actual = _row(_task_dir(split))

    assert expected["final_status"] == FinalStatus.SUCCESS.value, "the fixture must actually pass under `run`"
    assert actual["final_status"] == expected["final_status"]
    assert actual["weighted_score"] == expected["weighted_score"]
    assert [c["criterion_type"] for c in actual["success_criteria_results"]] == [
        c["criterion_type"] for c in expected["success_criteria_results"]
    ]
    assert [c["score"] for c in actual["success_criteria_results"]] == [
        c["score"] for c in expected["success_criteria_results"]
    ]


def test_evaluate_upgrades_the_row_in_place_and_keeps_the_original(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)
    assert _row(task_dir)["final_status"] == FinalStatus.NOT_GRADED.value

    _invoke(["evaluate", str(task_dir)])

    assert _row(task_dir)["final_status"] == FinalStatus.SUCCESS.value
    # The pre-grade record survives, so "this run was executed separately" stays
    # auditable rather than being silently overwritten.
    assert _row(task_dir, "task.execute.json")["final_status"] == FinalStatus.NOT_GRADED.value


def test_aggregate_rebuilds_a_graded_run_json_with_no_extra_step(tmp_path: Path) -> None:
    """Grading in place is what makes the rest of the toolchain free: the
    existing `aggregate` command sees the upgraded rows with no new code."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["tasks_not_graded"] == 1

    _invoke(["evaluate", str(_task_dir(run_dir))])
    _invoke(["aggregate", str(run_dir)])

    summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert summary["tasks_not_graded"] == 0
    assert summary["tasks_succeeded"] == 1
    assert summary["pass_rate"] == 1.0


def test_re_grade_carries_the_trajectory_not_an_empty_one(tmp_path: Path) -> None:
    """Criteria that read the agent's tool calls (command_executed,
    skill_triggered, judges with trajectory) score off `iterations`. A re-grade
    that dropped them would silently fail every such criterion."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)
    executed = _row(task_dir)

    _invoke(["evaluate", str(task_dir)])
    graded = _row(task_dir)

    assert len(graded["iterations"]) == len(executed["iterations"])
    assert graded["iteration_count"] == executed["iteration_count"]


def test_evaluate_does_not_move_or_delete_the_graded_workspace(tmp_path: Path) -> None:
    """Run-dir mode adopts the workspace; the caller keeps ownership."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    proof = sorted(run_dir.glob("**/artifacts/**/proof.txt"))
    assert proof, "fixture precondition: execute preserved a workspace"

    _invoke(["evaluate", str(_task_dir(run_dir))])

    assert proof[0].is_file(), "the adopted workspace was moved or deleted"


def test_evaluate_still_grades_a_plain_directory(tmp_path: Path) -> None:
    """The original two-argument form must keep working unchanged."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "proof.txt").write_text("coder-eval-ran-without-a-coder", encoding="utf-8")

    result = runner.invoke(app, ["evaluate", str(AGENTLESS_TASK), str(work)])

    assert result.exit_code == 0, result.output
    assert "All criteria passed" in result.output


def test_evaluate_rejects_workspace_flag_outside_run_dir_mode(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    result = runner.invoke(app, ["evaluate", str(AGENTLESS_TASK), str(work), "--workspace", str(work)])
    assert result.exit_code != 0
    assert "run directory only" in result.output


# --------------------------------------------------------------------------
# `run --resume` over an executed run dir
# --------------------------------------------------------------------------


def test_run_resume_grades_the_ungraded_rows_it_finds(tmp_path: Path) -> None:
    """The whole point of the resume fix: `run --resume` over an executed run
    must GRADE those rows, not report "already complete" and exit 0."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    assert _row(_task_dir(run_dir))["final_status"] == FinalStatus.NOT_GRADED.value

    result = _invoke(["run", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])

    assert "grading 1" in result.output
    assert _row(_task_dir(run_dir))["final_status"] == FinalStatus.SUCCESS.value
    summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert summary["tasks_not_graded"] == 0
    assert summary["tasks_succeeded"] == 1
    assert summary["pass_rate"] == 1.0


def test_run_resume_does_not_re_execute_the_agent(tmp_path: Path) -> None:
    """Grading must reuse the trajectory on disk. Re-executing would discard the
    expensive half — the reason `execute` and `run` were split at all."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    executed = _row(_task_dir(run_dir))

    result = _invoke(["run", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])

    assert "running 0 remaining" in result.output, "the task was re-executed instead of graded"
    graded = _row(_task_dir(run_dir))
    assert len(graded["iterations"]) == len(executed["iterations"])
    # The row still describes the TASK, not the grading pass: a re-execution
    # would restamp these, and reporting the grading pass's 2s as the task's
    # duration would corrupt average_duration and every harness comparison.
    assert graded["started_at"] == executed["started_at"]
    assert graded["duration_seconds"] == executed["duration_seconds"]
    # The grading pass's own cost is kept alongside, not discarded.
    assert "grading_duration_seconds" in graded["environment_info"]
    # The pre-grade record is preserved by this path too, not just by `evaluate`.
    assert _row(_task_dir(run_dir), "task.execute.json")["final_status"] == FinalStatus.NOT_GRADED.value


def test_execute_resume_treats_an_executed_row_as_done(tmp_path: Path) -> None:
    """`execute --resume` owes a NOT_GRADED row nothing — it finished executing."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])

    result = _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])

    assert "1 task(s) already complete" in result.output
    assert "grading" not in result.output
    assert _row(_task_dir(run_dir))["final_status"] == FinalStatus.NOT_GRADED.value


def test_a_detached_grade_does_not_re_run_pre_run_against_the_workspace(tmp_path: Path) -> None:
    """`run()` calls the pre/post-run hooks unconditionally, with cwd = the
    sandbox. On an ADOPTED sandbox that sandbox is the agent's own output, and
    several in-tree tasks stage fixtures there (`cp -a /app/[!.]* "$PWD/"`), so
    re-running them would overwrite the deliverables before the criteria read
    them — changing the verdict and destroying preserved artifacts."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)
    proof = sorted(run_dir.glob("**/artifacts/**/proof.txt"))[0]
    # Mark the agent's file. The fixture's pre_run rewrites proof.txt from
    # scratch, so a re-run would wipe this marker.
    proof.write_text("coder-eval-ran-without-a-coder AND-THE-AGENT-EDITED-THIS", encoding="utf-8")

    _invoke(["evaluate", str(task_dir)])

    assert "AND-THE-AGENT-EDITED-THIS" in proof.read_text(encoding="utf-8"), (
        "pre_run re-ran against the adopted workspace and overwrote the agent's work"
    )
    # The hooks' recorded outcomes are carried over rather than lost.
    assert _row(task_dir)["pre_run_results"], "the execute phase's pre_run results were dropped"


def test_run_resume_exits_non_zero_when_it_cannot_grade(tmp_path: Path) -> None:
    """`run` was asked for a verdict. If grading fails, reporting exit 0 tells CI
    the suite is fine when nothing was actually scored."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    # Remove the workspace so the re-grade has nothing to grade against.
    shutil.rmtree(run_dir / "default" / "agentless_smoke_test" / "00" / "artifacts", ignore_errors=True)
    row = _task_dir(run_dir) / "task.json"
    record = json.loads(row.read_text(encoding="utf-8"))
    record["sandbox_path"] = str(tmp_path / "gone")
    row.write_text(json.dumps(record), encoding="utf-8")

    result = runner.invoke(app, ["run", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])

    assert result.exit_code != 0, "a run that graded nothing must not report success"


def test_execute_still_exits_zero_with_every_row_ungraded(tmp_path: Path) -> None:
    """The other side of the rule above: under `execute` an ungraded row is the
    expected outcome, not a failure of the command."""
    run_dir = tmp_path / "r"
    result = runner.invoke(app, ["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    assert result.exit_code == 0, result.output


def test_execute_to_run_resume_emits_no_config_drift_warning(tmp_path: Path) -> None:
    """`grade` is exempt from the fingerprint diff: this flow is supported, and
    the warning's "keeps their original-config results" text is wrong for it."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])

    result = _invoke(["run", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])

    assert "run config changed" not in result.output
