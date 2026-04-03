"""Tests for API routing: _build_sdk_env() and route dataclasses."""

from claude_agent_sdk import ClaudeAgentOptions

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent, _dump_sdk_options
from coder_eval.models import BedrockRoute, DirectRoute, ProxyRoute


class TestBuildSdkEnv:
    """Test ClaudeCodeAgent._build_sdk_env() for all route types."""

    def test_direct_forwards_path_when_set(self, monkeypatch):
        """DirectRoute forwards PATH when set in parent environment."""
        custom_path = "/custom/bin:/usr/bin"
        monkeypatch.setenv("PATH", custom_path)
        env, model = ClaudeCodeAgent._build_sdk_env(DirectRoute())
        assert env["PATH"] == custom_path
        assert model is None

    def test_direct_omits_path_when_unset(self, monkeypatch):
        """DirectRoute omits PATH if not set in parent environment."""
        monkeypatch.delenv("PATH", raising=False)
        env, model = ClaudeCodeAgent._build_sdk_env(DirectRoute())
        assert "PATH" not in env
        assert model is None

    def test_proxy_returns_base_url_and_dummy_key(self, monkeypatch):
        """ProxyRoute produces ANTHROPIC_BASE_URL, dummy API key, and forwards PATH."""
        custom_path = "/proxy/bin:/usr/bin"
        monkeypatch.setenv("PATH", custom_path)
        env, _ = ClaudeCodeAgent._build_sdk_env(ProxyRoute(port=8080))
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8080"
        assert env["ANTHROPIC_API_KEY"] == "llmgw-proxy"
        assert env["PATH"] == custom_path

    def test_proxy_no_model_override(self):
        """ProxyRoute does not override model."""
        _, model = ClaudeCodeAgent._build_sdk_env(ProxyRoute(port=9999))
        assert model is None

    def test_bedrock_basic_env(self, monkeypatch):
        """BedrockRoute produces CLAUDE_CODE_USE_BEDROCK, token, region, and forwards PATH."""
        custom_path = "/bedrock/bin:/usr/bin"
        monkeypatch.setenv("PATH", custom_path)
        route = BedrockRoute(bearer_token="tok-123", region="us-east-1")
        env, _ = ClaudeCodeAgent._build_sdk_env(route)
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert env["AWS_BEARER_TOKEN_BEDROCK"] == "tok-123"
        assert env["AWS_REGION"] == "us-east-1"
        assert env["PATH"] == custom_path

    def test_bedrock_attribution_header_disabled(self):
        """disable_attribution_header=True sets CLAUDE_CODE_ATTRIBUTION_HEADER=0."""
        route = BedrockRoute(bearer_token="t", region="r", disable_attribution_header=True)
        env, _ = ClaudeCodeAgent._build_sdk_env(route)
        assert env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"

    def test_bedrock_attribution_header_enabled(self):
        """disable_attribution_header=False omits the header key."""
        route = BedrockRoute(bearer_token="t", region="r", disable_attribution_header=False)
        env, _ = ClaudeCodeAgent._build_sdk_env(route)
        assert "CLAUDE_CODE_ATTRIBUTION_HEADER" not in env

    def test_bedrock_model_override(self):
        """BedrockRoute.model returned as effective_model."""
        route = BedrockRoute(bearer_token="t", region="r", model="eu.anthropic.claude-sonnet-4-6")
        env, model = ClaudeCodeAgent._build_sdk_env(route)
        assert model == "eu.anthropic.claude-sonnet-4-6"
        assert env["ANTHROPIC_MODEL"] == "eu.anthropic.claude-sonnet-4-6"

    def test_bedrock_no_model_returns_none(self):
        """BedrockRoute without model returns None (use task config)."""
        route = BedrockRoute(bearer_token="t", region="r")
        env, model = ClaudeCodeAgent._build_sdk_env(route)
        assert model is None
        assert "ANTHROPIC_MODEL" not in env

    def test_bedrock_default_disables_attribution_header(self):
        """Default BedrockRoute (no explicit disable_attribution_header) sets CLAUDE_CODE_ATTRIBUTION_HEADER=0."""
        route = BedrockRoute(bearer_token="t", region="r")
        env, _ = ClaudeCodeAgent._build_sdk_env(route)
        assert env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"

    def test_bedrock_small_model(self):
        """BedrockRoute.small_model appears in env as ANTHROPIC_SMALL_FAST_MODEL."""
        route = BedrockRoute(bearer_token="t", region="r", small_model="eu.anthropic.claude-haiku-4-5")
        env, _ = ClaudeCodeAgent._build_sdk_env(route)
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "eu.anthropic.claude-haiku-4-5"


class TestSdkOptionsDumpRedaction:
    """Test that _dump_sdk_options redacts sensitive values."""

    def test_bedrock_token_redacted_in_dump(self):
        """AWS_BEARER_TOKEN_BEDROCK must not appear in plain text in sdk_options dump."""
        route = BedrockRoute(bearer_token="SECRET_TOKEN_123", region="us-east-1")
        env, _ = ClaudeCodeAgent._build_sdk_env(route)
        opts = ClaudeAgentOptions(cwd="/tmp", env=env)
        dump = _dump_sdk_options(opts)
        env_dump = dump.get("env", {})
        assert env_dump.get("AWS_BEARER_TOKEN_BEDROCK") != "SECRET_TOKEN_123"

    def test_proxy_dummy_key_redacted_in_dump(self):
        """ANTHROPIC_API_KEY (even dummy) is redacted in sdk_options dump."""
        env, _ = ClaudeCodeAgent._build_sdk_env(ProxyRoute(port=8080))
        opts = ClaudeAgentOptions(cwd="/tmp", env=env)
        dump = _dump_sdk_options(opts)
        env_dump = dump.get("env", {})
        assert env_dump.get("ANTHROPIC_API_KEY") != "llmgw-proxy"
