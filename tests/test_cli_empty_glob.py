"""Tests for CLI handling of empty glob pattern results.

Tests ensure clear error messages prevent user confusion from silent failures.
"""

from datetime import datetime
from unittest.mock import patch

import pytest
import typer

from coder_eval.models import RunSummary


def test_cli_handles_empty_glob_results(tmp_path):
    """Test that CLI provides clear error for glob patterns with no matches.

    Hypothesis: Empty glob should display error and exit with code 1.
    Expected: "[red]No task files found![/red]" message, typer.Exit(1) raised.

    Context: Lines 175-177 in cli.py check for empty all_task_files.
    """
    # Create empty directory (no .yaml files)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    # Create a non-matching file to verify glob doesn't pick it up
    (tasks_dir / "readme.txt").write_text("Not a yaml file")

    # Simulate CLI call with glob pattern that matches nothing
    glob_pattern = tasks_dir / "*.yaml"

    with patch("coder_eval.cli.console.console.print") as mock_print:
        with pytest.raises(typer.Exit) as exc_info:
            # Call the synchronous run function, which will try to process the glob
            # We need to use asyncio.run internally since the actual function is async
            import asyncio

            # The run() function calls asyncio.run internally, but we need to
            # trigger the empty glob check. Let's call _run_all_tasks directly.
            from coder_eval.cli.run_command import _run_all_tasks

            asyncio.run(
                _run_all_tasks(
                    task_files=[glob_pattern],
                    max_iterations=None,
                    preserve=True,
                    run_dir=None,
                    max_parallel=1,
                    snapshot_mode=None,
                    snapshot_checkpoint_freq=None,
                )
            )

        # Verify exit code is 1
        assert exc_info.value.exit_code == 1

        # Verify error message was printed
        error_calls = [call for call in mock_print.call_args_list if "No task files found" in str(call)]
        assert len(error_calls) > 0, "Expected error message about no task files"


def test_cli_handles_invalid_glob_pattern(tmp_path):
    """Test that invalid glob patterns produce clear errors.

    Hypothesis: Malformed patterns should be caught and reported.
    Expected: Clear error message, non-zero exit.
    """
    # Create tasks directory
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    # Try with pattern that won't match anything
    pattern = tasks_dir / "[invalid-brackets-*.yaml"

    with patch("coder_eval.cli.console.console.print"):
        with pytest.raises(typer.Exit) as exc_info:
            import asyncio

            from coder_eval.cli.run_command import _run_all_tasks

            asyncio.run(
                _run_all_tasks(
                    task_files=[pattern],
                    max_iterations=None,
                    preserve=True,
                    run_dir=None,
                    max_parallel=1,
                    snapshot_mode=None,
                    snapshot_checkpoint_freq=None,
                )
            )

        assert exc_info.value.exit_code == 1


def test_cli_accepts_explicit_file_paths(tmp_path):
    """Test that explicit file paths work even when glob would fail.

    Hypothesis: Direct file paths should bypass glob expansion.
    Expected: File is processed when exists, error when missing.
    """
    # Create a task file
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_file = tasks_dir / "test_task.yaml"
    task_file.write_text("""
task_id: test
description: Test task
initial_prompt: Test
max_iterations: 1
agent:
  type: claude-code
sandbox:
  driver: tempdir
success_criteria:
  - type: file_exists
    description: Check output exists
    path: output.txt
""")

    # Test that explicit path is recognized (we'll mock the actual execution)
    import asyncio

    from coder_eval.cli.run_command import _run_all_tasks

    mock_summary = RunSummary(
        run_id="test-run",
        start_time=datetime.now(),
        end_time=datetime.now(),
        total_duration_seconds=1.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[],
        framework_version="test",
    )

    # Mock run_batch (always used via experiment layer)
    with (
        patch("coder_eval.orchestration.batch.run_batch", return_value=(mock_summary, [])) as mock_batch,
        patch("coder_eval.cli.console.console.print"),
        patch("coder_eval.logging_config.aggregate_task_logs"),
        patch("coder_eval.reports_experiment.ExperimentReportGenerator.write_reports"),
    ):
        # Should not raise error - file exists
        asyncio.run(
            _run_all_tasks(
                task_files=[task_file],
                max_iterations=None,
                preserve=True,
                run_dir=tmp_path / "run",
                max_parallel=1,
                snapshot_mode=None,
                snapshot_checkpoint_freq=None,
            )
        )

        assert mock_batch.called, "run_batch was not called"


def test_cli_expands_valid_glob_patterns(tmp_path):
    """Test that valid glob patterns are expanded correctly.

    Hypothesis: Glob patterns matching multiple files work as expected.
    Expected: All matching files collected and processed.
    """
    # Create multiple task files
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    for i in range(3):
        task_file = tasks_dir / f"task_{i}.yaml"
        task_file.write_text(f"""
task_id: task_{i}
description: Test task {i}
initial_prompt: Test
max_iterations: 1
agent:
  type: claude-code
sandbox:
  driver: tempdir
success_criteria:
  - type: file_exists
    description: Check output exists
    path: output.txt
""")

    # Test glob expansion
    glob_pattern = tasks_dir / "task_*.yaml"

    import asyncio

    from coder_eval.cli.run_command import _run_all_tasks

    mock_summary = RunSummary(
        run_id="test-run",
        start_time=datetime.now(),
        end_time=datetime.now(),
        total_duration_seconds=1.0,
        tasks_run=3,
        tasks_succeeded=3,
        tasks_failed=0,
        tasks_error=0,
        task_results=[],
        framework_version="test",
    )

    with (
        patch("coder_eval.orchestration.batch.run_batch", return_value=(mock_summary, [])) as mock_batch,
        patch("coder_eval.cli.console.console.print"),
        patch("coder_eval.logging_config.aggregate_task_logs"),
        patch("coder_eval.reports_experiment.ExperimentReportGenerator.write_reports"),
    ):
        asyncio.run(
            _run_all_tasks(
                task_files=[glob_pattern],
                max_iterations=None,
                preserve=True,
                run_dir=tmp_path / "run",
                max_parallel=1,
                snapshot_mode=None,
                snapshot_checkpoint_freq=None,
            )
        )

        assert mock_batch.called
        call_args = mock_batch.call_args
        assert len(call_args.kwargs["resolved_tasks"]) == 3
