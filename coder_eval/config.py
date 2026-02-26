"""Configuration management using pydantic-settings."""

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


# Load .env file without overriding (allows shell environment for API keys)
load_dotenv(override=False)

# For certain keys, we want .env values to take precedence over shell environment
# because the shell may have outdated/different credentials
env_values = dotenv_values(".env")
for key in [
    "ANTHROPIC_API_KEY",
    "LLMGW_URL",
    "LLMGW_CLIENT_ID",
    "LLMGW_CLIENT_SECRET",
    "LLMGW_SEMANTIC_ORG_ID",
    "LLMGW_SEMANTIC_TENANT_ID",
    "LLMGW_SEMANTIC_USER_ID",
    "LLMGW_REQUESTING_PRODUCT",
    "LLMGW_REQUESTING_FEATURE",
    "LLMGW_TIMEOUT_SECONDS",
]:
    value = env_values.get(key)
    if value:
        os.environ[key] = value


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys (for Claude Code agent only)
    anthropic_api_key: str | None = None

    # LLM Gateway settings (required for LLM reviewer)
    llmgw_url: str | None = None
    llmgw_client_id: str | None = None
    llmgw_client_secret: str | None = None
    llmgw_semantic_org_id: str | None = None
    llmgw_semantic_tenant_id: str | None = None
    llmgw_semantic_user_id: str | None = None
    llmgw_requesting_product: str = "coder-eval"
    llmgw_requesting_feature: str = "llm-reviewer"
    llmgw_timeout_seconds: str = "290"

    # Paths
    runs_dir: Path = Path("runs")  # Base directory for timestamped runs

    # Defaults
    default_agent_type: str = "claude-code"
    default_max_iterations: int = 3

    # Logging
    log_level: str = "INFO"  # Default log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    log_to_file: bool = False  # Whether to enable file logging

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    def validate_api_keys(self, agent_type: str) -> None:
        """Validate that required API keys are present.

        Args:
            agent_type: The type of agent being used

        Raises:
            ValueError: If required API key is missing
        """
        if agent_type == "claude-code" and not self.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for Claude Code agent. Please set it in your .env file.")


# Global settings instance
settings = Settings()

# Export settings to environment variables for external libraries (like LLM Gateway client)
# that use os.getenv() instead of reading from the Settings object.
# Only export non-None values and convert non-string types to strings.
for key, value in settings.model_dump().items():
    if value is not None:
        env_key = key.upper()
        # Convert Path objects and other types to strings
        if isinstance(value, Path):
            os.environ[env_key] = str(value)
        elif isinstance(value, bool):
            os.environ[env_key] = str(value).lower()
        else:
            os.environ[env_key] = str(value)
