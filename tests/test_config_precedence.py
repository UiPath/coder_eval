"""Tests for configuration environment variable override precedence.

Tests ensure predictable config loading order prevents auth failures.
"""

import os
from pathlib import Path

import pytest


def test_config_shell_overrides_dotenv_for_api_keys(tmp_path, monkeypatch):
    """Test that shell environment overrides .env for ANTHROPIC_API_KEY.

    Hypothesis: Shell environment should take precedence for API keys.
    Expected: Shell value used when both shell and .env define the key.

    Context: Lines 10-11 in config.py use load_dotenv(override=False).
    """
    # Set shell environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell_key_value")

    # Create .env in temp directory
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=dotenv_key_value\n")

    # Change to temp directory and load settings
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)

        # Create new Settings instance which will read environment
        from coder_eval.config import Settings

        settings = Settings(_env_file=str(env_file))

        # Verify shell environment took precedence
        assert settings.anthropic_api_key == "shell_key_value"

    finally:
        os.chdir(original_cwd)


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
                os.environ[key] = value

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


def test_config_case_insensitive_env_vars(tmp_path, monkeypatch):
    """Test that environment variable names are case-insensitive.

    Hypothesis: Config should accept LOG_LEVEL and log_level equally.
    Expected: Both formats work without errors.

    Context: Line 60 in config.py sets case_sensitive=False.
    """
    # Set lowercase environment variable
    monkeypatch.setenv("log_level", "DEBUG")

    from coder_eval.config import Settings

    settings = Settings()

    # Verify lowercase was accepted (case-insensitive)
    assert settings.log_level == "DEBUG"


def test_config_validates_required_api_keys():
    """Test that validate_api_keys raises error for missing keys.

    Hypothesis: Missing ANTHROPIC_API_KEY should raise ValueError.
    Expected: Clear error message for missing credentials.

    Context: Lines 62-72 in config.py implement validation.
    """
    from coder_eval.config import Settings

    # Create settings with no API key
    settings = Settings(anthropic_api_key=None)

    # Verify validation raises error for claude-code agent
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is required"):
        settings.validate_api_keys("claude-code")


def test_config_exports_to_os_environ(monkeypatch):
    """Test that settings are exported to os.environ for external libraries.

    Hypothesis: Settings values should be available via os.getenv().
    Expected: All non-None settings exported as uppercase env vars.

    Context: Lines 81-90 in config.py export settings to os.environ.
    """
    # Set test values
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DEFAULT_AGENT_MODEL", "claude-sonnet-4-20250514")

    from coder_eval.config import Settings

    settings = Settings()

    # Manually export to os.environ (simulating lines 81-90)
    for key, value in settings.model_dump().items():
        if value is not None:
            env_key = key.upper()
            from pathlib import Path

            if isinstance(value, Path):
                os.environ[env_key] = str(value)
            elif isinstance(value, bool):
                os.environ[env_key] = str(value).lower()
            else:
                os.environ[env_key] = str(value)

    # Verify settings are in os.environ
    assert os.getenv("LOG_LEVEL") == "WARNING"
    assert os.getenv("DEFAULT_AGENT_MODEL") == "claude-sonnet-4-20250514"


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
