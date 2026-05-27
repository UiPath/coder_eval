"""Tests for config module."""

from pathlib import Path

from dashboard.config import CODER_EVAL_DIR, DASHBOARD_DIR, Config


def test_dashboard_dir_points_to_dashboard_root():
    assert (DASHBOARD_DIR / "pyproject.toml").exists()


def test_coder_eval_dir_is_parent_of_dashboard():
    assert DASHBOARD_DIR.parent == CODER_EVAL_DIR


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "teststorage")
    cfg = Config()
    assert cfg.azure_storage_account == "teststorage"
    assert cfg.azure_blob_container == "runs"  # default


def test_config_skills_dir_default():
    """skills_dir defaults to a sibling of the coder_eval repo."""
    cfg_skills = Config.model_fields["skills_dir"].default
    assert isinstance(cfg_skills, Path)
    assert cfg_skills.name == "skills"
