"""Tests for blob module."""

from unittest.mock import MagicMock, patch

import azure.storage.blob as azure_blob

from dashboard.blob import _excluded, upload_run


@patch.object(azure_blob, "BlobServiceClient")
def test_upload_run(mock_client_cls, tmp_path):
    """upload_run uploads each non-excluded file under the <run_id>/ prefix."""
    run_path = tmp_path / "test-run"
    run_path.mkdir()
    (run_path / "run.json").write_text("{}")
    (run_path / "sub").mkdir()
    (run_path / "sub" / "task.log").write_text("log")

    container_client = MagicMock()
    mock_client_cls.return_value.get_container_client.return_value = container_client

    upload_run(run_path, "test-run", "mystorageaccount", "mycontainer", account_key="key")

    # SDK client built against the storage-account blob endpoint.
    url = mock_client_cls.call_args[0][0]
    assert url == "https://mystorageaccount.blob.core.windows.net"
    mock_client_cls.return_value.get_container_client.assert_called_once_with("mycontainer")

    # Each file uploaded under the run-id prefix.
    uploaded = {c.args[0] for c in container_client.upload_blob.call_args_list}
    assert uploaded == {"test-run/run.json", "test-run/sub/task.log"}


def test_excluded_skips_build_artifacts():
    """_excluded matches reconstructible build artifacts, not run output."""
    assert _excluded("foo/.venv/lib/x.py")
    assert _excluded("proj/bin/thing.dll")
    assert _excluded("a.pyc")
    assert not _excluded("test-run/run.json")
