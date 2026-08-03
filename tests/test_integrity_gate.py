"""Tests for the integrity gate in Orchestrator._apply_integrity_gate.

The gate is the only place a verdict changes an outcome, so these pin the exact
status transitions -- especially the ones that must NOT happen: INCONCLUSIVE
never voids, `detect` never voids, and a non-SUCCESS row is never rewritten.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from coder_eval.models import (
    AgentKind,
    CommandTelemetry,
    EvaluationResult,
    FinalStatus,
    IntegrityMode,
    IntegrityVerdict,
    TaskDefinition,
    TurnRecord,
)
from coder_eval.orchestrator import Orchestrator


def _task() -> TaskDefinition:
    return TaskDefinition(
        task_id="leaky",
        description="d",
        initial_prompt="p",
        success_criteria=[{"type": "file_exists", "description": "x", "path": "out.txt"}],
    )


def _bash(command: str) -> CommandTelemetry:
    return CommandTelemetry(
        tool_name="Bash",
        tool_id="t0",
        timestamp=datetime.now(),
        parameters={"command": command},
        result_status="success",
    )


def _orchestrator(tmp_path, *, commands: list[CommandTelemetry], status: FinalStatus) -> Orchestrator:
    orch = Orchestrator(task=_task(), run_dir=tmp_path / "run", variant_id="t")
    orch.result = EvaluationResult(
        task_id="leaky",
        task_description="d",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=status,
        iteration_count=1,
        weighted_score=1.0,
        iterations=[TurnRecord(iteration=1, user_input="p", agent_output="a", commands=commands)],
    )
    return orch


_LEAK = "cat RESOLUTION.md"
_CLEAN = "ls -la"


def test_void_mode_downgrades_a_tainted_pass(tmp_path, monkeypatch):
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.VOID)
    orch = _orchestrator(tmp_path, commands=[_bash(_LEAK)], status=FinalStatus.SUCCESS)

    orch._apply_integrity_gate()

    assert orch.result.final_status is FinalStatus.FAILURE
    assert orch.result.integrity.verdict is IntegrityVerdict.TAINTED
    assert orch.result.integrity.voided is True
    assert orch.result.error_message is not None
    assert "voided" in orch.result.error_message.lower()


def test_voiding_preserves_the_weighted_score(tmp_path, monkeypatch):
    """The score is the diagnostic: the row passed BECAUSE it cheated."""
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.VOID)
    orch = _orchestrator(tmp_path, commands=[_bash(_LEAK)], status=FinalStatus.SUCCESS)

    orch._apply_integrity_gate()

    assert orch.result.weighted_score == 1.0


def test_detect_mode_records_but_never_voids(tmp_path, monkeypatch):
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.DETECT)
    orch = _orchestrator(tmp_path, commands=[_bash(_LEAK)], status=FinalStatus.SUCCESS)

    orch._apply_integrity_gate()

    assert orch.result.final_status is FinalStatus.SUCCESS
    assert orch.result.integrity.verdict is IntegrityVerdict.TAINTED
    assert orch.result.integrity.voided is False
    assert orch.result.error_message is None


def test_off_mode_skips_entirely(tmp_path, monkeypatch):
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.OFF)
    orch = _orchestrator(tmp_path, commands=[_bash(_LEAK)], status=FinalStatus.SUCCESS)

    orch._apply_integrity_gate()

    assert orch.result.final_status is FinalStatus.SUCCESS
    assert orch.result.integrity.verdict is IntegrityVerdict.SKIPPED
    assert orch.result.integrity.findings == []


def test_clean_run_is_recorded_clean_and_untouched(tmp_path, monkeypatch):
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.VOID)
    orch = _orchestrator(tmp_path, commands=[_bash(_CLEAN)], status=FinalStatus.SUCCESS)

    orch._apply_integrity_gate()

    assert orch.result.final_status is FinalStatus.SUCCESS
    assert orch.result.integrity.verdict is IntegrityVerdict.CLEAN
    assert orch.result.integrity.voided is False


def test_inconclusive_never_voids(tmp_path, monkeypatch):
    """A partially-blind scan is not evidence of a leak."""
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.VOID)
    orch = _orchestrator(tmp_path, commands=[_bash(_CLEAN)], status=FinalStatus.SUCCESS)
    orch.result.iterations[0].unrecovered_subagent_threads = 1

    orch._apply_integrity_gate()

    assert orch.result.integrity.verdict is IntegrityVerdict.INCONCLUSIVE
    assert orch.result.final_status is FinalStatus.SUCCESS
    assert orch.result.integrity.voided is False


@pytest.mark.parametrize(
    "status",
    [
        FinalStatus.FAILURE,
        FinalStatus.ERROR,
        FinalStatus.TIMEOUT,
        FinalStatus.MAX_TURNS_EXHAUSTED,
        FinalStatus.TOKEN_BUDGET_EXCEEDED,
        FinalStatus.COST_BUDGET_EXCEEDED,
        FinalStatus.BUILD_FAILED,
    ],
)
def test_non_success_statuses_are_never_rewritten(tmp_path, monkeypatch, status: FinalStatus):
    """Only a PASS can be voided; a row that already failed keeps its own reason."""
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.VOID)
    orch = _orchestrator(tmp_path, commands=[_bash(_LEAK)], status=status)

    orch._apply_integrity_gate()

    assert orch.result.final_status is status
    assert orch.result.integrity.verdict is IntegrityVerdict.TAINTED
    assert orch.result.integrity.voided is False


def test_existing_error_message_is_not_overwritten(tmp_path, monkeypatch):
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.VOID)
    orch = _orchestrator(tmp_path, commands=[_bash(_LEAK)], status=FinalStatus.SUCCESS)
    orch.result.error_message = "an earlier, more specific reason"

    orch._apply_integrity_gate()

    assert orch.result.error_message == "an earlier, more specific reason"


def test_gate_survives_an_integrity_failure(tmp_path, monkeypatch):
    """An integrity bug must not cost the row its task.json."""
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.VOID)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("integrity exploded")

    monkeypatch.setattr(orch_mod, "evaluate_integrity", _boom)
    orch = _orchestrator(tmp_path, commands=[_bash(_LEAK)], status=FinalStatus.SUCCESS)

    orch._apply_integrity_gate()  # must not raise

    assert orch.result.final_status is FinalStatus.SUCCESS
    assert orch.result.integrity.verdict is IntegrityVerdict.SKIPPED


def test_gate_is_a_no_op_without_a_result(tmp_path):
    orch = Orchestrator(task=_task(), run_dir=tmp_path / "run", variant_id="t")
    orch._apply_integrity_gate()  # must not raise
    assert orch.result is None


def test_task_file_widens_the_spec_to_the_task_yaml(tmp_path, monkeypatch):
    """With the task YAML known, reading it is a leak; without it, the glob still catches the name."""
    from coder_eval import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod.settings, "integrity_mode", IntegrityMode.VOID)
    task_file = tmp_path / "scenario" / "task.yaml"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("task_id: leaky\n", encoding="utf-8")

    orch = _orchestrator(tmp_path, commands=[_bash(f"cat {task_file.as_posix()}")], status=FinalStatus.SUCCESS)
    orch.task_file = task_file

    orch._apply_integrity_gate()

    assert orch.result.integrity.verdict is IntegrityVerdict.TAINTED
    assert orch.result.final_status is FinalStatus.FAILURE
