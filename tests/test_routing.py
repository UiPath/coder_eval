"""Tests for API routing: _build_sdk_env() and route dataclasses."""

from claude_agent_sdk import ClaudeAgentOptions

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent, _dump_sdk_options
from coder_eval.config import Settings
from coder_eval.models import AgentConfig, AgentKind, BedrockRoute, DirectRoute, ProxyRoute
from coder_eval.models.enums import ApiBackend
from coder_eval.models.routing import resolve_route, to_bedrock_inference_profile


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

    def test_path_prepend_prefixes_existing_path(self, monkeypatch):
        """path_prepend dirs are prepended (in order) to PATH, with parent PATH preserved after."""
        import os

        monkeypatch.setenv("PATH", "/parent/bin")
        env, _ = ClaudeCodeAgent._build_sdk_env(DirectRoute(), path_prepend=["/sandbox/mocks", "/sandbox/bins"])
        assert env["PATH"] == f"/sandbox/mocks{os.pathsep}/sandbox/bins{os.pathsep}/parent/bin"

    def test_path_prepend_works_when_parent_path_unset(self, monkeypatch):
        """path_prepend still produces a usable PATH when the parent has none."""
        import os

        monkeypatch.delenv("PATH", raising=False)
        env, _ = ClaudeCodeAgent._build_sdk_env(DirectRoute(), path_prepend=["/sandbox/mocks"])
        # Trailing pathsep is harmless (POSIX/Windows treat empty entries as "skip").
        assert env["PATH"] == f"/sandbox/mocks{os.pathsep}"

    def test_path_prepend_none_or_empty_leaves_path_alone(self, monkeypatch):
        """No prepend list (or an empty one) must not mutate PATH."""
        monkeypatch.setenv("PATH", "/parent/bin")
        env_none, _ = ClaudeCodeAgent._build_sdk_env(DirectRoute(), path_prepend=None)
        env_empty, _ = ClaudeCodeAgent._build_sdk_env(DirectRoute(), path_prepend=[])
        assert env_none["PATH"] == "/parent/bin"
        assert env_empty["PATH"] == "/parent/bin"


class TestToBedrockInferenceProfile:
    """Test to_bedrock_inference_profile() — vendor + region qualification."""

    def test_bare_alias_in_eu_region(self):
        """Claude alias gets both anthropic. and region. prefix."""
        assert to_bedrock_inference_profile("claude-sonnet-4-6", "eu-north-1") == ("eu.anthropic.claude-sonnet-4-6")

    def test_bare_alias_in_us_region(self):
        assert to_bedrock_inference_profile("claude-sonnet-4-6", "us-east-2") == ("us.anthropic.claude-sonnet-4-6")

    def test_anthropic_qualified_id_in_eu(self):
        """Vendor-qualified id only needs region prefix."""
        assert to_bedrock_inference_profile("anthropic.claude-sonnet-4-6", "eu-north-1") == (
            "eu.anthropic.claude-sonnet-4-6"
        )

    def test_apac_region(self):
        assert to_bedrock_inference_profile("claude-sonnet-4-6", "ap-southeast-1") == (
            "apac.anthropic.claude-sonnet-4-6"
        )

    def test_existing_region_prefix_left_untouched(self):
        """User-pinned eu./us./global. prefixes are not double-prefixed."""
        assert to_bedrock_inference_profile("global.anthropic.claude-sonnet-4-6", "eu-north-1") == (
            "global.anthropic.claude-sonnet-4-6"
        )
        assert to_bedrock_inference_profile("us.anthropic.claude-sonnet-4-6", "eu-north-1") == (
            "us.anthropic.claude-sonnet-4-6"
        )

    def test_unknown_region_passthrough(self):
        """Regions we don't know about still get the vendor qualifier but no region prefix."""
        assert to_bedrock_inference_profile("claude-sonnet-4-6", "ca-central-1") == ("anthropic.claude-sonnet-4-6")

    def test_none_inputs(self):
        assert to_bedrock_inference_profile(None, "us-east-2") is None
        assert to_bedrock_inference_profile("claude-sonnet-4-6", None) == "claude-sonnet-4-6"

    def test_strips_surrounding_whitespace(self):
        """Sloppy .env values like 'claude-sonnet-4-6 ' must not bypass the qualifier."""
        assert to_bedrock_inference_profile("  claude-sonnet-4-6  ", "eu-north-1") == ("eu.anthropic.claude-sonnet-4-6")

    def test_whitespace_only_input_returns_none(self):
        """Whitespace-only input must not produce a malformed prefix-only id like 'eu.'."""
        assert to_bedrock_inference_profile("   ", "eu-north-1") is None
        assert to_bedrock_inference_profile("\t\n", "us-east-2") is None

    def test_stale_region_prefix_passes_through_unchanged(self):
        """A pinned id with a stale region prefix is left alone (documented passthrough).

        It's the user's responsibility to keep BEDROCK_MODEL's region prefix in sync with
        AWS_REGION when they pin an explicit profile. Returning unchanged here lets a user
        intentionally pin a non-matching profile (e.g. global.* in an EU region).
        """
        assert to_bedrock_inference_profile("eu.anthropic.claude-sonnet-4-6", "us-east-2") == (
            "eu.anthropic.claude-sonnet-4-6"
        )


