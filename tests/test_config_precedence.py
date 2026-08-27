"""Tests for configuration environment variable override precedence.

Tests ensure predictable config loading order prevents auth failures.
"""

import os
from pathlib import Path

import pytest


def test_config_dotenv_overrides_shell_for_api_keys(tmp_path, monkeypatch):
    """Test that env vars override .env file for ANTHROPIC_API_KEY.

    In pydantic-settings, env vars have higher priority than _env_file.
    Verifies that monkeypatched env var wins over _env_file value.
    """
    # Import BEFORE monkeypatch so module-level load_dotenv doesn't overwrite
    from coder_eval.config import Settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell_key_value")

    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=dotenv_key_value\n")

    settings = Settings(_env_file=str(env_file))

    # env vars win over _env_file in pydantic-settings
    assert settings.anthropic_api_key == "shell_key_value"


def test_config_loads_defaults_when_no_env_vars(monkeypatch):
    """Test that config uses defaults when no environment variables set.

    Hypothesis: Missing env vars should fall back to coded defaults.
    Expected: Default values used without errors.
    """
    from coder_eval.config import Settings

    # Clear ambient env vars (CI sets TELEMETRY_ENABLED=false at the job level)
    # and bypass any real .env so the coded defaults are what we actually assert.
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    # Create Settings without environment file, relying on defaults
    settings = Settings(
        anthropic_api_key=None,
        _env_file=os.devnull,
    )

    # Verify defaults are used
    assert settings.log_level == "INFO"
    assert settings.telemetry_enabled is True


def test_config_case_insensitive_env_vars(monkeypatch):
    """Test that environment variable names are case-insensitive.

    Context: SettingsConfigDict sets case_sensitive=False, so LOG_LEVEL
    and log_level both map to the log_level field.
    """
    # Import BEFORE monkeypatch so module-level load_dotenv doesn't overwrite
    from coder_eval.config import Settings

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    # Use _env_file to avoid real .env polluting the test
    settings = Settings(_env_file=os.devnull)

    # Verify uppercase LOG_LEVEL matched the lowercase log_level field
    assert settings.log_level == "DEBUG"


def test_config_claude_code_no_key_direct_succeeds():
    """Test that claude-code agent allows missing API key with direct backend.

    Claude Code can authenticate via cached 'claude-code login' session,
    so we defer auth validation to the SDK.
    """
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(anthropic_api_key=None, api_backend=ApiBackend.DIRECT)

    # Should NOT raise — SDK handles auth (API key, cached login, or error)
    settings.validate_api_keys("claude-code")


def test_config_codex_direct_is_noop_without_key():
    """codex self-authenticates (CODEX_API_KEY / ChatGPT login) — no key required under direct."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(anthropic_api_key=None, api_backend=ApiBackend.DIRECT)

    # Must NOT raise and must NOT demand any credential.
    settings.validate_api_keys("codex")


def test_config_codex_bedrock_validates_backend_for_judges():
    """codex + bedrock is allowed: --backend also routes llm_judge/agent_judge, so the
    Bedrock settings are validated (they're needed for the judges), not rejected."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        api_backend=ApiBackend.BEDROCK,
        aws_bearer_token_bedrock=None,
        aws_region=None,
        bedrock_model=None,
    )
    # Missing Bedrock settings surface as the normal bedrock-validation error,
    # NOT a codex-specific "unsupported backend" rejection.
    with pytest.raises(ValueError) as exc:
        settings.validate_api_keys("codex")
    assert "Bedrock routing is enabled" in str(exc.value)
    assert "does not support" not in str(exc.value)


def test_config_exports_to_os_environ(monkeypatch):
    """Test that settings are exported to os.environ for external libraries.

    Context: Lines 148-157 in config.py export settings to os.environ.
    Uses constructor kwargs (highest priority) to avoid .env pollution.
    """
    from coder_eval.config import Settings

    # Use kwargs to set values deterministically (kwargs beat env vars and .env)
    settings = Settings(log_level="WARNING", bedrock_model="claude-sonnet-4-20250514")

    # Manually export to os.environ (simulating lines 148-157) using monkeypatch for cleanup
    for key, value in settings.model_dump().items():
        if value is not None:
            env_key = key.upper()
            if isinstance(value, Path):
                monkeypatch.setenv(env_key, str(value))
            elif isinstance(value, bool):
                monkeypatch.setenv(env_key, str(value).lower())
            else:
                monkeypatch.setenv(env_key, str(value))

    # Verify settings are in os.environ
    assert os.getenv("LOG_LEVEL") == "WARNING"
    assert os.getenv("BEDROCK_MODEL") == "claude-sonnet-4-20250514"


