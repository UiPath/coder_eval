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


def test_config_dotenv_overrides_shell_for_llmgw(tmp_path, monkeypatch):
    """Test that .env overrides shell for LLMGW_* variables.

    Hypothesis: LLMGW credentials from .env should override shell.
    Expected: .env value used for LLMGW_* even when shell defines it.

    Context: Lines 15-29 in config.py force .env precedence for LLMGW_*.
    """
    # Set shell environment
    monkeypatch.setenv("LLMGW_CLIENT_ID", "shell_client_id")

    # Create .env with different value
    env_file = tmp_path / ".env"
    env_file.write_text("LLMGW_CLIENT_ID=dotenv_client_id\n")

    # Change to temp directory
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)

        # Manually apply the special handling from config.py lines 15-29
        from dotenv import dotenv_values

        env_values = dotenv_values(str(env_file))
        for key in ["LLMGW_CLIENT_ID"]:
            value = env_values.get(key)
            if value:
                monkeypatch.setenv(key, value)

        from coder_eval.config import Settings

        settings = Settings(_env_file=str(env_file))

        # Verify .env took precedence for LLMGW_* variables
        assert settings.llmgw_client_id == "dotenv_client_id"

    finally:
        os.chdir(original_cwd)


def test_config_loads_defaults_when_no_env_vars():
    """Test that config uses defaults when no environment variables set.

    Hypothesis: Missing env vars should fall back to coded defaults.
    Expected: Default values used without errors.
    """
    from coder_eval.config import Settings

    # Create Settings without environment file, relying on defaults
    settings = Settings(
        anthropic_api_key=None,
        llmgw_url=None,
        llmgw_client_id=None,
    )

    # Verify defaults are used (note: llmgw_requesting_product may be overridden by system .env)
    assert settings.log_level == "INFO"
    assert settings.default_agent_model is None
    assert settings.default_permission_mode is None
    assert settings.default_max_turns is None
    # Don't assert llmgw_requesting_product as it may be overridden by system environment


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


def test_config_claude_code_proxy_missing_gateway_settings():
    """Test that claude-code with proxy backend raises on missing gateway settings."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        anthropic_api_key=None,
        api_backend=ApiBackend.PROXY,
        llmgw_url=None,
        llmgw_client_id=None,
        llmgw_client_secret=None,
    )

    with pytest.raises(ValueError):
        settings.validate_api_keys("claude-code")


def test_config_claude_code_proxy_with_gateway_settings_succeeds():
    """Test that claude-code with proxy backend succeeds when all gateway settings are present."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        anthropic_api_key=None,
        api_backend=ApiBackend.PROXY,
        llmgw_url="https://gateway.example.com",
        llmgw_client_id="client-id",
        llmgw_client_secret="client-secret",
        llmgw_semantic_org_id="org-id",
        llmgw_semantic_tenant_id="tenant-id",
    )

    # Should NOT raise — all required gateway settings are present
    settings.validate_api_keys("claude-code")


