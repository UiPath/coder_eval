"""Tests for analysis module."""

from unittest.mock import patch

import pytest

from dashboard.analysis import generate_analysis


@patch("dashboard.analysis.subprocess.run")
def test_generate_analysis_success(mock_run, tmp_path):
    """Test that generate_analysis invokes claude and returns the analysis path."""
    run_path = tmp_path / "test-run"
    run_path.mkdir()
    analysis_path = run_path / "analysis.md"
    analysis_path.write_text("# Analysis\nAll good.")

    result = generate_analysis(run_path)

    assert result == analysis_path
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert cmd[1] == "--print"
    assert "/coder-eval-run-analysis" in cmd[2]


@patch("dashboard.analysis.subprocess.run")
def test_generate_analysis_missing_output(mock_run, tmp_path):
    """Test that generate_analysis raises if analysis.md not created."""
    run_path = tmp_path / "test-run"
    run_path.mkdir()
    # Don't create analysis.md — should raise

    with pytest.raises(FileNotFoundError, match="Analysis was not generated"):
        generate_analysis(run_path)


@patch("dashboard.analysis.subprocess.run")
def test_generate_analysis_removes_claudecode_env(mock_run, tmp_path):
    """Test that CLAUDECODE env var is stripped."""
    run_path = tmp_path / "test-run"
    run_path.mkdir()
    (run_path / "analysis.md").write_text("ok")

    with patch.dict("os.environ", {"CLAUDECODE": "1", "HOME": "/home/test"}):
        generate_analysis(run_path)

    env = mock_run.call_args[1]["env"]
    assert "CLAUDECODE" not in env
    assert "HOME" in env
