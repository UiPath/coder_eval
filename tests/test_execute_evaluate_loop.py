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
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.models import FinalStatus


runner = CliRunner()

AGENTLESS_TASK = Path("tasks/agentless_smoke_test.yaml")

pytestmark = pytest.mark.skipif(
    not AGENTLESS_TASK.is_file(), reason="needs a source checkout (tasks/ is not in the wheel)"
)


@pytest.fixture(autouse=True)
def _isolate_default_runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an omitted ``--run-dir`` inside ``tmp_path``.

    ``evaluate`` grades into a FRESH run directory (that is what leaves the
    graded row's own ``task.log`` alone), and every ``evaluate`` call below
    omits ``--run-dir`` because the flag is not what the test is about. Without
    this, each one created a repo-relative ``runs/<second-resolution-timestamp>/``
    — littering the working tree, and colliding between xdist workers that
    happen to reach the same second.
    """
    from coder_eval.cli import run_helpers

    monkeypatch.setattr(run_helpers.settings, "runs_dir", tmp_path / "default-runs")


def _task_dir(run_dir: Path) -> Path:
    matches = sorted(p.parent for p in run_dir.glob("**/task.json"))
    assert len(matches) == 1, f"expected exactly one task.json under {run_dir}, got {matches}"
    return matches[0]


def _row(task_dir: Path, name: str = "task.json") -> dict[str, Any]:
    return json.loads((task_dir / name).read_text(encoding="utf-8"))


def _invoke(args: list[str], *, expect_exit: int = 0) -> Any:
    """Run a CLI command and pin its exit code.

    ``expect_exit`` is explicit rather than "0 unless it raised" because the exit
    code IS the contract for a CI wrapper: a helper that always demanded 0 once
    pinned a preserved-TIMEOUT row exiting 0 under "All criteria passed" as the
    expected behaviour.
    """
    result = runner.invoke(app, args)
    assert result.exit_code == expect_exit, (
        f"{args} exited {result.exit_code}, expected {expect_exit}:\n{result.output}"
    )
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


def test_run_resume_keeps_the_row_regradeable_when_grading_crashes(tmp_path: Path) -> None:
    """A grading crash is not a verdict about the run. Folding the ORIGINAL
    ungraded row back keeps the task re-gradeable — writing ERROR over it would
    not, since ERROR is "complete" for both commands."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])

    with patch(
        "coder_eval.orchestration.regrade.regrade_in_place",
        new=AsyncMock(side_effect=RuntimeError("checker exploded")),
    ):
        result = runner.invoke(app, ["run", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])

    assert result.exit_code != 0, "a resume that graded nothing must not report success"
    assert _row(_task_dir(run_dir))["final_status"] == FinalStatus.NOT_GRADED.value
    # The reason is durable, not console-only. It lands in run.json rather than
    # task.json: task.json stays the pristine execute record, which is what keeps
    # the row re-gradeable below.
    summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert "checker exploded" in str(summary["task_results"])

    # And the row really is still re-gradeable.
    _invoke(["run", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])
    assert _row(_task_dir(run_dir))["final_status"] == FinalStatus.SUCCESS.value


def test_run_resume_reports_a_failing_verdict_and_exits_non_zero(tmp_path: Path) -> None:
    """The other resume gate: grading that SUCCEEDS but fails the criteria."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    # Remove the file the criteria read, so the grade legitimately fails.
    for proof in run_dir.glob("**/artifacts/**/proof.txt"):
        proof.unlink()

    result = runner.invoke(app, ["run", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])

    assert result.exit_code != 0
    assert _row(_task_dir(run_dir))["final_status"] == FinalStatus.FAILURE.value
    summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert summary["tasks_failed"] == 1
    assert summary["tasks_not_graded"] == 0


def test_evaluate_grades_the_directory_named_by_workspace(tmp_path: Path) -> None:
    """--workspace exists for a verifier that built its own /app; nothing else
    asserted it actually grades that directory rather than the run's artifacts."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "proof.txt").write_text("coder-eval-ran-without-a-coder", encoding="utf-8")
    # Make the run's own artifacts FAIL, so a pass can only come from --workspace.
    for proof in run_dir.glob("**/artifacts/**/proof.txt"):
        proof.unlink()

    _invoke(["evaluate", str(_task_dir(run_dir)), "--workspace", str(elsewhere)])

    assert _row(_task_dir(run_dir))["final_status"] == FinalStatus.SUCCESS.value


def test_evaluate_refuses_to_re_grade_a_run_that_errored(tmp_path: Path) -> None:
    """Grading may only move NOT_GRADED to a verdict. An ERROR / TIMEOUT run is
    an execution fact this pass neither repeated nor observed — laundering it
    into SUCCESS would report a crashed run as a pass.

    The exit code has to agree. It exited 0 under "All criteria passed! ✓" for a
    row `run.json` counts as failed, because the gate read the criteria tally
    rather than the outcome — so a CI wrapper shelling `coder-eval evaluate`
    went green on a timed-out run."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)
    row = _row(task_dir)
    row["final_status"] = FinalStatus.TIMEOUT.value
    (task_dir / "task.json").write_text(json.dumps(row), encoding="utf-8")

    result = _invoke(["evaluate", str(task_dir)], expect_exit=1)

    assert _row(task_dir)["final_status"] == FinalStatus.TIMEOUT.value
    assert "All criteria passed" not in result.output
    assert FinalStatus.TIMEOUT.value in result.output


def test_an_inherited_error_still_renders_its_criteria_and_keeps_the_status(tmp_path: Path) -> None:
    """The other arm of the same confusion. A PRESERVED ERROR is not a grading
    crash: the ERROR branch fired anyway, printed the ORIGINAL run's crash
    message as though grading had failed, claimed the row was "left ungraded"
    (it was not — the restored record still reads ERROR), and discarded a verdict
    it had just computed."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)
    row = _row(task_dir)
    row["final_status"] = FinalStatus.ERROR.value
    row["error_message"] = "agent crashed during the original run"
    (task_dir / "task.json").write_text(json.dumps(row), encoding="utf-8")

    result = _invoke(["evaluate", str(task_dir)], expect_exit=1)

    assert "Criteria Results" in result.output, "the computed verdict was thrown away"
    assert "left ungraded" not in result.output, "grading did not crash; saying so is false"
    assert _row(task_dir)["final_status"] == FinalStatus.ERROR.value


def test_grading_the_same_run_twice_reaches_the_same_verdict(tmp_path: Path) -> None:
    """Idempotence. A second grade must see the same workspace the first did —
    it catches both a pre_run that mutated the tree and a lost sandbox_path."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)

    _invoke(["evaluate", str(task_dir)])
    first = _row(task_dir)
    _invoke(["evaluate", str(task_dir)])
    second = _row(task_dir)

    assert second["final_status"] == first["final_status"]
    assert second["weighted_score"] == first["weighted_score"]
    assert second["sandbox_path"] == first["sandbox_path"], "the artifacts pointer must survive a re-grade"
    # The pre-grade record is still the ORIGINAL ungraded one, not the first grade's.
    assert _row(task_dir, "task.execute.json")["final_status"] == FinalStatus.NOT_GRADED.value


# --------------------------------------------------------------------------
# `execute` withholds the verdict, never the facts of the run
# --------------------------------------------------------------------------


def test_execute_records_max_turns_exhausted_exactly_as_run_does(tmp_path: Path) -> None:
    """`max_turns_exhausted` is a fact about the RUN, not a verdict.

    It used to be captured AFTER the grading switch's early return, so under
    `execute` it was never recorded at all: the row finalized NOT_GRADED and the
    command exited 0 where `run` reported MAX_TURNS_EXHAUSTED and exited 1 — for
    identical agent output. `_seed_from_prior_result` cannot restore a fact the
    execute phase never captured, so a later `evaluate` inherited the wrong
    terminal status too.
    """
    from coder_eval.streaming.collector import EventCollector

    # One turn that reports the cap was hit, on both paths.
    original = EventCollector.build_turn_record

    def _exhausted(self, *args: Any, **kwargs: Any):
        record = original(self, *args, **kwargs)
        record.max_turns_exhausted = True
        return record

    def _run(command: str, run_dir: Path) -> Any:
        with patch.object(EventCollector, "build_turn_record", _exhausted):
            return runner.invoke(app, [command, str(AGENTLESS_TASK), "--run-dir", str(run_dir)])

    graded_dir = tmp_path / "graded"
    _run("run", graded_dir)
    graded = _row(_task_dir(graded_dir))

    executed_dir = tmp_path / "executed"
    _run("execute", executed_dir)
    executed = _row(_task_dir(executed_dir))

    assert graded["max_turns_exhausted"] is True, "the fixture must actually exhaust turns under `run`"
    assert executed["max_turns_exhausted"] is True, (
        "`execute` dropped a fact about the run. Only the verdict is withheld."
    )
    # The FACT is recorded; the STATUS is not decided. `run` returns SUCCESS for
    # a max-turns trajectory whose criteria pass and only falls through to
    # MAX_TURNS_EXHAUSTED when they fail — so the status is not knowable without
    # grading, and claiming it here made it both terminal and permanent
    # (MAX_TURNS_EXHAUSTED is an execution fact, which the detached grade may
    # never overturn).
    assert executed["final_status"] == FinalStatus.NOT_GRADED.value

    # The parity that matters: grading the executed run must land exactly where
    # `run` did. Asserting only the executed half is what let the divergence ship.
    _invoke(["evaluate", str(_task_dir(executed_dir))])
    regraded = _row(_task_dir(executed_dir))

    assert regraded["final_status"] == graded["final_status"]
    assert regraded["weighted_score"] == graded["weighted_score"]
    assert regraded["max_turns_exhausted"] is True, "the fact must survive the grade too"


def test_a_detached_grade_keeps_the_runs_api_routing_not_the_graders(tmp_path: Path) -> None:
    """`_seed_from_prior_result`'s contract is that the PRIOR run wins on
    environment_info. The route recorder ran after the seeding and overwrote
    `api_routing` with the grading host's, leaving a self-contradictory record —
    a direct route named beside the run's stale bedrock fields."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)

    before = _row(task_dir)["environment_info"]
    before["api_routing"] = "a_backend_this_host_does_not_use"
    row = _row(task_dir)
    row["environment_info"] = before
    (task_dir / "task.json").write_text(json.dumps(row), encoding="utf-8")

    _invoke(["evaluate", str(task_dir)])
    after = _row(task_dir)["environment_info"]

    assert after["api_routing"] == "a_backend_this_host_does_not_use", (
        "the grade overwrote the RUN's recorded routing with the grading host's"
    )
    assert after.get("graded_by_api_routing"), "the grader's own route must still be recorded, just not in place"


def test_an_explicit_task_file_over_a_run_dir_grades_with_the_given_criteria(tmp_path: Path) -> None:
    """`evaluate <task.yaml> <run_dir>` — the third documented shape, and the
    one `evaluate`'s own help text calls the main reason to keep `execute` and
    `evaluate` separate at all ("iterate on criteria against a run you already
    paid for").

    It shipped with no behavioural test: `test_evaluate_target.py` asserts the
    pure resolver returns RUN_DIR with `task_file` set and never invokes the
    command, so nothing proved the SUPPLIED criteria actually win over the
    recorded ones, nor that the run's own trajectory is still the thing graded.
    """
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)
    executed = _row(task_dir)

    # An edited copy whose criterion names a file the run never produced, so a
    # verdict that follows the recorded config is distinguishable from one that
    # follows this file.
    edited = tmp_path / "edited.yaml"
    edited.write_text(
        AGENTLESS_TASK.read_text(encoding="utf-8").replace("path: proof.txt", "path: never_written.txt"),
        encoding="utf-8",
    )

    _invoke(["evaluate", str(edited), str(task_dir)], expect_exit=1)
    regraded = _row(task_dir)

    assert regraded["final_status"] == FinalStatus.FAILURE.value, (
        "the supplied task file must override the run's recorded criteria"
    )
    assert [c["score"] for c in regraded["success_criteria_results"]] == [0.0, 0.0]
    # The trajectory is still the RUN's. Grading with a different task file
    # changes what is asked of the run, never what the run did.
    assert regraded["iterations"] == executed["iterations"]
    assert regraded["duration_seconds"] == executed["duration_seconds"]


