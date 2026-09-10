"""`coder-eval run --junit-xml` wiring at the ``_run_all_tasks`` seam.

Fully hermetic: every side-effecting collaborator is patched (see the pattern in
``test_cli_telemetry.py``). The ``_run_with_experiment`` mock writes a minimal
``run.json`` to disk, which is exactly what the JUnit writer reads.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import typer
from defusedxml.ElementTree import fromstring

from coder_eval.cli.run_command import _run_all_tasks


async def _invoke(
    run_dir: Path,
    junit_xml: Path | None,
    write_run_json: Callable[..., Path],
    *,
    status: str,
    failed: bool,
) -> None:
    summary = Mock(tasks_failed=1 if failed else 0, tasks_error=0, tasks_not_graded=0)

    async def _fake(*_args, **_kwargs):
        # Mirror production: run.json is persisted inside _run_with_experiment.
        write_run_json(run_dir, [{"task_id": "t", "status": status}])
        return summary, 0

    with (
        patch("coder_eval.cli.run_command.prepare_run_directory", return_value=run_dir),
        patch("coder_eval.cli.run_command.expand_task_files", return_value=[Path("a.yaml")]),
        patch("coder_eval.cli.run_command._run_with_experiment", new=AsyncMock(side_effect=_fake)),
        patch("coder_eval.logging_config.aggregate_task_logs"),
        patch("coder_eval.cli.run_command.print_execution_summary"),
        patch("coder_eval.telemetry.track_event"),
        patch("coder_eval.telemetry.flush_telemetry"),
    ):
        await _run_all_tasks(
            task_files=[Path("a.yaml")],
            preservation_mode=None,
            run_dir=run_dir,
            max_parallel=1,
            junit_xml=junit_xml,
        )


async def test_junit_written_on_success(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    junit = tmp_path / "j.xml"
    await _invoke(run_dir, junit, write_run_json, status="SUCCESS", failed=False)
    assert junit.is_file()
    fromstring(junit.read_text(encoding="utf-8"))


async def test_junit_written_even_when_run_fails(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    # Ordering guarantee: the report is written BEFORE the failure exit-code gate.
    run_dir = tmp_path / "run"
    junit = tmp_path / "j.xml"
    with pytest.raises(typer.Exit) as exc:
        await _invoke(run_dir, junit, write_run_json, status="FAILURE", failed=True)
    assert exc.value.exit_code == 1
    assert junit.is_file()
    fromstring(junit.read_text(encoding="utf-8"))


async def test_no_junit_when_flag_absent(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    junit = tmp_path / "j.xml"
    await _invoke(run_dir, None, write_run_json, status="SUCCESS", failed=False)
    assert not junit.exists()
