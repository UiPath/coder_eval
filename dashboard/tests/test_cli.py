"""Tests for CLI commands."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from dashboard.cli import _build_activation_suite, _build_skills_suite, cli


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
@patch("dashboard.blob.upload_run")
def test_upload_command(mock_upload_run, mock_config_cls, tmp_path):
    """Test that `dashboard upload` calls upload_run with the right args."""
    run_dir = tmp_path / "test-run"
    run_dir.mkdir()

    mock_cfg = MagicMock()
    mock_cfg.azure_storage_account = "teststorage"
    mock_cfg.azure_blob_container = "runs"
    mock_cfg.azure_storage_key = ""
    mock_config_cls.return_value = mock_cfg

    runner = CliRunner()
    result = runner.invoke(cli, ["upload", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "Uploaded" in result.output
    mock_upload_run.assert_called_once()
    assert "teststorage" in str(mock_upload_run.call_args)


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


def _setup_run_pipeline_mocks(mock_config_cls, tmp_path):
    """Configure shared mocks for `dashboard run` tests."""
    mock_cfg = MagicMock()
    mock_cfg.skills_dir = tmp_path / "skills"
    mock_cfg.uip_authority = ""
    mock_cfg.uip_client_id = ""
    mock_cfg.uip_client_secret = ""
    mock_cfg.uip_tenant = ""
    mock_cfg.uip_scope = "OR.Default"
    mock_cfg.azure_storage_account = "x"
    mock_cfg.azure_blob_container = "runs"
    mock_cfg.azure_storage_key = ""
    mock_cfg.adx_cluster_uri = "https://x"
    mock_cfg.adx_database = "x"
    mock_config_cls.return_value = mock_cfg

    latest_run = tmp_path / "run-1"
    latest_run.mkdir()
    return mock_cfg, latest_run


@patch("dashboard.cli.Config")
def test_cli_run_calls_review(mock_config_cls, tmp_path):
    """`dashboard run` invokes generate_reviews after generate_analysis per suite."""
    _, latest_run = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)

    with (
        patch("dashboard.run.run_tests", return_value=latest_run),
        patch("dashboard.analysis.generate_analysis") as mock_analysis,
        patch("dashboard.review.generate_reviews") as mock_review,
        patch("dashboard.blob.upload_run"),
        patch("dashboard.ingest.ingest_run"),
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["run", "--skip-pull", "--skip-login", "--suite", "smoke"],
        )

    assert result.exit_code == 0, result.output
    mock_analysis.assert_called_once_with(latest_run)
    mock_review.assert_called_once_with(latest_run)


@patch("dashboard.cli.Config")
def test_cli_skip_review(mock_config_cls, tmp_path):
    """--skip-review skips the call entirely."""
    _, latest_run = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)

    with (
        patch("dashboard.run.run_tests", return_value=latest_run),
        patch("dashboard.analysis.generate_analysis"),
        patch("dashboard.review.generate_reviews") as mock_review,
        patch("dashboard.blob.upload_run"),
        patch("dashboard.ingest.ingest_run"),
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "run",
                "--skip-pull",
                "--skip-login",
                "--skip-review",
                "--suite",
                "smoke",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_review.assert_not_called()
    assert "Skipping task reviews" in result.output


@patch("dashboard.cli.Config")
def test_cli_warns_when_skip_analysis_without_skip_review(mock_config_cls, tmp_path):
    """If analysis is skipped, the user should be warned reviews lose context."""
    _, latest_run = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)

    with (
        patch("dashboard.run.run_tests", return_value=latest_run),
        patch("dashboard.review.generate_reviews"),
        patch("dashboard.blob.upload_run"),
        patch("dashboard.ingest.ingest_run"),
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "run",
                "--skip-pull",
                "--skip-login",
                "--skip-analysis",
                "--suite",
                "smoke",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "WARNING: --skip-analysis is set but --skip-review is not" in result.output


@patch("dashboard.cli.Config")
def test_cli_review_failure_does_not_abort(mock_config_cls, tmp_path):
    """If review generation raises, upload_run and ingest_run still run."""
    _, latest_run = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)

    with (
        patch("dashboard.run.run_tests", return_value=latest_run),
        patch("dashboard.analysis.generate_analysis"),
        patch("dashboard.review.generate_reviews", side_effect=RuntimeError("boom")),
        patch("dashboard.blob.upload_run") as mock_upload,
        patch("dashboard.ingest.ingest_run") as mock_ingest,
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["run", "--skip-pull", "--skip-login", "--suite", "smoke"],
        )

    assert result.exit_code == 0, result.output
    mock_upload.assert_called_once()
    mock_ingest.assert_called_once()
    assert "Task review generation failed" in result.output


def test_build_skills_suite():
    """Test that _build_skills_suite constructs the right suite."""
    suite = _build_skills_suite("/path/to/skills")
    assert suite.name == "skills"
    assert suite.task_patterns == ["/path/to/skills/tests/tasks/**/*.yaml"]
    # Activation/ is carved out structurally; it's owned by the activation suite.
    assert suite.exclude_patterns == ["/path/to/skills/tests/tasks/activation/**/*.yaml"]
    assert suite.experiment == "/path/to/skills/tests/experiments/nightly.yaml"
    assert suite.uip_login is True
    assert suite.default is True
    assert suite.env == {"SKILLS_REPO_PATH": "/path/to/skills"}


def test_build_activation_suite():
    """Activation suite is opt-in (default=False) so it doesn't run on the daily."""
    suite = _build_activation_suite("/path/to/skills")
    assert suite.name == "activation"
    assert suite.task_patterns == ["/path/to/skills/tests/tasks/activation/**/*.yaml"]
    assert suite.experiment == "/path/to/skills/tests/experiments/activation.yaml"
    assert suite.default is False
    assert suite.uip_login is False
    assert suite.env == {"SKILLS_REPO_PATH": "/path/to/skills"}
