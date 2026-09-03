"""`coder-eval execute` — run without grading.

Three layers, deliberately:

* **End-to-end** against the agentless task (`agent: {type: none}`), which needs
  no API key and is fully deterministic. This is the only layer that proves the
  whole chain — CLI → batch → Orchestrator → task.json → run.json — actually
  withholds the verdict while still executing.
* **Contrast** — the same task under `run` must still produce SUCCESS with a real
  score. Without it, a totally broken `execute` (or a broken fixture) would pass
  the assertions above by accident.
* **Wiring** — the two commands share one body, so a signature or `grade` drift
  is caught mechanically rather than by a human noticing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.models import FinalStatus, RunSummary


runner = CliRunner()

# Rich styles each `--option` token in help text, and it splits the token across
# several style spans (`--junit-xml` renders as `-` + `-junit` + `-xml`, each with
# its own escape sequence). Styling is ON whenever rich thinks it is writing to a
# terminal — which includes GitHub Actions, so a bare substring check over
# `result.output` passes locally and fails only in CI. Same helper, same reason,
# as tests/test_cli_type_flag.py.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# The agentless smoke task: no agent, no model call, and a pre_run that writes a
# file its criteria read back. Executing it must still write that file (proving
# the run really happened) while scoring nothing.
AGENTLESS_TASK = Path("tasks/agentless_smoke_test.yaml")


def _invoke(command: str, run_dir: Path) -> Any:
    return runner.invoke(
        app,
        [command, str(AGENTLESS_TASK), "--run-dir", str(run_dir), "--preservation-mode", "MOVE_ON_WRITE"],
    )


def _task_json(run_dir: Path) -> dict[str, Any]:
    matches = sorted(run_dir.glob("**/task.json"))
    assert len(matches) == 1, f"expected exactly one task.json under {run_dir}, got {matches}"
    return json.loads(matches[0].read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The status itself
# --------------------------------------------------------------------------


def test_not_graded_is_its_own_category() -> None:
    """NOT_GRADED must not fold into succeeded/failed/error — each would lie."""
    assert FinalStatus.NOT_GRADED.category == "ungraded"
    assert FinalStatus.NOT_GRADED.icon == "?"


def test_ungraded_leaves_both_sides_of_the_pass_rate() -> None:
    """An all-ungraded run has NO pass rate — not a 0% one."""
    summary = RunSummary(
        run_id="r",
        start_time="2026-01-01T00:00:00",  # type: ignore[arg-type]
        end_time="2026-01-01T00:01:00",  # type: ignore[arg-type]
        total_duration_seconds=60.0,
        tasks_run=2,
        tasks_succeeded=0,
        tasks_failed=0,
        tasks_error=0,
        tasks_not_graded=2,
        task_results=[],
        framework_version="test",
    )
    assert summary.tasks_graded == 0
    assert summary.pass_rate is None
    assert summary.error_share is None


def test_ungraded_does_not_dilute_a_partially_graded_run() -> None:
    """One pass out of one graded task is 100%, even alongside three ungraded ones."""
    summary = RunSummary(
        run_id="r",
        start_time="2026-01-01T00:00:00",  # type: ignore[arg-type]
        end_time="2026-01-01T00:01:00",  # type: ignore[arg-type]
        total_duration_seconds=60.0,
        tasks_run=4,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        tasks_not_graded=3,
        task_results=[],
        framework_version="test",
    )
    assert summary.pass_rate == 1.0


def test_task_count_invariant_counts_the_ungraded_bucket() -> None:
    """The fourth bucket is part of the invariant, not a free-floating sub-counter."""
    with pytest.raises(ValueError, match="Task count invariant violated"):
        RunSummary(
            run_id="r",
            start_time="2026-01-01T00:00:00",  # type: ignore[arg-type]
            end_time="2026-01-01T00:01:00",  # type: ignore[arg-type]
            total_duration_seconds=1.0,
            tasks_run=2,
            tasks_succeeded=0,
            tasks_failed=0,
            tasks_error=0,
            tasks_not_graded=1,  # 0+0+0+1 != 2
            task_results=[],
            framework_version="test",
        )


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@pytest.mark.skipif(not AGENTLESS_TASK.is_file(), reason="needs a source checkout (tasks/ is not in the wheel)")
def test_execute_runs_the_task_but_grades_nothing(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    result = _invoke("execute", run_dir)

    assert result.exit_code == 0, result.output

    row = _task_json(run_dir)
    # The verdict is withheld ...
    assert row["final_status"] == FinalStatus.NOT_GRADED.value
    assert row["weighted_score"] is None, "must be None, never 0.0 — 0.0 reads as 'graded and scored zero'"
    assert row["success_criteria_results"] == []
    # ... but the run itself demonstrably happened: pre_run wrote its file into
    # the preserved sandbox. Without this the test would also pass if `execute`
    # had simply skipped the task.
    proof = sorted(run_dir.glob("**/artifacts/**/proof.txt"))
    assert proof, f"pre_run did not run — no proof.txt under {run_dir}"
    assert "coder-eval-ran-without-a-coder" in proof[0].read_text(encoding="utf-8")


@pytest.mark.skipif(not AGENTLESS_TASK.is_file(), reason="needs a source checkout (tasks/ is not in the wheel)")
def test_execute_run_json_reports_ungraded_not_failed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    assert _invoke("execute", run_dir).exit_code == 0

    summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert summary["tasks_run"] == 1
    assert summary["tasks_not_graded"] == 1
    # The whole point: an ungraded task is NOT a failure and NOT an error.
    assert summary["tasks_failed"] == 0
    assert summary["tasks_error"] == 0
    assert summary["tasks_succeeded"] == 0
    assert summary["pass_rate"] is None


@pytest.mark.skipif(not AGENTLESS_TASK.is_file(), reason="needs a source checkout (tasks/ is not in the wheel)")
def test_run_still_grades_the_same_task(tmp_path: Path) -> None:
    """The negative control: `run` must still score this task, or the assertions
    above prove nothing about grading being *deliberately* skipped."""
    run_dir = tmp_path / "run"
    result = _invoke("run", run_dir)

    assert result.exit_code == 0, result.output
    row = _task_json(run_dir)
    assert row["final_status"] == FinalStatus.SUCCESS.value
    assert row["weighted_score"] == 1.0
    assert len(row["success_criteria_results"]) == 2

    summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert summary["tasks_not_graded"] == 0
    assert summary["tasks_succeeded"] == 1
    assert summary["pass_rate"] == 1.0


# --------------------------------------------------------------------------
# Wiring: one shared body, two commands
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "module", "expected_grade"),
    [("run", "run_command", True), ("execute", "execute_command", False)],
)
def test_both_commands_call_the_shared_pipeline(command: str, module: str, expected_grade: bool) -> None:
    """`run` and `execute` differ ONLY in `grade` — no third code path.

    Patched per module because each command imported ``run_pipeline`` into its own
    namespace; patching the defining module would silently miss ``execute``.
    """
    with patch(f"coder_eval.cli.{module}.run_pipeline") as pipeline:
        result = runner.invoke(app, [command, "a.yaml"])
    assert result.exit_code == 0, result.output
    pipeline.assert_called_once()
    assert pipeline.call_args.kwargs["grade"] is expected_grade


def _option_names(command: str) -> set[str]:
    import typer.main

    click_app = typer.main.get_command(app)
    cmd = click_app.commands[command]  # type: ignore[attr-defined]
    return {opt for param in cmd.params for opt in getattr(param, "opts", [])}


# `execute` restates `run`'s Typer signature because Typer builds its parser from
# the signature and there is no way to share one. That duplication is the drift
# risk this test exists to close: a flag added to `run` must be added here too,
# or consciously listed below as a deliberate omission.
_DELIBERATELY_ABSENT_FROM_EXECUTE = {
    "--junit-xml",  # a report of verdicts, and there are none
}


def test_execute_exposes_run_flags_minus_the_refused_one() -> None:
    run_opts = _option_names("run")
    execute_opts = _option_names("execute")

    missing = run_opts - execute_opts - _DELIBERATELY_ABSENT_FROM_EXECUTE
    assert not missing, (
        f"`run` has flag(s) {sorted(missing)} that `execute` lacks. Add them to "
        "execute_command's signature, or list them in _DELIBERATELY_ABSENT_FROM_EXECUTE "
        "with the reason they are refused."
    )
    assert not execute_opts - run_opts, "`execute` must not grow flags of its own"
    # The omissions must be real, not stale entries masking a genuine gap.
    assert not _DELIBERATELY_ABSENT_FROM_EXECUTE & execute_opts


# --------------------------------------------------------------------------
# The docker boundary
# --------------------------------------------------------------------------


async def _staged_context(tmp_path: Path, *, grade: bool) -> dict[str, Any]:
    """Stage a docker task's inputs and read back the context.json the container sees."""
    from coder_eval.isolation.docker_runner import DockerRunner
    from coder_eval.models import ResolvedTask, TaskDefinition

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent={"type": "claude-code"},
        sandbox={"driver": "docker"},
        success_criteria=[{"type": "file_exists", "path": "x.txt", "description": "x"}],
    )
    rt = ResolvedTask(
        task=task,
        task_file=tmp_path / "t.yaml",
        run_dir=tmp_path / "run",
        variant_id="default",
        original_task_id="t",
    )
    staged = tmp_path / "input"
    staged.mkdir()
    await DockerRunner(rt, grade=grade)._stage_inputs(staged)
    return json.loads((staged / "context.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("grade", [True, False])
async def test_docker_forwards_grade_to_the_container(tmp_path: Path, grade: bool) -> None:
    """`grade` is a run-level CLI decision, so it is NOT recoverable from the staged
    task.yaml on the container side — it has to cross the boundary in context.json.
    Without this, `execute --driver docker` would silently grade after all."""
    assert (await _staged_context(tmp_path, grade=grade))["grade"] is grade


def test_container_defaults_to_grading_when_the_host_sends_no_key() -> None:
    """A host predating `execute` writes no `grade` key; the container must keep
    its original (grading) behavior rather than silently withholding verdicts."""
    # The parse is inline in a Typer command that cannot run outside a container,
    # so this reads its source. Resolved off the function object because
    # `coder_eval.cli` rebinds the submodule's name to the function it exports.
    import inspect

    from coder_eval.cli.run_task_internal_command import run_task_internal_command

    source = inspect.getsource(inspect.getmodule(run_task_internal_command))  # type: ignore[arg-type]
    assert 'context.get("grade", True)' in source, "the in-container default must be True (grade)"


def test_execute_help_explains_the_refused_flags() -> None:
    """The omission is documented in the help, not silently absent — a user who
    reaches for `--junit-xml` needs to learn why it is refused, not just that it
    is unrecognised. (Presence as a real *flag* is covered by the option-set test
    above; here we only require the help text to mention it.)"""
    result = runner.invoke(app, ["execute", "--help"])
    assert result.exit_code == 0
    output = _strip_ansi(result.output)
    for flag in _DELIBERATELY_ABSENT_FROM_EXECUTE:
        assert flag in output, f"execute's help should explain why {flag} is unavailable"
