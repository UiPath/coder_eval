"""Tests for the backfill_reviews script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "backfill_reviews.py"


def _import_backfill():
    spec = importlib.util.spec_from_file_location("backfill_reviews", _SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_reviews"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def backfill():
    return _import_backfill()


def _config():
    cfg = MagicMock()
    cfg.azure_storage_account = "x"
    cfg.azure_blob_container = "runs"
    cfg.azure_storage_key = ""
    cfg.adx_cluster_uri = "https://x"
    cfg.adx_database = "x"
    return cfg


def test_backfill_skips_existing(backfill):
    cfg = _config()
    with (
        patch.object(backfill, "blob_has_review_index", return_value=True) as mock_exists,
        patch.object(backfill, "download_run") as mock_download,
        patch.object(backfill, "generate_reviews") as mock_generate,
        patch.object(backfill, "upload_review_artifacts") as mock_upload,
        patch.object(backfill, "ingest_run") as mock_ingest,
    ):
        msg = backfill.backfill_run("2026-05-08_01-00-00", cfg, force=False, dry_run=False)

    assert "skip" in msg
    mock_exists.assert_called_once()
    mock_download.assert_not_called()
    mock_generate.assert_not_called()
    mock_upload.assert_not_called()
    mock_ingest.assert_not_called()


def test_backfill_force_regenerates(backfill):
    cfg = _config()

    def fake_download(run_id, dest, account, container, account_key=""):
        run_dir = dest / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("{}")

    with (
        patch.object(backfill, "blob_has_review_index", return_value=True) as mock_exists,
        patch.object(backfill, "download_run", side_effect=fake_download) as mock_download,
        patch.object(backfill, "generate_reviews") as mock_generate,
        patch.object(backfill, "upload_review_artifacts", return_value=3) as mock_upload,
        patch.object(backfill, "ingest_run") as mock_ingest,
    ):
        msg = backfill.backfill_run("2026-05-08_01-00-00", cfg, force=True, dry_run=False)

    mock_exists.assert_not_called()
    mock_download.assert_called_once()
    mock_generate.assert_called_once()
    mock_upload.assert_called_once()
    mock_ingest.assert_called_once()
    assert "OK" in msg


def test_backfill_skips_ingest_when_no_artifacts(backfill):
    """When upload_review_artifacts returns 0 (no review files), ingest is skipped."""
    cfg = _config()

    def fake_download(run_id, dest, account, container, account_key=""):
        run_dir = dest / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("{}")

    with (
        patch.object(backfill, "blob_has_review_index", return_value=False),
        patch.object(backfill, "download_run", side_effect=fake_download),
        patch.object(backfill, "generate_reviews"),
        patch.object(backfill, "upload_review_artifacts", return_value=0),
        patch.object(backfill, "ingest_run") as mock_ingest,
    ):
        msg = backfill.backfill_run("2026-05-08_01-00-00", cfg, force=False, dry_run=False)

    assert "skip (no review artifacts" in msg
    mock_ingest.assert_not_called()


def test_backfill_az_subprocess_does_not_pass_key_in_argv(backfill):
    """Storage key is exported via AZURE_STORAGE_KEY env, never on argv."""
    cfg = _config()
    cfg.azure_storage_key = "supersecret"
    captured: list[dict] = []

    def fake_run(cmd, env=None, **kwargs):
        captured.append({"cmd": cmd, "env": env})
        return MagicMock(stdout="", returncode=0)

    with patch.object(backfill.subprocess, "run", side_effect=fake_run):
        backfill.list_run_ids(cfg.azure_storage_account, cfg.azure_blob_container, cfg.azure_storage_key)

    assert captured, "subprocess.run was not invoked"
    cmd = captured[0]["cmd"]
    env = captured[0]["env"]
    assert "--account-key" not in cmd, f"secret leaked into argv: {cmd}"
    assert "supersecret" not in cmd, f"secret leaked into argv: {cmd}"
    assert env is not None and env.get("AZURE_STORAGE_KEY") == "supersecret"


def test_backfill_dry_run_no_writes(backfill):
    cfg = _config()
    with (
        patch.object(backfill, "blob_has_review_index") as mock_exists,
        patch.object(backfill, "download_run") as mock_download,
        patch.object(backfill, "generate_reviews") as mock_generate,
        patch.object(backfill, "upload_review_artifacts") as mock_upload,
        patch.object(backfill, "ingest_run") as mock_ingest,
    ):
        msg = backfill.backfill_run("2026-05-08_01-00-00", cfg, force=False, dry_run=True)

    assert "would" in msg
    mock_exists.assert_not_called()
    mock_download.assert_not_called()
    mock_generate.assert_not_called()
    mock_upload.assert_not_called()
    mock_ingest.assert_not_called()


def test_list_run_ids_filters_to_timestamped_prefixes(backfill):
    fake_proc = MagicMock(
        stdout="\n".join(
            [
                "2026-05-08_01-00-00/run.json",
                "2026-05-08_01-00-00/default/t1/00/task.json",
                "2026-05-07_23-30-00/run.json",
                "latest/run.json",
                "junk-prefix/file",
            ]
        )
    )
    with patch.object(backfill.subprocess, "run", return_value=fake_proc):
        ids = backfill.list_run_ids("acct", "runs")
    assert ids == ["2026-05-07_23-30-00", "2026-05-08_01-00-00"]
