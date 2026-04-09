"""Tests for run module."""

from pathlib import Path
from unittest.mock import patch

import pytest

from dashboard.run import pull_coder_eval, run_tests, uip_login


@patch("dashboard.run.subprocess.run")
def test_pull_coder_eval(mock_run):
    """Test that pull_coder_eval runs git pull in the right directory."""
    pull_coder_eval()
    mock_run.assert_called_once()
    call_args = mock_run.call_args
    assert call_args[0][0] == ["git", "pull"]


@patch("dashboard.run.subprocess.run")
@patch("dashboard.run.glob")
def test_run_tests_basic(mock_glob, mock_run, tmp_path):
    """Test that run_tests constructs the right coder-eval command."""
    mock_glob.return_value = ["/fake/tasks/task1.yaml"]

    with patch.object(Path, "resolve", return_value=tmp_path), patch.object(Path, "exists", return_value=True):
        run_tests(model="claude-sonnet-4-6", max_iter=2, tags="smoke")

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert "uv" in cmd
    assert "coder-eval" in cmd
    assert "--model" in cmd
    assert "--tags" in cmd


@patch("dashboard.run.subprocess.run")
@patch("dashboard.run.glob")
def test_run_tests_no_tasks_raises(mock_glob, mock_run):
    """Test that run_tests raises if no task files found."""
    mock_glob.return_value = []
    with pytest.raises(FileNotFoundError, match="No task YAML files"):
        run_tests(tags="smoke")


@patch("dashboard.run.subprocess.run")
@patch("dashboard.run.glob")
def test_run_tests_with_concurrency(mock_glob, mock_run, tmp_path):
    """Test that concurrency flag is passed through."""
    mock_glob.return_value = ["/fake/tasks/task1.yaml"]

    with patch.object(Path, "resolve", return_value=tmp_path), patch.object(Path, "exists", return_value=True):
        run_tests(tags=None, concurrency=4)

    cmd = mock_run.call_args[0][0]
    assert "-j" in cmd
    assert "4" in cmd
    # No --tags when tags=None
    assert "--tags" not in cmd


@patch("dashboard.run.subprocess.run")
def test_uip_login_does_not_pass_secret_as_arg(mock_run):
    """Test that uip_login passes the secret via env, not as a CLI argument."""
    uip_login(
        authority="https://auth.example.com",
        client_id="my-client",
        client_secret="super-secret",
        tenant="my-tenant",
        scope="OR.Default",
    )
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    # Secret should NOT appear in the command arguments
    assert "super-secret" not in cmd
    # Scope should be passed as a CLI argument
    assert "--scope" in cmd
    assert "OR.Default" in cmd
    # But secret should be in the environment
    env = mock_run.call_args[1]["env"]
    assert env["UIP_CLIENT_SECRET"] == "super-secret"


@patch("dashboard.run.subprocess.run")
def test_run_tests_strips_claudecode_env(mock_run):
    """Test that CLAUDECODE env var is stripped when running tests."""
    with (
        patch("dashboard.run.glob", return_value=["/fake/task.yaml"]),
        patch.object(Path, "resolve", return_value=Path("/fake/run")),
        patch.object(Path, "exists", return_value=True),
        patch.dict("os.environ", {"CLAUDECODE": "1", "HOME": "/home/test"}),
    ):
        run_tests(tags="smoke")

    env = mock_run.call_args[1]["env"]
    assert "CLAUDECODE" not in env
