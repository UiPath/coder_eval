"""Tests for trajectory.json persistence (orchestrator) — ATIF emit, Phase 3."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from coder_eval.harbor import Trajectory
from coder_eval.models import (
    AgentKind,
    AssistantMessage,
    ContentBlock,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    PreservationMode,
    SandboxConfig,
    TaskDefinition,
    TurnRecord,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator


FIXTURES_DIR = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)


def _turn_with_generation() -> TurnRecord:
    return TurnRecord(
        iteration=1,
        user_input="do the task",
        agent_output="done",
        messages=[
            AssistantMessage(
                started_at=T0,
                completed_at=T0,
                generation_duration_ms=100.0,
                content_blocks=[ContentBlock(block_type="text", sequence=0, text="done")],
                input_tokens=10,
                output_tokens=5,
                model="claude-haiku-4-5",
            )
        ],
    )


def _bootstrap_orchestrator(tmp_path: Path, *, iterations: list[TurnRecord]) -> Orchestrator:
    """Orchestrator primed to run _finalize_result without running the loop.

    Mirrors tests/test_orchestrator.py::_bootstrap_finalize_orchestrator.
    """
    task = TaskDefinition(
        task_id="atif_persist_task",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(),
        success_criteria=[FileExistsCriterion(description="x", path="x.py")],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    orchestrator = Orchestrator(task, run_dir, preservation_mode=PreservationMode.NONE, variant_id="v1")
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="v1",
        agent_type=task.agent.type,
        started_at=T0,
        final_status=FinalStatus.SUCCESS,
        iteration_count=len(iterations),
        iterations=iterations,
        success_criteria_results=[
            CriterionResult(criterion_type="file_exists", description="x", score=1.0, pass_threshold=0.9)
        ],
        weighted_score=1.0,
        environment_info={},
    )
    orchestrator.agent = None
    return orchestrator


def _finalize(orchestrator: Orchestrator) -> None:
    # HTML rendering is out of scope for these tests — patch it to a no-op,
    # matching the existing _finalize_result test pattern.
    with patch("coder_eval.reports_html.write_task_html", return_value=None):
        orchestrator._finalize_result(start_time=0.0)


class TestFinalizeWritesTrajectory:
    def test_trajectory_written_alongside_task_json(self, tmp_path):
        orch = _bootstrap_orchestrator(tmp_path, iterations=[_turn_with_generation()])
        _finalize(orch)

        # task.json is intact (parseable), not merely present.
        persisted = EvaluationResult.model_validate_json(orch.report_path.read_text(encoding="utf-8"))
        assert persisted.final_status == FinalStatus.SUCCESS
        assert orch.trajectory_path.exists()
        t = Trajectory.model_validate(json.loads(orch.trajectory_path.read_text(encoding="utf-8")))
        assert t.session_id == "atif_persist_task/v1"
        assert [s.source for s in t.steps] == ["user", "agent"]

    def test_zero_turn_result_writes_no_trajectory(self, tmp_path):
        orch = _bootstrap_orchestrator(tmp_path, iterations=[])
        _finalize(orch)

        assert orch.report_path.exists()
        assert not orch.trajectory_path.exists()

    def test_trajectory_failure_never_blocks_task_json(self, tmp_path):
        orch = _bootstrap_orchestrator(tmp_path, iterations=[_turn_with_generation()])
        with patch("coder_eval.harbor.write_trajectory_json", side_effect=RuntimeError("boom")):
            _finalize(orch)  # must not raise

        # The interrupt-proof contract promises an INTACT task.json, not just
        # an existing file — validate it round-trips.
        persisted = EvaluationResult.model_validate_json(orch.report_path.read_text(encoding="utf-8"))
        assert persisted.final_status == FinalStatus.SUCCESS
        assert not orch.trajectory_path.exists()


class TestReportAtifBackfill:
    """`coder-eval report --format atif` regenerates trajectory.json from task.json."""

    @staticmethod
    def _write_task_json(run_dir: Path, task_id: str, *, iterations: list[TurnRecord]) -> Path:
        result = EvaluationResult(
            task_id=task_id,
            task_description="backfill test",
            variant_id="default",
            agent_type="claude-code",
            started_at=T0,
            final_status=FinalStatus.SUCCESS,
            iteration_count=len(iterations),
            iterations=iterations,
        )
        task_dir = run_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        task_json = task_dir / "task.json"
        task_json.write_text(result.model_dump_json(), encoding="utf-8")
        return task_json

    def test_backfill_writes_valid_trajectory(self, tmp_path):
        from typer.testing import CliRunner

        from coder_eval.cli import app

        run_dir = tmp_path / "run"
        self._write_task_json(run_dir, "task-a", iterations=[_turn_with_generation()])

        res = CliRunner().invoke(app, ["report", str(run_dir), "--format", "atif"])
        assert res.exit_code == 0
        trajectory = run_dir / "task-a" / "trajectory.json"
        t = Trajectory.model_validate(json.loads(trajectory.read_text(encoding="utf-8")))
        assert t.session_id == "task-a/default"
        assert "Generated 1 trajectory(s)" in res.stdout

    def test_corrupt_task_json_skipped_good_one_processed(self, tmp_path):
        from typer.testing import CliRunner

        from coder_eval.cli import app

        run_dir = tmp_path / "run"
        self._write_task_json(run_dir, "task-good", iterations=[_turn_with_generation()])
        bad_dir = run_dir / "task-bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "task.json").write_text("{ not valid json", encoding="utf-8")

        res = CliRunner().invoke(app, ["report", str(run_dir), "--format", "atif"])
        assert (run_dir / "task-good" / "trajectory.json").exists()
        assert not (bad_dir / "trajectory.json").exists()
        assert "Skipping" in res.stdout
        assert res.exit_code == 1  # failed files are reported, mirroring the html contract

    def test_zero_turn_task_json_skipped_not_failed(self, tmp_path):
        from typer.testing import CliRunner

        from coder_eval.cli import app

        run_dir = tmp_path / "run"
        self._write_task_json(run_dir, "task-empty", iterations=[])

        res = CliRunner().invoke(app, ["report", str(run_dir), "--format", "atif"])
        assert res.exit_code == 0  # a zero-step result is a legitimate skip, not a failure
        assert not (run_dir / "task-empty" / "trajectory.json").exists()
        assert "Skipped" in res.stdout

    def test_converter_failure_is_a_failure_not_a_skip(self, tmp_path):
        """A converter bug must surface as a FAILURE (exit 1), not be silently
        collapsed into the legitimate zero-step skip — an explicit backfill run
        must distinguish 'nothing to emit' from 'emission failed'."""
        from typer.testing import CliRunner

        from coder_eval.cli import app

        run_dir = tmp_path / "run"
        self._write_task_json(run_dir, "task-a", iterations=[_turn_with_generation()])

        with patch(
            "coder_eval.harbor.atif_emit.evaluation_result_to_trajectory",
            side_effect=RuntimeError("converter bug"),
        ):
            res = CliRunner().invoke(app, ["report", str(run_dir), "--format", "atif"])
        assert res.exit_code == 1
        assert "Failed" in res.stdout
        assert not (run_dir / "task-a" / "trajectory.json").exists()

    def test_output_flag_rejected_for_atif(self, tmp_path):
        from typer.testing import CliRunner

        from coder_eval.cli import app

        run_dir = tmp_path / "run"
        self._write_task_json(run_dir, "task-a", iterations=[_turn_with_generation()])

        res = CliRunner().invoke(app, ["report", str(run_dir), "--format", "atif", "--output", str(tmp_path / "x")])
        assert res.exit_code == 1
        assert "--output is not supported with --format atif" in res.stdout


class TestEvaluateOnlyPath:
    def test_evaluate_run_produces_no_trajectory(self, tmp_path):
        """End-to-end through the evaluate-only orchestrator path: zero turns →
        task.json lands, trajectory.json does not."""
        task_file = FIXTURES_DIR / "tasks" / "test_task_pass.yaml"
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        (work_dir / "app.py").write_text("print('hello')")
        run_dir = tmp_path / "eval_run"
        run_dir.mkdir()

        from coder_eval.cli.evaluate_command import evaluate_command

        with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
            with pytest.raises(typer.Exit) as exc_info:
                evaluate_command(task_file=task_file, work_dir=work_dir, run_dir=run_dir)
            assert exc_info.value.exit_code == 0

        assert (run_dir / "task.json").exists()
        assert not (run_dir / "trajectory.json").exists()
