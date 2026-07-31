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


# Application Insights connection string baked into the application so a fresh
# install reports usage telemetry to the shared coder-eval resource with no
# configuration. An explicitly-set connection string (env or .env, via any of the
# field's aliases below) takes precedence — pydantic-settings always prefers an
# env value over a field default.
#
# This is an INGESTION-ONLY connection string (InstrumentationKey + IngestionEndpoint):
# it can only WRITE telemetry to the resource, never read/query/manage it — the same
# class of value embedded in every distributed telemetry client (VS Code, Azure CLI,
# gh, the UiPath CLI). Approved by security for embedding. It is base64-wrapped ONLY
# to avoid tripping naive secret scanners / push-protection and to mark it as an
# intentional, reviewed default — NOT for secrecy (base64 is trivially reversible).
# Residual risk is telemetry spoofing / ingestion-cost abuse, bounded by the resource
# being dedicated to coder-eval usage telemetry.
_DEFAULT_TELEMETRY_CONNECTION_STRING = base64.b64decode(
    "SW5zdHJ1bWVudGF0aW9uS2V5PTgxZDBkOGI1LTg1ZjktNDMxNS1iYjJlLTg4ODg0Y2ZkYTVhNztJbmdlc3Rpb25FbmRwb2ludD1odHRwczovL3dlc3R1czItMi5pbi5hcHBsaWNhdGlvbmluc2lnaHRzLmF6dXJlLmNvbS87TGl2ZUVuZHBvaW50PWh0dHBzOi8vd2VzdHVzMi5saXZlZGlhZ25vc3RpY3MubW9uaXRvci5henVyZS5jb20vO0FwcGxpY2F0aW9uSWQ9MDRjN2U3ZjItYjg0OC00ZjhlLTkxNzMtZjI3NmE1YTAwMzk0"
).decode("utf-8")


# Load .env file with override so .env values always win over shell environment
load_dotenv(override=True)

# For certain keys, we want .env values to take precedence over shell environment
# because the shell may have outdated/different credentials
env_values = dotenv_values(".env")
for key in [
    "ANTHROPIC_API_KEY",
]:
    value = env_values.get(key)
    if value:
        os.environ[key] = value


# Removed layer-5 `.env` knobs → their `-D` override paths. pydantic-settings
# silently ignores unknown env vars, so without an explicit guard a stale knob
# would silently stop having any effect — fail loud with a migration hint instead.
_REMOVED_DEFAULT_KNOBS = {
    "DEFAULT_AGENT_MODEL": "agent.model",
    "DEFAULT_PERMISSION_MODE": "agent.permission_mode",
    "DEFAULT_MAX_TURNS": "run_limits.max_turns",
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

    # API Keys (for Claude Code agent only)
    anthropic_api_key: str | None = None

    # Paths
    runs_dir: Path = Path("runs")  # Base directory for timestamped runs

    # API Backend routing
    api_backend: ApiBackend = ApiBackend.DIRECT

    # AWS Bedrock settings (used when api_backend == "bedrock")
    aws_bearer_token_bedrock: str | None = None
    aws_region: str | None = None
    bedrock_model: str | None = None  # Cross-region model ID
    bedrock_small_model: str | None = None  # Cross-region small model ID

    # LiteLLM (Anthropic-compatible) endpoint settings (used when api_backend == "litellm").
    # These map to ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL /
    # ANTHROPIC_SMALL_FAST_MODEL, but ONLY inside the SDK subprocess env (see
    # ClaudeCodeAgent._build_sdk_env). They are deliberately NOT named anthropic_*
    # so the os.environ export loop below can't leak ANTHROPIC_BASE_URL process-wide
    # (which would silently redirect the judge's in-process Anthropic() client).
    litellm_base_url: str | None = None
    litellm_auth_token: str | None = None
    litellm_model: str | None = None
    litellm_small_model: str | None = None
    # Path to the per-call cost/cache JSONL the LiteLLM proxy's cost_logger writes
    # (LITELLM_COST_LOG). Must point at the SAME file the proxy uses (see
    # litellm/start-litellm.sh). When set and the file exists, the harness joins each
    # call's ACTUAL OpenRouter cost + cache onto the turn (litellm_cost.apply_actual_cost),
    # overriding the static rate-card estimate; unset/missing => static pricing (fallback).
    litellm_cost_log: str | None = None

    # Codex settings (CodexAgent). CODEX_MODEL is the fallback model/deployment
    # used when a task doesn't pin agent.model; CODEX_BASE_URL routes to a custom
    # OpenAI-/responses-compatible endpoint (incl. Azure OpenAI). For Azure also
    # set CODEX_API_VERSION (the required ``api-version`` query param) and use the
    # deployment name as the model. CODEX_BASE_URL / CODEX_API_VERSION /
    # CODEX_API_KEY are read directly via os.getenv in the agent, not mirrored here.
    codex_model: str | None = None

    # Antigravity settings (AntigravityAgent — Google's Gemini coding harness).
    # GEMINI_API_KEY authenticates the local harness (read from .env here so the
    # export loop below re-publishes it to os.environ, where the google-antigravity
    # SDK looks for it). ANTIGRAVITY_MODEL is the fallback Gemini model used when a
    # task doesn't pin agent.model.
    gemini_api_key: str | None = None
    antigravity_model: str | None = None

    # Logging
    log_level: str = "INFO"  # Default log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    log_to_file: bool = False  # Whether to enable file logging

    # Usage telemetry (OpenTelemetry → Azure Application Insights customEvents).
    # On by default via the baked-in connection string; see coder_eval/telemetry.py.
    # telemetry_enabled (TELEMETRY_ENABLED) is the single canonical disable gate.
    telemetry_enabled: bool = True
    # Defaults to the embedded coder-eval resource; any set value (env or .env, via
    # the aliases below) overrides it — pydantic-settings prefers env over default.
    telemetry_connection_string: str | None = Field(
        default=_DEFAULT_TELEMETRY_CONNECTION_STRING,
        validation_alias=AliasChoices(
            "telemetry_connection_string",
            "applicationinsights_connection_string",
            "uipath_ai_connection_string",
        ),
    )
    # Caller-settable origin stamp (TELEMETRY_SOURCE), emitted as the `Source`
    # dimension on every event. Lets downstream pipelines tag themselves (e.g.
    # `nightly-vm` / `skill-eval`) so internal runs are distinguishable from
    # anonymous local ones — `IsCI` alone can't, and the framework's own CI is
    # muted. Defaults to "coder-eval" for a plain local install.
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
        # BEDROCK_MODEL is the route-level model source. Without it, an
        # invocation that doesn't override via --model / task.agent.model
        # would send model=None to the SDK and Bedrock would return an
        # opaque 400. Fail fast at startup with a clear error.
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
        # base_url is present (not in `missing`); reject a malformed one so the
        # downstream preflight (urlopen) and environment_info (urlparse hostname)
        # get a well-formed absolute URL instead of a raw ValueError / empty host.
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

        # Claude Code agent can use either:
        # 1. ANTHROPIC_API_KEY environment variable
        # 2. Cached CLI authentication from 'claude-code login' (subscription account)
        # We don't validate the API key here because the SDK handles auth and fails clearly if missing.
        if agent_type == AgentKind.CLAUDE_CODE.value:
            return


# Global settings instance
settings = Settings()

# Export settings to environment variables for external libraries (the Anthropic SDK,
# boto3/Bedrock) that use os.getenv() instead of reading from the Settings object.
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
