"""`resolve_evaluate_target` — the shape detection behind `coder-eval evaluate`.

Pure and exhaustively testable by design: the command accepts two forms that
look alike on the command line, and picking the wrong one silently grades the
wrong thing. Every combination of (one arg / two args) x (run dir / plain dir /
file / missing) is enumerated here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coder_eval.cli.evaluate_target import (
    EvaluateMode,
    EvaluateTargetError,
    is_run_dir,
    resolve_evaluate_target,
)


def _run_dir(tmp_path: Path, name: str = "run") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "task.json").write_text(json.dumps({"task_id": "t"}), encoding="utf-8")
    return d


def _plain_dir(tmp_path: Path, name: str = "work") -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def _file(tmp_path: Path, name: str = "task.yaml") -> Path:
    f = tmp_path / name
    f.write_text("task_id: t", encoding="utf-8")
    return f


def test_is_run_dir_keys_on_task_json(tmp_path: Path) -> None:
    assert is_run_dir(_run_dir(tmp_path))
    assert not is_run_dir(_plain_dir(tmp_path))


# --- one argument ---------------------------------------------------------


def test_lone_run_dir_is_run_dir_mode(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    resolved = resolve_evaluate_target(run, None)
    assert resolved.mode is EvaluateMode.RUN_DIR
    assert resolved.target == run
    assert resolved.task_file is None, "a run dir carries its own config; nothing to supply"


def test_lone_task_file_is_rejected_with_the_fix(tmp_path: Path) -> None:
    """A task file alone names no place to grade."""
    with pytest.raises(EvaluateTargetError, match="not a directory"):
        resolve_evaluate_target(_file(tmp_path), None)


def test_lone_plain_dir_is_rejected_with_the_fix(tmp_path: Path) -> None:
    """A plain directory alone names no criteria."""
    plain = _plain_dir(tmp_path)
    with pytest.raises(EvaluateTargetError) as exc:
        resolve_evaluate_target(plain, None)
    # The message must name the missing piece AND the corrected command — a
    # caller here is one argument away from the right invocation.
    assert "task.json" in str(exc.value)
    assert "<task.yaml>" in str(exc.value)


# --- two arguments --------------------------------------------------------


def test_task_file_plus_plain_dir_is_the_original_form(tmp_path: Path) -> None:
    """The pre-existing shape must keep resolving exactly as before."""
    task, work = _file(tmp_path), _plain_dir(tmp_path)
    resolved = resolve_evaluate_target(task, work)
    assert resolved.mode is EvaluateMode.WORK_DIR
    assert resolved.target == work
    assert resolved.task_file == task


def test_task_file_plus_run_dir_re_grades_with_the_given_task(tmp_path: Path) -> None:
    """Iterating on criteria against a run you already paid for: the run supplies
    the trajectory and workspace, the explicit file supplies the criteria."""
    task, run = _file(tmp_path), _run_dir(tmp_path)
    resolved = resolve_evaluate_target(task, run)
    assert resolved.mode is EvaluateMode.RUN_DIR
    assert resolved.target == run
    assert resolved.task_file == task, "the override must survive; it is the whole point of this form"


def test_a_nonexistent_second_arg_stays_work_dir_mode(tmp_path: Path) -> None:
    """Shape detection must not invent run-dir mode for a path that isn't there;
    the command reports the missing directory itself, with a clearer message."""
    resolved = resolve_evaluate_target(_file(tmp_path), tmp_path / "nope")
    assert resolved.mode is EvaluateMode.WORK_DIR
