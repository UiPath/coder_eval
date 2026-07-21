"""Tests for the custom Anthropic-compatible backend (LiteLLMRoute).

Covers Phase 1 (route resolution + config validation) and Phase 2 (SDK env
building + effective-model sync) of the open-weight support plan. Mirrors the
Bedrock equivalents in ``tests/test_routing.py``.
"""

from __future__ import annotations

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.config import Settings
from coder_eval.models import (
    AgentKind,
    LiteLLMRoute,
    parse_agent_config,
)
from coder_eval.models.enums import ApiBackend
from coder_eval.models.routing import ROUTE_NAMES, resolve_route


def _make_agent(route, *, config_model: str | None = None) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE, model=config_model), route=route)


class TestResolveRouteCustom:
    """resolve_route() builds a LiteLLMRoute for the CUSTOM backend."""

    def test_resolves_custom_route_with_all_fields(self):
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url="http://localhost:4000",
            litellm_auth_token="sk-master",
            litellm_model="deepseek.v3.2",
        )
        route = resolve_route(settings)
        assert isinstance(route, LiteLLMRoute)
        assert route.base_url == "http://localhost:4000"
        assert route.auth_token == "sk-master"
        assert route.model == "deepseek.v3.2"

    def test_small_model_falls_back_to_model(self):
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url="http://localhost:4000",
            litellm_auth_token="sk-master",
            litellm_model="deepseek.v3.2",
            litellm_small_model=None,
        )
        route = resolve_route(settings)
        assert isinstance(route, LiteLLMRoute)
        assert route.small_model == "deepseek.v3.2"
        assert route.small_model == route.model

    def test_explicit_small_model_wins(self):
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url="http://localhost:4000",
            litellm_auth_token="sk-master",
            litellm_model="zai.glm-5",
            litellm_small_model="deepseek.v3.2",
        )
        route = resolve_route(settings)
        assert isinstance(route, LiteLLMRoute)
        assert route.small_model == "deepseek.v3.2"

    def test_model_passed_verbatim_no_inference_profile(self):
        """The dotted Bedrock id must NOT get an eu./anthropic. prefix (unlike Bedrock)."""
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url="http://localhost:4000",
            litellm_auth_token="sk-master",
            litellm_model="zai.glm-5",
        )
        route = resolve_route(settings)
        assert isinstance(route, LiteLLMRoute)
        assert route.model == "zai.glm-5"


class TestRouteRegistration:
    def test_route_names_covers_custom(self):
        assert ROUTE_NAMES[LiteLLMRoute] == "litellm"

    def test_importable_from_models(self):
        from coder_eval.models import LiteLLMRoute as Imported

        assert Imported is LiteLLMRoute


class TestValidateApiKeysCustom:
    """validate_api_keys() fails fast on missing custom settings."""

    def test_missing_base_url_raises_naming_it(self):
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url=None,
            litellm_auth_token="sk-master",
            litellm_model="zai.glm-5",
        )
        with pytest.raises(ValueError, match="LITELLM_BASE_URL"):
            settings.validate_api_keys("claude-code")

    def test_all_present_does_not_raise(self):
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url="http://localhost:4000",
            litellm_auth_token="sk-master",
            litellm_model="zai.glm-5",
        )
        settings.validate_api_keys("claude-code")  # no raise

    def test_none_agent_skips_custom_validation(self):
        """The no-op agent needs no backend creds — validation must be skipped."""
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url=None,
            litellm_auth_token=None,
            litellm_model=None,
        )
        settings.validate_api_keys("none")  # no raise


class TestBuildSdkEnvCustom:
    """Phase 2: _build_sdk_env() for the custom route."""

    def test_custom_route_env_has_anthropic_vars_only(self):
        route = LiteLLMRoute(
            base_url="http://x:4000",
            auth_token="sk-1",
            model="deepseek.v3.2",
            small_model="deepseek.v3.2",
        )
        env, model = ClaudeCodeAgent._build_sdk_env(route)
        assert env["ANTHROPIC_BASE_URL"] == "http://x:4000"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-1"
        assert env["ANTHROPIC_MODEL"] == "deepseek.v3.2"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "deepseek.v3.2"
        assert model == "deepseek.v3.2"
        # No Bedrock/Direct-specific vars.
        assert "CLAUDE_CODE_USE_BEDROCK" not in env
        assert "AWS_BEARER_TOKEN_BEDROCK" not in env
        assert "AWS_REGION" not in env

    def test_custom_route_no_model_omits_model_vars(self):
        route = LiteLLMRoute(base_url="http://x:4000", auth_token="sk-1")
        env, model = ClaudeCodeAgent._build_sdk_env(route)
        assert model is None
        assert "ANTHROPIC_MODEL" not in env
        assert "ANTHROPIC_SMALL_FAST_MODEL" not in env

    def test_custom_route_forwards_path(self, monkeypatch):
        import os

        custom_path = f"/custom/bin{os.pathsep}/usr/bin"
        monkeypatch.setenv("PATH", custom_path)
        env, _ = ClaudeCodeAgent._build_sdk_env(
            LiteLLMRoute(base_url="http://x:4000", auth_token="sk-1")
        )
        assert env["PATH"] == custom_path

    def test_custom_route_neutralizes_inherited_anthropic_api_key(self, monkeypatch):
        """The SDK merges os.environ, so a stray x-api-key must be overridden to
        empty in options.env (not merely omitted) — else it would fight the
        bearer auth_token against the gateway."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked-key")
        env, _ = ClaudeCodeAgent._build_sdk_env(
            LiteLLMRoute(base_url="http://x:4000", auth_token="sk-1")
        )
        assert env["ANTHROPIC_API_KEY"] == ""


class TestResolveEffectiveModelCustom:
    """Phase 2: _resolve_effective_model() on the custom route — no prefixing."""

    def test_config_model_synced_verbatim(self):
        route = LiteLLMRoute(base_url="http://x:4000", auth_token="sk-1", model="deepseek.v3.2")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route, config_model="zai.glm-5")
        effective = agent._resolve_effective_model("zai.glm-5", env, route_model)
        assert effective == "zai.glm-5"
        assert env["ANTHROPIC_MODEL"] == "zai.glm-5"  # no eu./anthropic. prefix

    def test_route_model_used_when_config_none(self):
        route = LiteLLMRoute(base_url="http://x:4000", auth_token="sk-1", model="deepseek.v3.2")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route)
        effective = agent._resolve_effective_model(None, env, route_model)
        assert effective == "deepseek.v3.2"

    def test_both_none_returns_none(self):
        route = LiteLLMRoute(base_url="http://x:4000", auth_token="sk-1")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route)
        effective = agent._resolve_effective_model(None, env, route_model)
        assert effective is None
        assert "ANTHROPIC_MODEL" not in env