def test_a_re_grade_does_not_truncate_the_runs_own_task_log(tmp_path: Path) -> None:
    """`run --resume` grades into the row's OWN directory, and
    `task_log_handler` opens its file `mode="w"`. Pointing it at `task.log`
    replaced the agent trajectory log the run had already paid for with the
    grading pass's handful of lines — while `_apply_resume`'s own contract says
    "to_grade is deliberately NOT cleared: its artifacts are the run's output
    and the very thing being graded"."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_log = _task_dir(run_dir) / "task.log"
    sentinel = "AGENT RUN LOG — the trajectory this run paid for\n"
    task_log.write_text(sentinel, encoding="utf-8")

    _invoke(["run", str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--resume"])

    assert task_log.read_text(encoding="utf-8") == sentinel, "the re-grade truncated the agent's task.log"
    assert (_task_dir(run_dir) / "grade.log").is_file(), "the grading pass must log somewhere"


def test_a_copy_grade_leaves_the_runs_artifacts_pointer_alone(tmp_path: Path) -> None:
    """`--copy` grades in a tempdir, and `_setup` + `_cleanup` both wrote that
    tempdir into `sandbox_path` — which `_write_back` then persisted into the
    ORIGINAL task.json. The run then pointed at another run's artifacts, and the
    NEXT `evaluate <run_dir>` failed `default_workspace`'s containment guard: a
    re-grade that permanently broke re-grading."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)
    recorded = _row(task_dir)["sandbox_path"]

    # --copy re-runs the recorded provisioning + pre_run, which is exactly what
    # the trust gate covers, so the consent flag is part of this shape.
    _invoke(["evaluate", str(task_dir), "--copy", "--allow-recorded-commands"])
    assert _row(task_dir)["sandbox_path"] == recorded, "the grading copy's path replaced the run's"

    # The consequence, asserted directly rather than inferred from the field.
    _invoke(["evaluate", str(task_dir)])
    assert _row(task_dir)["final_status"] == FinalStatus.SUCCESS.value


