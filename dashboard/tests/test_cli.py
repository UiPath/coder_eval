"""Tests for CLI commands."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from dashboard.cli import cli


@patch("dashboard.cli.Config")
@patch("dashboard.ingest.adx.get_client")
def test_ingest_command(mock_get_client, mock_config_cls, tmp_path):
    """Test that `dashboard ingest` calls ingest_run with config values."""
    # Create minimal run fixture
    run_dir = tmp_path / "test-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        '{"run_id":"r1","start_time":"2026-01-01T00:00:00Z","end_time":"2026-01-01T00:01:00Z",'
        '"total_duration_seconds":60,"tasks_run":1,"tasks_succeeded":1,"tasks_failed":0,'
        '"tasks_error":0,"task_results":[]}'
    )

    mock_cfg = MagicMock()
    mock_cfg.adx_cluster_uri = "https://fake-cluster"
    mock_cfg.adx_database = "fake-db"
    mock_config_cls.return_value = mock_cfg

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(cli, ["ingest", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "SmokeRuns OK" in result.output
    mock_get_client.assert_called_with("https://fake-cluster")


@patch("dashboard.cli.Config")
@patch("dashboard.blob.subprocess.run")
def test_upload_command(mock_subprocess, mock_config_cls, tmp_path):
    """Test that `dashboard upload` calls az storage blob upload-batch."""
    run_dir = tmp_path / "test-run"
    run_dir.mkdir()

    mock_cfg = MagicMock()
    mock_cfg.azure_storage_account = "teststorage"
    mock_cfg.azure_blob_container = "runs"
    mock_config_cls.return_value = mock_cfg

    runner = CliRunner()
    result = runner.invoke(cli, ["upload", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "Uploaded" in result.output
    mock_subprocess.assert_called_once()
    call_args = mock_subprocess.call_args[0][0]
    assert "az" in call_args
    assert "--account-name" in call_args


@patch("dashboard.cli.Config")
@patch("dashboard.schema.adx.get_client")
def test_schema_command(mock_get_client, mock_config_cls):
    """Test that `dashboard schema` creates tables."""
    mock_cfg = MagicMock()
    mock_cfg.adx_cluster_uri = "https://fake-cluster"
    mock_cfg.adx_database = "fake-db"
    mock_config_cls.return_value = mock_cfg

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(cli, ["schema"])
    assert result.exit_code == 0, result.output
    assert "Schema is ready" in result.output

    # Should have called execute_mgmt for drop + create (4 tables each = 8 calls)
    assert mock_client.execute_mgmt.call_count == 8


@patch("dashboard.cli.Config")
@patch("dashboard.schema.adx.get_client")
def test_schema_drop_only(mock_get_client, mock_config_cls):
    """Test that `dashboard schema --drop` only drops tables."""
    mock_cfg = MagicMock()
    mock_cfg.adx_cluster_uri = "https://fake-cluster"
    mock_cfg.adx_database = "fake-db"
    mock_config_cls.return_value = mock_cfg

    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(cli, ["schema", "--drop"])
    assert result.exit_code == 0, result.output
    assert "Tables dropped" in result.output

    # Only 4 drop calls, no create
    assert mock_client.execute_mgmt.call_count == 4
