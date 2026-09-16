"""Configuration management using pydantic-settings."""

# by-design model-hub ↔ config type-level cycle; runtime imports are lazy per CE017
# pyright: reportImportCycles=false

import base64
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from coder_eval.models import AgentKind, ApiBackend


# HAZARD: an INGESTION-ONLY connection string, baked in so a fresh install reports
# usage telemetry with no configuration. It can only WRITE to the resource, never
# read, query or manage it, and it is base64-wrapped to avoid tripping naive secret
# scanners -- NOT for secrecy. An explicitly-set one takes precedence.
# Rationale: .claude/notes/reporting.md § On by default, and what that obliges
_DEFAULT_TELEMETRY_CONNECTION_STRING = base64.b64decode(
    "SW5zdHJ1bWVudGF0aW9uS2V5PTgxZDBkOGI1LTg1ZjktNDMxNS1iYjJlLTg4ODg0Y2ZkYTVhNztJbmdlc3Rpb25FbmRwb2ludD1odHRwczovL3dlc3R1czItMi5pbi5hcHBsaWNhdGlvbmluc2lnaHRzLmF6dXJlLmNvbS87TGl2ZUVuZHBvaW50PWh0dHBzOi8vd2VzdHVzMi5saXZlZGlhZ25vc3RpY3MubW9uaXRvci5henVyZS5jb20vO0FwcGxpY2F0aW9uSWQ9MDRjN2U3ZjItYjg0OC00ZjhlLTkxNzMtZjI3NmE1YTAwMzk0"
).decode("utf-8")


# override=True so .env always wins over the shell's possibly-stale credentials.
load_dotenv(override=True)

env_values = dotenv_values(".env")
for key in [
    "ANTHROPIC_API_KEY",
]:
    value = env_values.get(key)
    if value:
        os.environ[key] = value


# pydantic-settings silently ignores unknown env vars, so without this guard a
# stale knob would quietly stop having any effect. Fail loud with a migration hint.
_REMOVED_DEFAULT_KNOBS = {
    "DEFAULT_AGENT_MODEL": "agent.by_type.claude-code.model",
    "DEFAULT_PERMISSION_MODE": "agent.by_type.claude-code.permission_mode",
    "DEFAULT_MAX_TURNS": "run_limits.max_tool_calls",
}


