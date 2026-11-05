"""Failed grading preserves real verdicts and respects the output boundary."""

import importlib
import json

import pytest

from coder_eval.errors.judge import JudgeInfrastructureError
from coder_eval.models import EvaluationResult, FinalStatus, SimulationTelemetry
from coder_eval.orchestration.regrade import restore_pre_grade_record
from tests.test_evaluate_format_harbor import _TASK_YAML, _write_trajectory
from tests.test_grading_execution_completion import cli as cli
from tests.test_grading_execution_completion import fail_one_grade, repair, saved, task


@pytest.mark.parametrize("command", ["resume", "evaluate"])
@pytest.mark.parametrize("measured", [False, True])
def test_failed_grade_preserves_execution_fact_and_measurement(tmp_path, monkeypatch, cli, command, measured):
    path = task(tmp_path)
    run = tmp_path / "run"
    assert cli(["run" if measured else "execute", str(path), "--run-dir", str(run)]).exit_code == 0
    task_json, before = saved(run)
    before.update(
        final_status="TIMEOUT",
        error_phase="execution",
        error_message="Original timeout",
        error_details={"synthetic_execution_origin": True},
        error_log_tail="Original execution log",
    )
    task_json.write_text(json.dumps(before), encoding="utf-8")
    faults = fail_one_grade(monkeypatch)
    repair(cli, command, path, run, task_json)
    after = json.loads(task_json.read_text(encoding="utf-8"))
    for key in (
        "final_status",
        "error_phase",
        "error_message",
        "error_details",
        "error_log_tail",
        "iterations",
        "weighted_score",
        "success_criteria_results",
    ):
        assert after[key] == before[key]
    if measured and command == "resume":
        assert not faults
    else:
        assert len(faults) == 1
        assert after["grading_error"]["message"] == "Synthetic grader failure"
    summary = json.loads((run / "run.json").read_text(encoding="utf-8"))
    projected = summary["task_results"][0]
    assert projected["status"] == after["final_status"]
    assert projected["weighted_score"] == after["weighted_score"]
    assert projected["error_message"] == after["error_message"]


def test_harbor_grading_failure_cannot_write_to_workdir(tmp_path, monkeypatch, cli):
    path = tmp_path / "task.yaml"
    path.write_text(_TASK_YAML, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "test.txt").write_text("Synthetic artifact", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in work.iterdir()}
    trajectory = _write_trajectory(tmp_path)
    fail_one_grade(monkeypatch)
    result = cli(
        [
            "evaluate",
            str(path),
            str(work),
            "--format",
            "harbor",
            "--trajectory",
            str(trajectory),
            "--run-dir",
            str(tmp_path / "grade"),
        ]
    )
    assert result.exit_code == 1
    assert {p.name: p.read_bytes() for p in work.iterdir()} == before
    assert "restored" not in result.output.lower()
    assert "provenance" not in result.output.lower()


@pytest.mark.parametrize("command", ["resume", "evaluate"])
def test_grader_message_markup_cannot_interrupt_restoration(tmp_path, monkeypatch, cli, command):
    path = task(tmp_path)
    run = tmp_path / "run"
    cli(["execute", str(path), "--run-dir", str(run)])
    task_json, _ = saved(run)
    message = "[/synthetic_bad_closing_tag]"
    fail_one_grade(monkeypatch, JudgeInfrastructureError(message))
    result = repair(cli, command, path, run, task_json)
    assert result.exit_code == 1
    assert type(result.exception).__name__ != "MarkupError"
    assert "MarkupError" not in result.output
    after = json.loads(task_json.read_text(encoding="utf-8"))
    assert after["final_status"] == "NOT_GRADED"
    assert after["weighted_score"] is None
    assert after["grading_error"]["message"] == message


@pytest.mark.parametrize("command", ["resume", "evaluate"])
def test_failed_restore_does_not_fold_or_announce_prior_verdict(tmp_path, monkeypatch, cli, command):
    path = task(tmp_path)
    run = tmp_path / "run"
    cli(["execute", str(path), "--run-dir", str(run)])
    task_json, before = saved(run)
    fail_one_grade(monkeypatch)
    monkeypatch.setattr("coder_eval.orchestration.regrade.restore_pre_grade_record", lambda *a, **k: False)
    evaluate_module = importlib.import_module("coder_eval.cli.evaluate_command")
    monkeypatch.setattr(evaluate_module, "restore_pre_grade_record", lambda *a, **k: False)
    result = repair(cli, command, path, run, task_json)
    assert result.exit_code == 1
    assert "Could not restore" in result.output
    assert "prior verdict restored" not in result.output
    after = json.loads(task_json.read_text(encoding="utf-8"))
    summary = json.loads((run / "run.json").read_text(encoding="utf-8"))
    if command == "resume":
        assert after["final_status"] == "ERROR"
    else:
        assert after == before
    assert summary["task_results"][0]["status"] == after["final_status"]
    assert summary["task_results"][0]["weighted_score"] == after["weighted_score"]


def test_restore_helper_intentional_noop_remains_false(tmp_path, cli):
    path = task(tmp_path)
    run = tmp_path / "run"
    cli(["execute", str(path), "--run-dir", str(run)])
    task_json, row = saved(run)
    prior = EvaluationResult.model_validate(row)
    task_json.write_text(prior.model_dump_json(indent=2), encoding="utf-8")
    before = task_json.read_bytes()
    assert restore_pre_grade_record(task_json.parent, prior_result=prior) is False
    assert task_json.read_bytes() == before


def test_all_status_phase_and_dialog_combinations_keep_execution_facts():
    facts = {"ERROR", "TIMEOUT", "BUILD_FAILED", "TOKEN_BUDGET_EXCEEDED", "COST_BUDGET_EXCEEDED"}
    for status in FinalStatus:
        for phase in (None, "execution", "grading"):
            for stop in (None, "max_turns", "error"):
                result = EvaluationResult(
                    task_id="synthetic",
                    task_description="Classifier table",
                    variant_id="fixture",
                    agent_type="none",
                    started_at="2026-01-01T00:00:00Z",
                    iteration_count=1,
                    final_status=status,
                    error_phase=phase,
                    simulation=None
                    if stop is None
                    else SimulationTelemetry(n_trials=1, replicate_index=0, total_turns=1, stop_reason=stop),
                )
                expected = status.value in facts
                if status is FinalStatus.ERROR and phase == "grading" and stop != "error":
                    expected = False
                assert result.has_execution_failure is expected, (status, phase, stop)