# ---------------------------------------------------------------------------
# post_run belongs to the GRADING phase
#
# `post_run` is defined as running "after the evaluation verdict is finalized",
# and it may mutate the workspace. Under `execute` there is no verdict to come
# after, so running it there inverted its own contract AND broke the round-trip
# guarantee: the criteria had not read the tree yet, so `evaluate` graded a
# workspace `post_run` had already modified. `execute` now DEFERS it to whichever
# command grades.
# ---------------------------------------------------------------------------

# Deliberately destructive, and destructive of the exact file the criteria read.
# A `post_run` that mutates something no criterion observes (`rm -rf
# node_modules` on a pure-python task — the shape that shipped) cannot tell the
# two orderings apart, which is precisely why the defect survived: the in-tree
# tasks all got away with it.
_POST_RUN_TASK = """
task_id: post_run_phase_probe
description: "post_run deletes the file the criteria read, so ORDER decides the verdict."
tags: [smoke]
agent:
  type: none
sandbox:
  driver: tempdir
pre_run:
  - command: "printf ok > proof.txt"
post_run:
  - command: "rm -f proof.txt"
success_criteria:
  - type: file_exists
    path: proof.txt
    description: "Present iff the criteria ran BEFORE post_run."
"""


@pytest.fixture
def post_run_task(tmp_path: Path) -> Path:
    task_file = tmp_path / "post_run_phase_probe.yaml"
    task_file.write_text(_POST_RUN_TASK, encoding="utf-8")
    return task_file


