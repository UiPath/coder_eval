"""Tests for build module."""

from unittest.mock import call, patch

from dashboard.build import build_cli


@patch("dashboard.build.subprocess.run")
def test_build_cli_success(mock_run, tmp_path):
    """Test successful CLI build returns True."""
    result = build_cli(tmp_path)
    assert result is True
    assert mock_run.call_count == 5
    # Verify the sequence of commands
    calls = mock_run.call_args_list
    assert calls[0] == call(["git", "checkout", "main"], cwd=tmp_path, check=True)
    assert calls[1] == call(["git", "pull"], cwd=tmp_path, check=True)
    assert calls[2] == call(["bun", "install"], cwd=tmp_path, check=True)
    assert calls[3] == call(["bun", "run", "build"], cwd=tmp_path, check=True)
    assert calls[4] == call(["bun", "run", "dev:install-cli"], cwd=tmp_path, check=True)


@patch("dashboard.build.subprocess.run")
def test_build_cli_failure(mock_run, tmp_path):
    """Test failed CLI build returns False."""
    import subprocess

    mock_run.side_effect = subprocess.CalledProcessError(1, "git")
    result = build_cli(tmp_path)
    assert result is False


@patch("dashboard.build.subprocess.run")
def test_build_cli_partial_failure(mock_run, tmp_path):
    """Test that failure mid-sequence returns False."""
    import subprocess

    mock_run.side_effect = [None, None, subprocess.CalledProcessError(1, "bun"), None, None]
    result = build_cli(tmp_path)
    assert result is False
    # Should have stopped at bun install (3rd call)
    assert mock_run.call_count == 3


@patch("dashboard.build.subprocess.run")
def test_build_cli_missing_dir(mock_run):
    """Test that missing cli_dir returns False instead of crashing."""
    from pathlib import Path

    mock_run.side_effect = FileNotFoundError("No such directory")
    result = build_cli(Path("/nonexistent/cli"))
    assert result is False
