"""Configuration management using pydantic-settings."""

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

from coder_eval.models.enums import ApiBackend


# Load .env file with override so .env values always win over shell environment
load_dotenv(override=True)

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
    llmgw_timeout_seconds: int = 290

    # Paths
    runs_dir: Path = Path("runs")  # Base directory for timestamped runs

    # Agent defaults (CLI > .env > task YAML)
    default_agent_model: str | None = None
    default_permission_mode: str | None = None
    default_max_turns: int | None = None

    # API Backend routing
    api_backend: ApiBackend = ApiBackend.DIRECT

    # LLM Gateway Proxy settings (used when api_backend == "proxy")
    llmgw_proxy_vendor: str = "awsbedrock"
    llmgw_proxy_api_flavor: str = "invoke"

    # AWS Bedrock settings (used when api_backend == "bedrock")
    aws_bearer_token_bedrock: str | None = None
    aws_region: str | None = None
    bedrock_model: str | None = None  # Cross-region model ID
    bedrock_small_model: str | None = None  # Cross-region small model ID

    # Logging
    log_level: str = "INFO"  # Default log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    log_to_file: bool = False  # Whether to enable file logging

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    def _validate_llmgw_settings(self) -> None:
        """Validate that required LLM Gateway settings are present.

        Raises:
            ValueError: If required gateway settings are missing
        """
        missing = []
        if not self.llmgw_url:
            missing.append("LLMGW_URL")
        if not self.llmgw_client_id:
            missing.append("LLMGW_CLIENT_ID")
        if not self.llmgw_client_secret:
            missing.append("LLMGW_CLIENT_SECRET")
        if not self.llmgw_semantic_org_id:
            missing.append("LLMGW_SEMANTIC_ORG_ID")
        if not self.llmgw_semantic_tenant_id:
            missing.append("LLMGW_SEMANTIC_TENANT_ID")
        if missing:
            raise ValueError(
                f"LLM Gateway proxy is enabled but missing required settings: {', '.join(missing)}."
                + " Please set them in your .env file."
            )

    def _validate_bedrock_settings(self) -> None:
        """Validate that required AWS Bedrock settings are present.

        Raises:
            ValueError: If required Bedrock settings are missing
        """
        missing = []
        if not self.aws_bearer_token_bedrock:
            missing.append("AWS_BEARER_TOKEN_BEDROCK")
        if not self.aws_region:
            missing.append("AWS_REGION")
        if missing:
            raise ValueError(
                f"Bedrock routing is enabled but missing required settings: {', '.join(missing)}."
                + " Please set them in your .env file."
            )

    def validate_api_keys(self, agent_type: str) -> None:
        """Validate that required API keys are present.

        Args:
            agent_type: The type of agent being used

        Raises:
            ValueError: If required API key is missing
        """
        if self.api_backend == ApiBackend.BEDROCK:
            self._validate_bedrock_settings()
        elif self.api_backend == ApiBackend.PROXY:
            self._validate_llmgw_settings()

        # Claude Code agent can use either:
        # 1. ANTHROPIC_API_KEY environment variable
        # 2. Cached CLI authentication from 'claude-code login' (subscription account)
        # 3. LLM Gateway proxy (sets auth via proxy)
        # We don't validate the API key here because the SDK handles auth and fails clearly if missing.
        if agent_type == "claude-code":
            return


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