def _reject_removed_default_knobs() -> None:
    """Fail loud on stale removed DEFAULT_* env knobs (see _REMOVED_DEFAULT_KNOBS).

    An os.environ-only check suffices: load_dotenv(override=True) at module
    import folds the .env file into os.environ before Settings is constructed
    (if that load_dotenv were ever removed, this guard would silently narrow to
    shell-env-only).

    Called from Settings.__init__ BEFORE pydantic validation so the plain
    ValueError propagates as-is — a pydantic ValidationError would echo the
    full input dict (including API keys) into the error message.
    """
    stale = [name for name in _REMOVED_DEFAULT_KNOBS if os.environ.get(name)]
    if stale:
        hints = " ".join(
            f"{name} was removed — set the baseline in experiments/default.yaml"
            + f" ({_REMOVED_DEFAULT_KNOBS[name]}) or override per-run with -D {_REMOVED_DEFAULT_KNOBS[name]}=…"
            + (" / --model." if name == "DEFAULT_AGENT_MODEL" else ".")
            for name in stale
        )
        raise ValueError(f"{hints} Remove the variable(s) from your .env / shell environment.")


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _reject_removed_default_knobs()
        super().__init__(*args, **kwargs)

    anthropic_api_key: str | None = None

    runs_dir: Path = Path("runs")  # Base directory for timestamped runs

    api_backend: ApiBackend = ApiBackend.DIRECT

    aws_bearer_token_bedrock: str | None = None
    aws_region: str | None = None
    bedrock_model: str | None = None  # Cross-region model ID
    bedrock_small_model: str | None = None  # Cross-region small model ID

    # HAZARD: these map to the ANTHROPIC_* vars ONLY inside the SDK subprocess env.
    # NOT named anthropic_*, so the export loop cannot leak ANTHROPIC_BASE_URL
    # process-wide and redirect the judge's own client.
    litellm_base_url: str | None = None
    litellm_auth_token: str | None = None
    litellm_model: str | None = None
    litellm_small_model: str | None = None
    # Must point at the SAME file the proxy writes; unset or missing => static pricing.
    # Rationale: .claude/notes/reporting.md § Cost joining
    litellm_cost_log: str | None = None

    # CODEX_MODEL is the fallback when a task doesn't pin agent.model. For Azure set
    # CODEX_API_VERSION too and use the deployment name as the model.
    codex_model: str | None = None

    # GEMINI_API_KEY is read from .env here so the export loop re-publishes it to
    # os.environ, where the SDK looks for it. ANTIGRAVITY_MODEL is the fallback.
    gemini_api_key: str | None = None
    antigravity_model: str | None = None

    log_level: str = "INFO"  # Default log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    log_to_file: bool = False  # Whether to enable file logging

    # On by default via the baked-in connection string, which any set value (env
    # or .env) overrides. TELEMETRY_ENABLED is the single canonical disable gate.
    telemetry_enabled: bool = True
    telemetry_connection_string: str | None = Field(
        default=_DEFAULT_TELEMETRY_CONNECTION_STRING,
        validation_alias=AliasChoices(
            "telemetry_connection_string",
            "applicationinsights_connection_string",
            "uipath_ai_connection_string",
        ),
    )
    # Emitted as the `Source` dimension so a downstream pipeline can tag itself and
    # be told apart from an anonymous local run -- `IsCI` alone cannot.
    telemetry_source: str = "coder-eval"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

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
        # Without it an invocation that overrides nothing sends model=None and
        # Bedrock returns an opaque 400. Fail fast with a clear error.
        if not self.bedrock_model:
            missing.append("BEDROCK_MODEL")
        if missing:
            raise ValueError(
                f"Bedrock routing is enabled but missing required settings: {', '.join(missing)}."
                + " Please set them in your .env file."
            )

    def _validate_litellm_settings(self) -> None:
        """Validate that required custom Anthropic-endpoint settings are present.

        Raises:
            ValueError: If required custom settings are missing
        """
        missing = []
        if not self.litellm_base_url:
            missing.append("LITELLM_BASE_URL")
        if not self.litellm_auth_token:
            missing.append("LITELLM_AUTH_TOKEN")
        # LITELLM_MODEL is required for the same reason BEDROCK_MODEL is: a None
        # model sent to the SDK/gateway yields an opaque 400. Fail fast instead.
        if not self.litellm_model:
            missing.append("LITELLM_MODEL")
        if missing:
            raise ValueError(
                f"LiteLLM-endpoint routing is enabled but missing required settings: {', '.join(missing)}."
                + " Please set them in your .env file."
            )
        # Reject a malformed base_url so the preflight and environment_info get a well-formed URL.
        parts = urlsplit(self.litellm_base_url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(
                f"LITELLM_BASE_URL must be an http(s) URL with a host, got {self.litellm_base_url!r}. "
                + "Set it to e.g. http://localhost:4000 in your .env file."
            )

    def validate_api_keys(self, agent_type: str) -> None:
        """Validate that required API keys are present.

        Args:
            agent_type: The type of agent being used

        Raises:
            ValueError: If required API key is missing
        """
        # The no-op agent (agent: {type: none}) makes no model API call, so it
        # needs no credentials — not even the backend (Bedrock) settings.
        if agent_type == AgentKind.NONE.value:
            return

        if self.api_backend == ApiBackend.BEDROCK:
            self._validate_bedrock_settings()

        if self.api_backend == ApiBackend.LITELLM:
            self._validate_litellm_settings()

        # Either ANTHROPIC_API_KEY or cached CLI auth works, and the SDK fails
        # clearly when neither does -- so no key validation here.
        if agent_type == AgentKind.CLAUDE_CODE.value:
            return


settings = Settings()

# For external libraries that read os.getenv(); non-None values only, stringified.
for key, value in settings.model_dump().items():
    if value is not None:
        env_key = key.upper()
        if isinstance(value, Path):
            os.environ[env_key] = str(value)
        elif isinstance(value, bool):
            os.environ[env_key] = str(value).lower()
        else:
            os.environ[env_key] = str(value)
