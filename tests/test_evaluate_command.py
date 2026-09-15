"""Tests for evaluate CLI command."""

import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from coder_eval.cli import app


FIXTURES_DIR = Path(__file__).parent / "fixtures"
AGENTLESS_TASK = Path("tasks/agentless_smoke_test.yaml")
_needs_agentless = pytest.mark.skipif(
    not AGENTLESS_TASK.is_file(), reason="needs a source checkout (tasks/ is not in the wheel)"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def test_evaluate_command_success(tmp_path):
    """Test evaluate command with passing criteria."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_pass.yaml"

    # Create work directory with required file
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("print('hello')")

    # Import and run command
    from coder_eval.cli.evaluate_command import run_evaluation

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        # Should not raise - all criteria pass
        with pytest.raises(typer.Exit) as exc_info:
            run_evaluation(task_or_run_dir=task_file, work_dir=work_dir, run_dir=run_dir)

        assert exc_info.value.exit_code == 0


def test_evaluate_command_defaults_agent_type_when_missing(tmp_path):
    """Phase-3 regression: evaluate-only must work for tasks without `agent:` or `agent.type`.

    Such tasks defer the agent kind to experiment / --type for run paths, but
    evaluate-only bypasses both. The CLI fills in CLAUDE_CODE so the orchestrator
    invariants hold (the agent type is purely a label here — no agent runs).
    """
    task_file = tmp_path / "no_agent_task.yaml"
    task_file.write_text(
        "task_id: no_agent_task\n"
        "description: deferred-agent-type evaluate-only path\n"
        "initial_prompt: noop\n"
        "sandbox:\n"
        "  driver: tempdir\n"
        "  python: null\n"
        "success_criteria:\n"
        "  - type: file_exists\n"
        "    path: app.py\n"
        "    description: app.py must exist\n",
        encoding="utf-8",
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("print('hello')")

    from coder_eval.cli.evaluate_command import run_evaluation

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            run_evaluation(task_or_run_dir=task_file, work_dir=work_dir, run_dir=run_dir)
        assert exc_info.value.exit_code == 0


@pytest.mark.parametrize(
    ("preserve", "expected_mode"),
    [(True, "MOVE_ON_WRITE"), (False, "NONE")],
)
def test_evaluate_command_maps_preserve_to_mode(tmp_path, preserve, expected_mode):
    """evaluate maps its boolean --preserve to MOVE_ON_WRITE / NONE on the real Orchestrator."""
    from coder_eval.models import PreservationMode

    task_file = FIXTURES_DIR / "tasks" / "test_task_pass.yaml"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("print('hello')")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    from coder_eval.cli.evaluate_command import run_evaluation

    captured: dict[str, PreservationMode] = {}

    class _StopError(Exception):
        pass

    class _CapturingOrchestrator:
        def __init__(self, **kwargs):
            captured["mode"] = kwargs["preservation_mode"]

        async def run(self):
            raise _StopError  # short-circuit before the display logic; we only need the kwarg

    with (
        patch("coder_eval.cli.console.console.print"),
        patch("coder_eval.logging_config.setup_logging"),
        patch("coder_eval.cli.evaluate_command.Orchestrator", _CapturingOrchestrator),
        pytest.raises(_StopError),
    ):
        run_evaluation(task_or_run_dir=task_file, work_dir=work_dir, run_dir=run_dir, preserve=preserve)

    assert captured["mode"] == PreservationMode(expected_mode)


def test_evaluate_command_failure(tmp_path):
    """Test evaluate command with failing criteria."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_pass.yaml"

    # Create empty work directory (missing required file)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Import and run command
    from coder_eval.cli.evaluate_command import run_evaluation

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        # Should fail - file doesn't exist
        with pytest.raises(typer.Exit) as exc_info:
            run_evaluation(task_or_run_dir=task_file, work_dir=work_dir, run_dir=run_dir)

        assert exc_info.value.exit_code == 1


def test_evaluate_command_informational_criterion_does_not_fail_exit(tmp_path):
    """A failing weight=0 criterion must not flip the exit code (it is non-gating)."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_informational_criterion.yaml"

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("print('hello')")  # gating criterion passes; missing.py absent

    from coder_eval.cli.evaluate_command import run_evaluation

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            run_evaluation(task_or_run_dir=task_file, work_dir=work_dir, run_dir=run_dir)

        # The only gating criterion passed → exit 0, despite the weight=0 miss.
        assert exc_info.value.exit_code == 0


def test_evaluate_command_invalid_task_file(tmp_path):
    """Test evaluate command with invalid task file."""
    # Use non-existent task file
    task_file = FIXTURES_DIR / "tasks" / "nonexistent.yaml"

    # Create work directory
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Import and run command
    from coder_eval.cli.evaluate_command import run_evaluation

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            run_evaluation(task_or_run_dir=task_file, work_dir=work_dir, run_dir=run_dir)

        assert exc_info.value.exit_code == 1


def test_evaluate_command_invalid_work_dir(tmp_path):
    """Test evaluate command with invalid work directory."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_pass.yaml"

    # Use non-existent work directory
    work_dir = tmp_path / "nonexistent"

    # Import and run command
    from coder_eval.cli.evaluate_command import run_evaluation

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            run_evaluation(task_or_run_dir=task_file, work_dir=work_dir, run_dir=run_dir)

        assert exc_info.value.exit_code == 1


def test_evaluate_command_multiple_criteria(tmp_path):
    """Test evaluate command with multiple criteria."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_multiple_criteria.yaml"

    # Create work directory with solution
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("print('hello')")

    # Import and run command
    from coder_eval.cli.evaluate_command import run_evaluation

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            run_evaluation(task_or_run_dir=task_file, work_dir=work_dir, run_dir=run_dir)

        # All 3 criteria should pass
        assert exc_info.value.exit_code == 0


# --------------------------------------------------------------------------
# `evaluate <run_dir>` refreshes the run-level run.json itself
# --------------------------------------------------------------------------


def _shows(output: str, text: str) -> bool:
    """Whether console ``output`` shows ``text``; Rich wraps paths at any character, so compare without whitespace."""

    def squash(s: str) -> str:
        return "".join(_ANSI_RE.sub("", s).split())

    return squash(text) in squash(output)


def _row_dirs(run_dir: Path) -> list[Path]:
    return sorted(p.parent for p in run_dir.rglob("task.json"))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _plant_ungraded_row(run_dir: Path, source_row: Path, task_id: str) -> Path:
    """A second executed row beside ``source_row``, under its own task id."""
    from coder_eval.models import EvaluationResult
    from coder_eval.path_utils import build_task_run_dir

    record = EvaluationResult.model_validate_json((source_row / "task.json").read_text(encoding="utf-8"))
    record.task_id = task_id
    target = build_task_run_dir(run_dir, record.variant_id, task_id, 0)
    target.mkdir(parents=True)
    (target / "task.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return target


def _evaluate(target: Path):
    return CliRunner().invoke(app, ["evaluate", str(target)])


@pytest.fixture
def executed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real `execute` run dir: one NOT_GRADED row and its run.json. Grading passes stay inside tmp_path."""
    from coder_eval.cli import run_helpers

    monkeypatch.setattr(run_helpers.settings, "runs_dir", tmp_path / "default-runs")
    run_dir = tmp_path / "r"
    result = CliRunner().invoke(app, ["execute", str(AGENTLESS_TASK), "--run-dir", str(run_dir)])
    assert result.exit_code == 0, result.output
    return run_dir


@_needs_agentless
def test_evaluate_refreshes_the_run_level_run_json(executed_run: Path) -> None:
    """No second command: after grading one row, run.json agrees with every row on disk."""
    from coder_eval.orchestration.batch import recover_task_results

    (graded_row,) = _row_dirs(executed_run)
    _plant_ungraded_row(executed_run, graded_row, "a_second_row")
    stale = _read_json(executed_run / "run.json")

    result = _evaluate(graded_row)

    assert result.exit_code == 0, result.output
    buckets = Counter(r.result.final_status.category for r in recover_task_results(executed_run))
    refreshed = _read_json(executed_run / "run.json")
    assert refreshed["tasks_run"] == sum(buckets.values()) == stale["tasks_run"] + 1
    assert refreshed["tasks_succeeded"] == buckets["succeeded"]
    assert refreshed["tasks_not_graded"] == buckets["ungraded"]
    assert _shows(result.output, f"Refreshed {executed_run / 'run.json'}")


@_needs_agentless
def test_evaluate_without_a_run_root_says_so(executed_run: Path, tmp_path: Path) -> None:
    """A row copied out of its run has no run.json above it: say so, and create none."""
    (row,) = _row_dirs(executed_run)
    copied_root = tmp_path / "copied-out"
    copied_row = copied_root / "00"
    shutil.copytree(row, copied_row, symlinks=True)
    # The recorded workspace still points into the original run, so name the copy explicitly.
    (workspace,) = (copied_row / "artifacts").iterdir()

    result = CliRunner().invoke(app, ["evaluate", str(copied_row), "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert _shows(result.output, "not inside a run directory")
    assert not list(copied_root.rglob("run.json"))


@_needs_agentless
@pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs a privilege on Windows")
def test_a_symlinked_run_json_is_refused(executed_run: Path, tmp_path: Path) -> None:
    """A run dir is a shareable artifact; following its run.json link would overwrite any file the grader can write."""
    (row,) = _row_dirs(executed_run)
    victim = tmp_path / "victim.json"
    victim.write_text("keep me", encoding="utf-8")
    (executed_run / "run.json").unlink()
    (executed_run / "run.json").symlink_to(victim)

    result = _evaluate(row)

    assert result.exit_code == 0, result.output
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert (executed_run / "run.json").is_symlink()
    assert _shows(result.output, "is a symlink"), result.output


@_needs_agentless
def test_a_quarantined_row_is_not_folded_in(executed_run: Path) -> None:
    """`task.json.unhonored` is a refused container record; `rglob("task.json")` must not see it."""
    (row,) = _row_dirs(executed_run)
    graded_task_id = _read_json(row / "task.json")["task_id"]
    refused = _plant_ungraded_row(executed_run, row, "refused_by_the_contract_echo")
    (refused / "task.json").rename(refused / "task.json.unhonored")

    result = _evaluate(row)

    assert result.exit_code == 0, result.output
    assert _shows(result.output, f"Refreshed {executed_run / 'run.json'}"), result.output
    assert {r["task_id"] for r in _read_json(executed_run / "run.json")["task_results"]} == {graded_task_id}


@_needs_agentless
def test_a_rebuild_failure_does_not_change_the_exit_code(executed_run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort, like the write-back: the verdict is computed and printed before the refresh."""
    from coder_eval.orchestration import run_summary_rebuild

    def _boom(_run_dir: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(run_summary_rebuild, "rebuild_run_summary", _boom)
    (row,) = _row_dirs(executed_run)

    result = _evaluate(row)

    assert result.exit_code == 0, result.output
    assert _shows(result.output, "disk full")
    assert _read_json(row / "task.json")["final_status"] == "SUCCESS"


@_needs_agentless
def test_a_grading_run_dir_inside_the_run_does_not_refresh(executed_run: Path) -> None:
    """`--run-dir` under the owning run writes a second task.json there; folding it in would count the row twice."""
    (row,) = _row_dirs(executed_run)
    before = (executed_run / "run.json").read_text(encoding="utf-8")

    result = CliRunner().invoke(app, ["evaluate", str(row), "--run-dir", str(executed_run / "regrade")])

    assert result.exit_code == 0, result.output
    assert _shows(result.output, "would count as a second row"), result.output
    assert (executed_run / "run.json").read_text(encoding="utf-8") == before


def test_the_refresh_lines_survive_rich_markup_in_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run directory name is untrusted; `[/y]` in it must print, not raise a MarkupError after the verdict."""
    from coder_eval.cli.evaluate_command import _refresh_run_summary
    from coder_eval.orchestration import run_summary_rebuild

    odd_root = tmp_path / "odd[/y]run"
    monkeypatch.setattr(run_summary_rebuild, "find_run_root", lambda _path: odd_root)
    monkeypatch.setattr(run_summary_rebuild, "rebuild_run_summary", lambda _root: object())

    _refresh_run_summary(tmp_path / "row", tmp_path / "elsewhere")

    assert _shows(capsys.readouterr().out, f"Refreshed {odd_root / 'run.json'}")


@_needs_agentless
def test_work_dir_mode_refreshes_no_run_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`evaluate <task.yaml> <dir>` grades a directory, not a run row, so nothing run-level is touched."""
    from coder_eval.cli import run_helpers

    monkeypatch.setattr(run_helpers.settings, "runs_dir", tmp_path / "default-runs")
    work = tmp_path / "work"
    work.mkdir()
    (work / "proof.txt").write_text("coder-eval-ran-without-a-coder", encoding="utf-8")
    ancestor_run_json = '{"run_id": "not-this-one", "task_results": []}'
    (tmp_path / "run.json").write_text(ancestor_run_json, encoding="utf-8")

    result = CliRunner().invoke(app, ["evaluate", str(AGENTLESS_TASK), str(work)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "run.json").read_text(encoding="utf-8") == ancestor_run_json
    assert not _shows(result.output, "Refreshed")
    assert not _shows(result.output, "not inside a run directory")