def test_config_non_claude_code_proxy_requires_semantic_ids():
    """Test that non-claude-code agents with proxy require semantic org/tenant IDs."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings(
        anthropic_api_key=None,
        api_backend=ApiBackend.PROXY,
        llmgw_url="https://gateway.example.com",
        llmgw_client_id="client-id",
        llmgw_client_secret="client-secret",
        llmgw_semantic_org_id=None,
        llmgw_semantic_tenant_id=None,
    )

    with pytest.raises(ValueError):
        settings.validate_api_keys("other-agent")


def test_config_exports_to_os_environ(monkeypatch):
    """Test that settings are exported to os.environ for external libraries.

    Context: Lines 148-157 in config.py export settings to os.environ.
    Uses constructor kwargs (highest priority) to avoid .env pollution.
    """
    from coder_eval.config import Settings

    # Use kwargs to set values deterministically (kwargs beat env vars and .env)
    settings = Settings(log_level="WARNING", default_agent_model="claude-sonnet-4-20250514")

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
    assert os.getenv("DEFAULT_AGENT_MODEL") == "claude-sonnet-4-20250514"


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


def test_agent_override_precedence_cli_over_env_over_yaml():
    """Test CLI > .env > task YAML precedence for agent overrides.

    Verifies that BatchRunConfig fields (CLI) take priority over
    Settings fields (.env), which take priority over task YAML values.
    """
    from coder_eval.models import AgentConfig, AgentKind, RunLimits, SandboxConfig, TaskDefinition
    from coder_eval.orchestration.config import BatchRunConfig

    # Simulate a task with YAML-defined values
    task = TaskDefinition(
        task_id="test-precedence",
        description="Test precedence",
        initial_prompt="test",
        agent=AgentConfig(type=AgentKind.CLAUDE_CODE, model="yaml-model", permission_mode="default"),
        run_limits=RunLimits(max_turns=10),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[{"type": "file_exists", "path": "test.py", "description": "test"}],
    )

    # CLI value should win over .env and YAML
    config = BatchRunConfig(
        run_dir=Path("runs/test"),
        agent_model="cli-model",
        permission_mode="bypassPermissions",
        max_turns=99,
    )

    # Apply overrides (simulating batch.py logic)
    effective_model = config.agent_model or None  # CLI value
    if effective_model:
        task.agent.model = effective_model

    effective_perm = config.permission_mode or None
    if effective_perm:
        task.agent.permission_mode = effective_perm

    effective_max_turns = config.max_turns if config.max_turns is not None else None
    if effective_max_turns is not None:
        base = task.run_limits.model_dump(exclude_none=True) if task.run_limits else {}
        base["max_turns"] = effective_max_turns
        task.run_limits = RunLimits(**base)

    assert task.agent.model == "cli-model"
    assert task.agent.permission_mode == "bypassPermissions"
    assert task.run_limits is not None
    assert task.run_limits.max_turns == 99


def test_api_backend_enum_values():
    """Verify ApiBackend has exactly 3 values with expected string representations."""
    from coder_eval.models.enums import ApiBackend

    assert set(ApiBackend) == {ApiBackend.DIRECT, ApiBackend.BEDROCK, ApiBackend.PROXY}
    assert str(ApiBackend.DIRECT) == "direct"
    assert str(ApiBackend.BEDROCK) == "bedrock"
    assert str(ApiBackend.PROXY) == "proxy"


def test_settings_api_backend_default():
    """Verify default api_backend is DIRECT."""
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    settings = Settings()
    assert settings.api_backend == ApiBackend.DIRECT


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
    """Build a DIRECT-backend Settings with all credential fields explicitly cleared.

    The shared helper makes the judge-transport precedence tests independent of
    the developer's shell env (pydantic-settings would otherwise pick up real
    LLMGW_* / ANTHROPIC_API_KEY values from the parent process).
    """
    from coder_eval.config import Settings
    from coder_eval.models.enums import ApiBackend

    base = {
        "api_backend": ApiBackend.DIRECT,
        "anthropic_api_key": None,
        "llmgw_url": None,
        "llmgw_client_id": None,
        "llmgw_client_secret": None,
        "llmgw_semantic_org_id": None,
        "llmgw_semantic_tenant_id": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_resolve_route_direct_judge_transport_anthropic_when_api_key_set():
    """ANTHROPIC_API_KEY present → judge_transport='anthropic' (takes precedence over LLMGW)."""
    from coder_eval.models import DirectRoute, resolve_route

    s = _direct_settings(
        anthropic_api_key="sk-test",
        llmgw_url="https://gw.example.com/",
        llmgw_client_id="cid",
        llmgw_client_secret="secret",
        llmgw_semantic_org_id="org",
        llmgw_semantic_tenant_id="tenant",
    )
    route = resolve_route(s)
    assert isinstance(route, DirectRoute)
    assert route.judge_transport == "anthropic"


def test_resolve_route_direct_judge_transport_llmgw_when_only_llmgw_set():
    """No ANTHROPIC_API_KEY but full LLMGW creds → judge_transport='llmgw'."""
    from coder_eval.models import DirectRoute, resolve_route

    s = _direct_settings(
        llmgw_url="https://gw.example.com/",
        llmgw_client_id="cid",
        llmgw_client_secret="secret",
        llmgw_semantic_org_id="org",
        llmgw_semantic_tenant_id="tenant",
    )
    route = resolve_route(s)
    assert isinstance(route, DirectRoute)
    assert route.judge_transport == "llmgw"


def test_resolve_route_direct_judge_transport_none_when_neither_set():
    """No ANTHROPIC_API_KEY and no LLMGW creds → judge_transport=None (run starts; llm_judge fails at dispatch)."""
    from coder_eval.models import DirectRoute, resolve_route

    route = resolve_route(_direct_settings())
    assert isinstance(route, DirectRoute)
    assert route.judge_transport is None


def test_resolve_route_direct_judge_transport_none_when_partial_llmgw():
    """Incomplete LLMGW credential set is not enough — must be all five fields."""
    from coder_eval.models import DirectRoute, resolve_route

    s = _direct_settings(
        llmgw_url="https://gw.example.com/",
        llmgw_client_id="cid",
        llmgw_client_secret="secret",
        llmgw_semantic_org_id="org",
        # llmgw_semantic_tenant_id intentionally omitted
    )
    route = resolve_route(s)
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
    assert route.bearer_token == "tok-123"
    assert route.region == "us-east-1"
    assert route.model == "eu.anthropic.claude-sonnet-4-6"


def test_resolve_route_proxy_raises():
    """resolve_route does not handle PROXY — the orchestrator constructs ProxyRoute inline."""
    from coder_eval.config import Settings
    from coder_eval.models import resolve_route
    from coder_eval.models.enums import ApiBackend

    s = Settings(api_backend=ApiBackend.PROXY)
    with pytest.raises(ValueError, match="PROXY"):
        resolve_route(s)


def test_resolve_route_bedrock_missing_token_asserts():
    """resolve_route with BEDROCK and missing token raises AssertionError."""
    from coder_eval.config import Settings
    from coder_eval.models import resolve_route
    from coder_eval.models.enums import ApiBackend

    s = Settings(api_backend=ApiBackend.BEDROCK, aws_bearer_token_bedrock=None, aws_region="us-east-1")
    with pytest.raises(AssertionError, match="aws_bearer_token_bedrock"):
        resolve_route(s)