def _post_run_commands(task_dir: Path) -> list[str]:
    """The recorded post_run commands.

    Never compared as a whole list: `experiments/default.yaml` appends its own
    `rm -rf node_modules .npm-prefix` (cleanup-last, per post_run's reverse
    append order), so pinning the full list would assert the layer merge rather
    than the phase these tests are about — and would break the day a baseline
    default changes.
    """
    return [r["command"] for r in _row(task_dir)["post_run_results"]]


def _proof(task_dir: Path) -> Path:
    artifacts = sorted((task_dir / "artifacts").glob("*"))
    assert len(artifacts) == 1, f"expected one preserved workspace, got {artifacts}"
    return artifacts[0] / "proof.txt"


def test_a_destructive_post_run_does_not_change_the_verdict_across_the_split(
    tmp_path: Path, post_run_task: Path
) -> None:
    """The round-trip guarantee, on the one task shape that can actually break it.

    `run` checks the criteria and THEN deletes proof.txt, so it passes. If
    `execute` runs post_run too, the file is gone before `evaluate` ever looks
    and the identical trajectory scores 0.00 — same agent output, opposite
    verdict.
    """
    direct = tmp_path / "direct"
    _invoke(["run", str(post_run_task), "--run-dir", str(direct)])
    expected = _row(_task_dir(direct))
    assert expected["final_status"] == FinalStatus.SUCCESS.value, "the fixture must pass under a single `run`"

    split = tmp_path / "split"
    _invoke(["execute", str(post_run_task), "--run-dir", str(split)])
    _invoke(["evaluate", str(_task_dir(split)), "--allow-recorded-commands"])
    actual = _row(_task_dir(split))

    assert actual["final_status"] == expected["final_status"]
    assert actual["weighted_score"] == expected["weighted_score"]


