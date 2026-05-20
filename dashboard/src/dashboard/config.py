"""Typed configuration loaded from environment / .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Walk up from src/dashboard/config.py → src/dashboard → src → dashboard/
# NOTE: Assumes source-checkout layout. Won't resolve correctly from pip-installed site-packages.
DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent

# The coder_eval repo root is one level above the dashboard/ directory.
CODER_EVAL_DIR = DASHBOARD_DIR.parent


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DASHBOARD_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ADX
    adx_cluster_uri: str
    adx_database: str

    # Azure
    azure_subscription_id: str = ""
    azure_storage_account: str
    azure_blob_container: str = "runs"
    # Storage account access key. When set, blob upload + pull use
    # `--auth-mode key`; when empty, fall back to `--auth-mode login`
    # (active az login or Managed Identity).
    azure_storage_key: str = ""

    # Skills repo path (sibling of coder_eval by default)
    skills_dir: Path = CODER_EVAL_DIR.parent / "skills"

    # UiPath CLI login (required for flow tasks)
    uip_authority: str = ""
    uip_client_id: str = ""
    uip_client_secret: str = ""
    uip_tenant: str = ""
    uip_scope: str = "OR.Default"