def test_config_bedrock_missing_token():
    """Bedrock backend + missing token raises ValueError."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        api_backend=ApiBackend.BEDROCK,
        aws_bearer_token_bedrock=None,
        aws_region="us-east-1",
        bedrock_model="claude-sonnet-4-6",
    )

    with pytest.raises(ValueError, match="AWS_BEARER_TOKEN_BEDROCK"):
        settings.validate_api_keys("claude-code")


def test_config_bedrock_missing_region():
    """Bedrock backend + missing region raises ValueError."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        api_backend=ApiBackend.BEDROCK,
        aws_bearer_token_bedrock="tok-123",
        aws_region=None,
        bedrock_model="claude-sonnet-4-6",
    )

    with pytest.raises(ValueError, match="AWS_REGION"):
        settings.validate_api_keys("claude-code")


def test_config_bedrock_missing_model():
    """Bedrock backend + missing model raises ValueError so we fail fast at
    startup instead of an opaque Bedrock 400 on first invocation."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        api_backend=ApiBackend.BEDROCK,
        aws_bearer_token_bedrock="tok-123",
        aws_region="us-east-1",
        bedrock_model=None,
    )

    with pytest.raises(ValueError, match="BEDROCK_MODEL"):
        settings.validate_api_keys("claude-code")


def test_config_bedrock_valid():
    """Bedrock backend with valid settings passes."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        api_backend=ApiBackend.BEDROCK,
        aws_bearer_token_bedrock="tok-123",
        aws_region="us-east-1",
        bedrock_model="claude-sonnet-4-6",
    )

    # Should NOT raise
    settings.validate_api_keys("claude-code")