def test_execute_defers_post_run_instead_of_running_it(tmp_path: Path, post_run_task: Path) -> None:
    """Asserted on the side effect, not on a log line: the deletion must not have
    happened, so the workspace `evaluate` inherits is the one the agent left."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(post_run_task), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)

    assert _proof(task_dir).is_file(), "execute ran post_run and deleted the criteria's input"
    assert _post_run_commands(task_dir) == [], "a deferred command must not record a result"


def test_the_grading_pass_runs_post_run_and_records_it(tmp_path: Path, post_run_task: Path) -> None:
    """Deferred, not cancelled — the commands still run, one phase later."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(post_run_task), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)

    _invoke(["evaluate", str(task_dir), "--allow-recorded-commands"])

    assert not _proof(task_dir).exists(), "the grading pass never ran post_run"
    assert _post_run_commands(task_dir).count("rm -f proof.txt") == 1
    recorded = _row(task_dir)["post_run_results"]
    assert all(r["exit_code"] == 0 for r in recorded), recorded


def test_a_second_grade_does_not_run_post_run_again(tmp_path: Path, post_run_task: Path) -> None:
    """Nothing declares post_run idempotent, and the first grade already ran it.

    Re-running would repeat the side effects and double-count the records, since
    `_seed_from_prior_result` has already carried the first pass's results onto
    this row.
    """
    run_dir = tmp_path / "r"
    _invoke(["execute", str(post_run_task), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)
    _invoke(["evaluate", str(task_dir), "--allow-recorded-commands"])
    after_first = _post_run_commands(task_dir)

    # The re-grade now FAILS the criterion — post_run deleted its input on the
    # first pass. That is expected and is not what this test is about; what
    # matters is that the record does not grow.
    _invoke(["evaluate", str(task_dir), "--allow-recorded-commands"], expect_exit=1)
    assert _post_run_commands(task_dir) == after_first, "post_run ran twice"


def test_run_resume_grades_the_deferred_post_run_too(tmp_path: Path, post_run_task: Path) -> None:
    """`run --resume` is the other command that grades an executed row, so it
    owes the same deferred commands `evaluate` does."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(post_run_task), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)

    _invoke(["run", str(post_run_task), "--run-dir", str(run_dir), "--resume"])

    assert _row(task_dir)["final_status"] == FinalStatus.SUCCESS.value
    assert _post_run_commands(task_dir).count("rm -f proof.txt") == 1
    assert not _proof(task_dir).exists()


def test_an_in_place_grade_refuses_a_recorded_post_run_without_consent(tmp_path: Path, post_run_task: Path) -> None:
    """post_run now runs on the DEFAULT (in-place) path, so the trust gate has to
    cover it there. It was scanned only under `include_setup_phase`, which is
    False in place — which would have let a shared run directory run recorded
    shell on the grader's host with no prompt at all."""
    run_dir = tmp_path / "r"
    _invoke(["execute", str(post_run_task), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)

    result = _invoke(["evaluate", str(task_dir)], expect_exit=2)
    assert "rm -f proof.txt" in result.output, "the refusal must name the command it refused"
    assert _proof(task_dir).is_file(), "refused, yet the command ran anyway"


def test_the_baseline_post_run_alone_does_not_prompt(tmp_path: Path) -> None:
    """The whole point of the exemption, on the shape that is 100% of real runs.

    `experiments/default.yaml` appends `rm -rf node_modules .npm-prefix` to every
    task, so once post_run began running on the in-place path, scanning it
    naively made EVERY `evaluate <run_dir>` demand --allow-recorded-commands.
    A refusal that always fires is read as a formality and waved through, which
    is how the gate would have stopped protecting the authored commands that DO
    represent a choice by whoever wrote the run directory.

    The agentless task authors no post_run of its own, so the recorded list holds
    only the grader's own baseline — nothing the record chose.
    """
    run_dir = tmp_path / "r"
    _invoke(["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    task_dir = _task_dir(run_dir)

    # No --allow-recorded-commands, and no refusal.
    _invoke(["evaluate", str(task_dir)])

    assert _row(task_dir)["final_status"] == FinalStatus.SUCCESS.value
    assert "rm -rf node_modules .npm-prefix" in _post_run_commands(task_dir), (
        "exempt from the PROMPT is not exempt from RUNNING — the command still has to execute"
    )
