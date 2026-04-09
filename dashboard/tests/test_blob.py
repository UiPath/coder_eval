"""Tests for blob module."""

from unittest.mock import patch

from dashboard.blob import upload_run


@patch("dashboard.blob.subprocess.run")
def test_upload_run(mock_run, tmp_path):
    """Test that upload_run calls az CLI with correct arguments."""
    run_path = tmp_path / "test-run"
    run_path.mkdir()

    upload_run(run_path, "test-run", "mystorageaccount", "mycontainer")

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "az"
    assert "upload-batch" in cmd
    assert "--account-name" in cmd
    idx = cmd.index("--account-name")
    assert cmd[idx + 1] == "mystorageaccount"
    assert "--destination" in cmd
    idx = cmd.index("--destination")
    assert cmd[idx + 1] == "mycontainer"
    assert "--destination-path" in cmd
    idx = cmd.index("--destination-path")
    assert cmd[idx + 1] == "test-run"


@patch("dashboard.blob.subprocess.run")
def test_upload_run_check_true(mock_run, tmp_path):
    """Test that upload_run passes check=True."""
    upload_run(tmp_path, "run-id", "acct", "ctr")
    assert mock_run.call_args[1]["check"] is True
