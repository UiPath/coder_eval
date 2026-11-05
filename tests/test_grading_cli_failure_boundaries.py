"""Failed grading preserves real verdicts and respects the output boundary."""

import importlib
import json

import pytest

from coder_eval.errors import GradingError
from coder_eval.errors.judge import JudgeInfrastructureError
from coder_eval.models import EvaluationResult, FinalStatus, SimulationTelemetry
from coder_eval.orchestration.regrade import write_grading_incident
from coder_eval.sandbox import Sandbox
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
        assert after["error_history"][-1]["message"] == "Synthetic grader failure"
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
def test_grader_message_markup_cannot_interrupt_incident_write(tmp_path, monkeypatch, cli, command):
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
    assert after["error_history"][-1]["message"] == message


@pytest.mark.parametrize("command", ["resume", "evaluate"])
def test_failed_incident_write_preserves_prior_verdict(tmp_path, monkeypatch, cli, command):
    path = task(tmp_path)
    run = tmp_path / "run"
    cli(["execute", str(path), "--run-dir", str(run)])
    task_json, before = saved(run)
    fail_one_grade(monkeypatch)

    def fail_write(*args, **kwargs):
        raise OSError("Synthetic incident write failure")

    monkeypatch.setattr("coder_eval.orchestration.regrade.write_text_atomic", fail_write)
    result = repair(cli, command, path, run, task_json)
    assert result.exit_code == 1
    assert "Could not record the grading incident" in result.output
    assert "incident recorded" not in result.output
    after = json.loads(task_json.read_text(encoding="utf-8"))
    summary = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert after == before
    assert summary["task_results"][0]["status"] == after["final_status"]
    assert summary["task_results"][0]["weighted_score"] == after["weighted_score"]


def test_incident_writer_preserves_its_prior_argument(tmp_path, cli):
    path = task(tmp_path)
    run = tmp_path / "run"
    cli(["execute", str(path), "--run-dir", str(run)])
    task_json, row = saved(run)
    prior = EvaluationResult.model_validate(row)
    before = prior.model_dump()
    error = GradingError(RuntimeError("New grading incident"), component="test.grading")
    selected = write_grading_incident(task_json.parent, prior, error)
    assert prior.model_dump() == before
    assert selected.error_history[-1].message == "New grading incident"
    assert selected.final_status == prior.final_status


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
                if status is FinalStatus.ERROR and phase == "grading":
                    expected = False
                assert result.has_execution_failure is expected, (status, phase, stop)


@pytest.mark.parametrize("initial", ["execute", "run"])
@pytest.mark.parametrize("same_output", [False, True])
def test_failed_evaluate_writes_only_prior_with_one_incident(tmp_path, monkeypatch, cli, initial, same_output):
    path = task(tmp_path)
    run = tmp_path / "run"
    assert cli([initial, str(path), "--run-dir", str(run)]).exit_code == 0
    target, before = saved(run)
    # This immutable first audit copy may legitimately predate a newer verdict.
    stale = dict(before, final_status="FAILURE", weighted_score=0.0)
    (target.parent / "task.execute.json").write_text(json.dumps(stale))
    protected = {
        key: before[key]
        for key in (
            "final_status",
            "weighted_score",
            "success_criteria_results",
            "iterations",
            "error_message",
            "error_details",
            "error_log_tail",
            "error_phase",
            "started_at",
            "duration_seconds",
            "sandbox_path",
            "iteration_count",
        )
    }
    writes = []
    for module_name in (
        "coder_eval.orchestrator",
        "coder_eval.orchestration.regrade",
        "coder_eval.cli.evaluate_command",
        "coder_eval.cli.run_command",
    ):
        module = importlib.import_module(module_name)
        original = getattr(module, "write_text_atomic", None)
        if original is None:
            continue

        def write(path_arg, text_arg, *args, _original=original, **kwargs):
            if path_arg == target:
                writes.append(json.loads(text_arg))
            return _original(path_arg, text_arg, *args, **kwargs)

        monkeypatch.setattr(module, "write_text_atomic", write)
    fail_one_grade(monkeypatch, JudgeInfrastructureError("[/synthetic_bad_closing_tag]"))
    result = cli(
        ["evaluate", str(target.parent), "--run-dir", str(target.parent if same_output else tmp_path / "grade")]
    )
    assert result.exit_code == 1
    assert "MarkupError" not in result.output
    after = json.loads(target.read_text())
    assert len(writes) == 1, "A failed regrade must not write an intermediate failed verdict"
    assert {key: after[key] for key in protected} == protected
    assert {key: writes[0][key] for key in protected} == protected
    assert len(after["error_history"]) == len(before.get("error_history", [])) + 1
    assert after["error_history"][-1]["message"] == "[/synthetic_bad_closing_tag]"
    assert "grading_error" not in after
    assert json.loads((target.parent / "task.execute.json").read_text()) == stale


def test_copy_setup_fault_preserves_prior_and_records_one_incident(tmp_path, monkeypatch, cli):
    path = task(tmp_path)
    run = tmp_path / "run"
    assert cli(["run", str(path), "--run-dir", str(run)]).exit_code == 0
    target, before = saved(run)
    writes = []
    for name in ("coder_eval.orchestrator", "coder_eval.orchestration.regrade", "coder_eval.cli.evaluate_command"):
        module = importlib.import_module(name)
        original = getattr(module, "write_text_atomic", None)
        if original is None:
            continue

        def write(path_arg, text_arg, *args, _original=original, **kwargs):
            if path_arg == target:
                writes.append(json.loads(text_arg))
            return _original(path_arg, text_arg, *args, **kwargs)

        monkeypatch.setattr(module, "write_text_atomic", write)

    def failed_setup(self):
        raise RuntimeError("Intentional copy-grading setup failure")

    monkeypatch.setattr(Sandbox, "setup", failed_setup)
    outcome = cli(["evaluate", str(target.parent), "--copy", "--run-dir", str(tmp_path / "grade")])
    assert outcome.exit_code == 1
    after = json.loads(target.read_text())
    assert len(writes) == 1
    assert {k: v for k, v in after.items() if k != "error_history"} == {
        k: v for k, v in before.items() if k != "error_history"
    }
    assert after["error_history"][:-1] == before["error_history"]
    assert after["error_history"][-1]["message"] == "Intentional copy-grading setup failure"