def _make_agent(route, *, config_model: str | None = None) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(AgentConfig(type=AgentKind.CLAUDE_CODE, model=config_model), route=route)


class TestResolveRouteBedrockModel:
    """resolve_route() reads only BEDROCK_MODEL; DEFAULT_AGENT_MODEL is resolved at the agent layer."""

    def test_unset_bedrock_model_leaves_route_model_none(self):
        """No BEDROCK_MODEL → BedrockRoute.model is None; agent layer fills it from task/CLI."""
        settings = Settings(
            api_backend=ApiBackend.BEDROCK,
            aws_bearer_token_bedrock="t",
            aws_region="eu-north-1",
            bedrock_model=None,
            default_agent_model=None,
        )
        route = resolve_route(settings)
        assert isinstance(route, BedrockRoute)
        assert route.model is None

    def test_default_agent_model_does_not_leak_into_route(self):
        """DEFAULT_AGENT_MODEL must not appear at the route layer — _apply_cli_overrides handles it."""
        settings = Settings(
            api_backend=ApiBackend.BEDROCK,
            aws_bearer_token_bedrock="t",
            aws_region="us-east-2",
            bedrock_model=None,
            default_agent_model="claude-opus-4-7",
        )
        route = resolve_route(settings)
        assert isinstance(route, BedrockRoute)
        assert route.model is None

    def test_bedrock_model_qualified(self):
        settings = Settings(
            api_backend=ApiBackend.BEDROCK,
            aws_bearer_token_bedrock="t",
            aws_region="us-east-2",
            bedrock_model="anthropic.claude-opus-4-7",
        )
        route = resolve_route(settings)
        assert isinstance(route, BedrockRoute)
        assert route.model == "us.anthropic.claude-opus-4-7"


class TestResolveEffectiveModel:
    """Test ClaudeCodeAgent._resolve_effective_model() precedence + env sync + region prefix."""

    def test_config_model_overrides_bedrock_route_model(self):
        """Task/CLI model wins over BEDROCK_MODEL and is synced into env."""
        route = BedrockRoute(bearer_token="t", region="us-east-2", model="us.anthropic.claude-sonnet-4-6")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route, config_model="global.anthropic.claude-sonnet-4-6")
        effective = agent._resolve_effective_model("global.anthropic.claude-sonnet-4-6", env, route_model)
        assert effective == "global.anthropic.claude-sonnet-4-6"
        assert env["ANTHROPIC_MODEL"] == "global.anthropic.claude-sonnet-4-6"

    def test_route_model_used_when_config_none(self):
        """BEDROCK_MODEL is the fallback when no task/CLI model is set."""
        route = BedrockRoute(bearer_token="t", region="us-east-2", model="us.anthropic.claude-sonnet-4-6")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route)
        effective = agent._resolve_effective_model(None, env, route_model)
        assert effective == "us.anthropic.claude-sonnet-4-6"
        assert env["ANTHROPIC_MODEL"] == "us.anthropic.claude-sonnet-4-6"

    def test_bare_config_model_auto_prefixes_for_bedrock(self):
        """A bare --model on a Bedrock route gets the region's inference-profile prefix."""
        route = BedrockRoute(bearer_token="t", region="eu-north-1", model="eu.anthropic.claude-sonnet-4-6")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route, config_model="anthropic.claude-sonnet-4-6")
        effective = agent._resolve_effective_model("anthropic.claude-sonnet-4-6", env, route_model)
        assert effective == "eu.anthropic.claude-sonnet-4-6"
        assert env["ANTHROPIC_MODEL"] == "eu.anthropic.claude-sonnet-4-6"

    def test_both_none_returns_none(self):
        """No model anywhere → None, no env mutation."""
        route = BedrockRoute(bearer_token="t", region="us-east-2")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route)
        effective = agent._resolve_effective_model(None, env, route_model)
        assert effective is None
        assert "ANTHROPIC_MODEL" not in env

    def test_config_model_injects_anthropic_model_when_route_has_none(self):
        """Bedrock route without BEDROCK_MODEL must still propagate config_model into env.

        The subprocess relies on ANTHROPIC_MODEL when ClaudeAgentOptions.model isn't honored
        by every Bedrock code path, so config_model must be injected into env on Bedrock even
        when _build_sdk_env left ANTHROPIC_MODEL absent.
        """
        route = BedrockRoute(bearer_token="t", region="eu-north-1")  # no model
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        assert "ANTHROPIC_MODEL" not in env  # precondition
        agent = _make_agent(route, config_model="claude-sonnet-4-6")
        effective = agent._resolve_effective_model("claude-sonnet-4-6", env, route_model)
        assert effective == "eu.anthropic.claude-sonnet-4-6"
        assert env["ANTHROPIC_MODEL"] == "eu.anthropic.claude-sonnet-4-6"

    def test_direct_route_does_not_inject_or_prefix(self):
        """DirectRoute: no ANTHROPIC_MODEL in env, no Bedrock prefix applied."""
        env, route_model = ClaudeCodeAgent._build_sdk_env(DirectRoute())
        agent = _make_agent(DirectRoute(), config_model="claude-opus-4-7")
        effective = agent._resolve_effective_model("claude-opus-4-7", env, route_model)
        assert effective == "claude-opus-4-7"
        assert "ANTHROPIC_MODEL" not in env


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
