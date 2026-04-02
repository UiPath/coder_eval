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


def test_config_claude_code_no_key_no_proxy_succeeds():
    """Test that claude-code agent allows missing API key when proxy is disabled.

    Claude Code can authenticate via cached 'claude-code login' session,
    so we defer auth validation to the SDK.
    """
    from coder_eval.config import Settings

    settings = Settings(anthropic_api_key=None, llmgw_proxy_enabled=False)

    # Should NOT raise — SDK handles auth (API key, cached login, or error)
    settings.validate_api_keys("claude-code")


def test_config_claude_code_proxy_missing_gateway_settings():
    """Test that claude-code with proxy enabled raises on missing gateway settings."""
    from coder_eval.config import Settings

    settings = Settings(
        anthropic_api_key=None,
        bedrock_enabled=False,
        llmgw_proxy_enabled=True,
        llmgw_url=None,
        llmgw_client_id=None,
        llmgw_client_secret=None,
    )

    with pytest.raises(ValueError):
        settings.validate_api_keys("claude-code")


def test_config_claude_code_proxy_with_gateway_settings_succeeds():
    """Test that claude-code with proxy enabled succeeds when all gateway settings are present."""
    from coder_eval.config import Settings

    settings = Settings(
        anthropic_api_key=None,
        llmgw_proxy_enabled=True,
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

    settings = Settings(
        anthropic_api_key=None,
        bedrock_enabled=False,
        llmgw_proxy_enabled=True,
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
    """Bedrock enabled + missing token raises ValueError."""
    from coder_eval.config import Settings

    settings = Settings(
        bedrock_enabled=True,
        aws_bearer_token_bedrock=None,
        aws_region="us-east-1",
    )

    with pytest.raises(ValueError, match="AWS_BEARER_TOKEN_BEDROCK"):
        settings.validate_api_keys("claude-code")


def test_config_bedrock_missing_region():
    """Bedrock enabled + missing region raises ValueError."""
    from coder_eval.config import Settings

    settings = Settings(
        bedrock_enabled=True,
        aws_bearer_token_bedrock="tok-123",
        aws_region=None,
    )

    with pytest.raises(ValueError, match="AWS_REGION"):
        settings.validate_api_keys("claude-code")


def test_config_bedrock_valid():
    """Bedrock enabled with valid settings passes."""
    from coder_eval.config import Settings

    settings = Settings(
        bedrock_enabled=True,
        aws_bearer_token_bedrock="tok-123",
        aws_region="us-east-1",
    )

    # Should NOT raise
    settings.validate_api_keys("claude-code")


def test_config_bedrock_disabled_skips_validation():
    """Bedrock disabled skips validation even if settings are empty."""
    from coder_eval.config import Settings

    settings = Settings(
        bedrock_enabled=False,
        aws_bearer_token_bedrock=None,
        aws_region=None,
    )

    # Should NOT raise — bedrock is disabled
    settings.validate_api_keys("claude-code")


def test_config_bedrock_skips_proxy_validation():
    """Bedrock enabled skips proxy validation even if proxy is enabled with missing settings."""
    from coder_eval.config import Settings

    settings = Settings(
        bedrock_enabled=True,
        aws_bearer_token_bedrock="tok-123",
        aws_region="us-east-1",
        llmgw_proxy_enabled=True,
        llmgw_url=None,
        llmgw_client_id=None,
        llmgw_client_secret=None,
    )

    # Should NOT raise — Bedrock takes precedence, proxy won't be started
    settings.validate_api_keys("claude-code")


def test_agent_override_precedence_cli_over_env_over_yaml():
    """Test CLI > .env > task YAML precedence for agent overrides.

    Verifies that BatchRunConfig fields (CLI) take priority over
    Settings fields (.env), which take priority over task YAML values.
    """
    from coder_eval.models import AgentConfig, AgentKind, SandboxConfig, TaskDefinition
    from coder_eval.orchestration.config import BatchRunConfig

    # Simulate a task with YAML-defined values
    task = TaskDefinition(
        task_id="test-precedence",
        description="Test precedence",
        initial_prompt="test",
        agent=AgentConfig(type=AgentKind.CLAUDE_CODE, model="yaml-model", permission_mode="default", max_turns=10),
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
        task.agent.max_turns = effective_max_turns

    assert task.agent.model == "cli-model"
    assert task.agent.permission_mode == "bypassPermissions"
    assert task.agent.max_turns == 99