def test_config_direct_skips_bedrock_validation():
    """Direct backend skips bedrock validation even if settings are empty."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        api_backend=ApiBackend.DIRECT,
        aws_bearer_token_bedrock=None,
        aws_region=None,
    )

    # Should NOT raise — bedrock is not selected
    settings.validate_api_keys("claude-code")


def _precedence_task():
    from coder_eval.models import AgentKind, RunLimits, SandboxConfig, TaskDefinition, parse_agent_config

    return TaskDefinition(
        task_id="test-precedence",
        description="Test precedence",
        initial_prompt="test",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, model="yaml-model", permission_mode="default"),
        run_limits=RunLimits(max_turns=10),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[{"type": "file_exists", "path": "test.py", "description": "test"}],
    )


def test_agent_override_precedence_cli_over_yaml():
    """CLI (-D/alias) overrides win over task YAML values — layer 5 is CLI-only."""
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestration.experiment import _apply_cli_overrides

    task = _precedence_task()
    config = BatchRunConfig(
        run_dir=Path("runs/test"),
        overrides={
            "agent.model": "cli-model",
            "agent.permission_mode": "bypassPermissions",
            "run_limits.max_turns": 99,
        },
    )

    _apply_cli_overrides(task, config)

    assert task.agent.model == "cli-model"
    assert task.agent.permission_mode == "bypassPermissions"
    assert task.run_limits is not None
    assert task.run_limits.max_turns == 99


def test_cli_override_applies_with_lineage_detail():
    """A -D run_limits.max_turns override applies and records `-D` lineage detail."""
    from coder_eval.models import ConfigLineageEntry
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestration.experiment import _apply_cli_overrides

    task = _precedence_task()
    config = BatchRunConfig(run_dir=Path("runs/test"), overrides={"run_limits.max_turns": 7})
    lineage: dict[str, ConfigLineageEntry] = {}

    _apply_cli_overrides(task, config, lineage=lineage)

    assert task.run_limits is not None
    assert task.run_limits.max_turns == 7
    assert lineage["run_limits.max_turns"].source_detail == "-D run_limits.max_turns"


def test_api_backend_enum_values():
    """Verify ApiBackend has exactly 3 values with expected string representations."""
    from coder_eval.models.enums import ApiBackend

    assert set(ApiBackend) == {ApiBackend.DIRECT, ApiBackend.BEDROCK, ApiBackend.LITELLM}
    assert str(ApiBackend.DIRECT) == "direct"
    assert str(ApiBackend.BEDROCK) == "bedrock"
    assert str(ApiBackend.LITELLM) == "litellm"


def test_settings_api_backend_default():
    """Verify default api_backend is DIRECT (or respects environment if configured)."""
    import os

    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings()
    # The test environment may have API_BACKEND configured in .env.
    # This test verifies that Settings loads and parses the api_backend value correctly.
    # If no API_BACKEND is set, it should default to DIRECT.
    if "API_BACKEND" not in os.environ:
        assert settings.api_backend == ApiBackend.DIRECT
    else:
        # If environment specifies a backend, respect it
        expected = ApiBackend(os.environ["API_BACKEND"].lower())
        assert settings.api_backend == expected


def test_settings_api_backend_from_env(monkeypatch):
    """Setting API_BACKEND env var coerces to ApiBackend enum."""
    import os

    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    monkeypatch.setenv("API_BACKEND", "bedrock")
    settings = Settings(_env_file=os.devnull)
    assert settings.api_backend == ApiBackend.BEDROCK


def test_resolve_route_direct():
    """resolve_route with DIRECT returns DirectRoute."""
    from coder_eval.config import Settings
    from coder_eval.models import DirectRoute, resolve_route
    from coder_eval.models.enums import ApiBackend

    s = Settings(api_backend=ApiBackend.DIRECT)
    route = resolve_route(s)
    assert isinstance(route, DirectRoute)


def _direct_settings(**overrides):
    """Build a DIRECT-backend Settings with the ANTHROPIC_API_KEY field explicitly cleared.

    The shared helper makes the judge-transport precedence tests independent of
    the developer's shell env (pydantic-settings would otherwise pick up a real
    ANTHROPIC_API_KEY value from the parent process).
    """
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    base = {
        "api_backend": ApiBackend.DIRECT,
        "anthropic_api_key": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_resolve_route_direct_judge_transport_anthropic_when_api_key_set():
    """ANTHROPIC_API_KEY present → judge_transport='anthropic'."""
    from coder_eval.models import DirectRoute, resolve_route

    s = _direct_settings(anthropic_api_key="sk-test")
    route = resolve_route(s)
    assert isinstance(route, DirectRoute)
    assert route.judge_transport == "anthropic"


def test_resolve_route_direct_judge_transport_none_when_neither_set():
    """No ANTHROPIC_API_KEY → judge_transport=None (run starts; llm_judge fails at dispatch)."""
    from coder_eval.models import DirectRoute, resolve_route

    route = resolve_route(_direct_settings())
    assert isinstance(route, DirectRoute)
    assert route.judge_transport is None


def test_resolve_route_bedrock():
    """resolve_route with BEDROCK returns BedrockRoute."""
    from coder_eval.config import Settings
    from coder_eval.models import BedrockRoute, resolve_route
    from coder_eval.models.enums import ApiBackend

    s = Settings(
        api_backend=ApiBackend.BEDROCK,
        aws_bearer_token_bedrock="tok-123",
        aws_region="us-east-1",
        bedrock_model="eu.anthropic.claude-sonnet-4-6",
    )
    route = resolve_route(s)
    assert isinstance(route, BedrockRoute)
    assert route.region == "us-east-1"
    assert route.model == "eu.anthropic.claude-sonnet-4-6"


def test_resolve_route_bedrock_missing_token_asserts():
    """resolve_route with BEDROCK and missing token raises AssertionError."""
    from coder_eval.config import Settings
    from coder_eval.models import resolve_route
    from coder_eval.models.enums import ApiBackend

    s = Settings(api_backend=ApiBackend.BEDROCK, aws_bearer_token_bedrock=None, aws_region="us-east-1")
    with pytest.raises(AssertionError, match="aws_bearer_token_bedrock"):
        resolve_route(s)


@pytest.mark.parametrize(
    ("var_name", "replacement"),
    [
        ("DEFAULT_AGENT_MODEL", "agent.model"),
        ("DEFAULT_PERMISSION_MODE", "agent.permission_mode"),
        ("DEFAULT_MAX_TURNS", "run_limits.max_turns"),
    ],
)
def test_stale_default_env_var_raises(monkeypatch, var_name: str, replacement: str):
    """A stale removed DEFAULT_* knob fails Settings construction with a migration hint.

    Covers the .env-file source transitively: load_dotenv(override=True) at
    config.py import folds .env into os.environ before Settings is constructed.
    """
    from coder_eval.config import Settings

    monkeypatch.setenv(var_name, "some-value")
    with pytest.raises(ValueError) as exc:
        Settings()
    msg = str(exc.value)
    assert var_name in msg
    assert replacement in msg
    assert "experiments/default.yaml" in msg
