"""Tests for evaluate CLI command."""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_evaluate_command_success(tmp_path):
    """Test evaluate command with passing criteria."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_pass.yaml"

    # Create work directory with required file
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("print('hello')")

    # Import and run command
    from coder_eval.cli.evaluate_command import evaluate_command

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        # Should not raise - all criteria pass
        with pytest.raises(typer.Exit) as exc_info:
            evaluate_command(task_file=task_file, work_dir=work_dir, run_dir=run_dir)

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

    from coder_eval.cli.evaluate_command import evaluate_command

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            evaluate_command(task_file=task_file, work_dir=work_dir, run_dir=run_dir)
        assert exc_info.value.exit_code == 0


def test_evaluate_command_failure(tmp_path):
    """Test evaluate command with failing criteria."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_pass.yaml"

    # Create empty work directory (missing required file)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Import and run command
    from coder_eval.cli.evaluate_command import evaluate_command

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        # Should fail - file doesn't exist
        with pytest.raises(typer.Exit) as exc_info:
            evaluate_command(task_file=task_file, work_dir=work_dir, run_dir=run_dir)

        assert exc_info.value.exit_code == 1


def test_evaluate_command_invalid_task_file(tmp_path):
    """Test evaluate command with invalid task file."""
    # Use non-existent task file
    task_file = FIXTURES_DIR / "tasks" / "nonexistent.yaml"

    # Create work directory
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Import and run command
    from coder_eval.cli.evaluate_command import evaluate_command

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            evaluate_command(task_file=task_file, work_dir=work_dir, run_dir=run_dir)

        assert exc_info.value.exit_code == 1


def test_evaluate_command_invalid_work_dir(tmp_path):
    """Test evaluate command with invalid work directory."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_pass.yaml"

    # Use non-existent work directory
    work_dir = tmp_path / "nonexistent"

    # Import and run command
    from coder_eval.cli.evaluate_command import evaluate_command

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            evaluate_command(task_file=task_file, work_dir=work_dir, run_dir=run_dir)

        assert exc_info.value.exit_code == 1


def test_evaluate_command_multiple_criteria(tmp_path):
    """Test evaluate command with multiple criteria."""
    task_file = FIXTURES_DIR / "tasks" / "test_task_multiple_criteria.yaml"

    # Create work directory with solution
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("print('hello')")

    # Import and run command
    from coder_eval.cli.evaluate_command import evaluate_command

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch("coder_eval.cli.console.console.print"), patch("coder_eval.logging_config.setup_logging"):
        with pytest.raises(typer.Exit) as exc_info:
            evaluate_command(task_file=task_file, work_dir=work_dir, run_dir=run_dir)

        # All 3 criteria should pass
        assert exc_info.value.exit_code == 0
