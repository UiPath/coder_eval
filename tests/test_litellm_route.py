"""Tests for the LiteLLM (Anthropic-compatible) open-weight backend (LiteLLMRoute).

Covers route resolution, config validation, SDK env building, effective-model
sync, pricing, and cost repricing. Mirrors the Bedrock equivalents in
``tests/test_routing.py``.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.cli.run_command import _litellm_preflight_error
from coder_eval.config import Settings
from coder_eval.models import (
    AgentKind,
    LiteLLMRoute,
    TokenUsage,
    parse_agent_config,
)
from coder_eval.models.enums import ApiBackend
from coder_eval.models.routing import ROUTE_NAMES, resolve_route
from coder_eval.pricing import _normalize_model, calculate_cost


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
    """_build_sdk_env() for the LiteLLM route."""

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
    """_resolve_effective_model() on the LiteLLM route — no prefixing."""

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


class TestOpenWeightPricing:
    """Pricing for the open-weight models + LiteLLM/Bedrock prefix normalization."""

    def test_glm5_rate(self):
        # 1M input + 1M output → input_per_mtok + output_per_mtok.
        assert calculate_cost("zai.glm-5", 1_000_000, 1_000_000) == pytest.approx(1.2 + 3.84)

    def test_deepseek_rate(self):
        assert calculate_cost("deepseek.v3.2", 1_000_000, 1_000_000) == pytest.approx(0.74 + 2.22)

    def test_kimi_rate(self):
        assert calculate_cost("moonshotai.kimi-k2.5", 1_000_000, 1_000_000) == pytest.approx(0.72 + 3.6)

    def test_kimi_converse_prefixed_prices_same(self):
        assert calculate_cost("converse/moonshotai.kimi-k2.5", 1_000_000, 1_000_000) == pytest.approx(0.72 + 3.6)

    def test_normalize_strips_converse_prefix(self):
        assert _normalize_model("converse/zai.glm-5") == "zai.glm-5"
        assert _normalize_model("bedrock/converse/deepseek.v3.2") == "deepseek.v3.2"

    def test_normalize_identity_on_bare_ids(self):
        assert _normalize_model("zai.glm-5") == "zai.glm-5"
        assert _normalize_model("deepseek.v3.2") == "deepseek.v3.2"

    def test_converse_prefixed_id_prices_same(self):
        """The SDK reports model_used as e.g. 'converse/zai.glm-5' — must still price."""
        assert calculate_cost("converse/zai.glm-5", 1_000_000, 1_000_000) == pytest.approx(1.2 + 3.84)


class TestRepriceForLitellm:
    """The litellm backend recomputes cost from tokens, overriding the SDK estimate."""

    def test_overrides_wrong_sdk_cost_with_real_rate(self):
        u = TokenUsage(uncached_input_tokens=1_000_000, output_tokens=1_000_000, total_cost_usd=3.68)
        ClaudeCodeAgent._reprice_for_litellm(u, "zai.glm-5")
        assert u.total_cost_usd == pytest.approx(1.2 + 3.84)

    def test_converse_prefixed_model_reprices(self):
        u = TokenUsage(uncached_input_tokens=1_000_000, output_tokens=1_000_000, total_cost_usd=99.0)
        ClaudeCodeAgent._reprice_for_litellm(u, "converse/zai.glm-5")
        assert u.total_cost_usd == pytest.approx(1.2 + 3.84)

    def test_unpriced_model_yields_none_not_sdk_figure(self):
        """An unknown model must show N/A (None), never the misleading SDK cost."""
        u = TokenUsage(uncached_input_tokens=100, output_tokens=100, total_cost_usd=9.99)
        ClaudeCodeAgent._reprice_for_litellm(u, "some-unknown-model")
        assert u.total_cost_usd is None

    def test_none_model_yields_none(self):
        u = TokenUsage(uncached_input_tokens=100, output_tokens=100, total_cost_usd=9.99)
        ClaudeCodeAgent._reprice_for_litellm(u, None)
        assert u.total_cost_usd is None


class TestLitellmPreflight:
    """External-proxy reachability preflight — fail fast instead of hanging on a dead proxy."""

    def test_none_for_non_litellm_backend(self):
        s = Settings(api_backend=ApiBackend.BEDROCK, litellm_base_url="http://x:4000")
        assert _litellm_preflight_error(s) is None

    def test_none_when_no_base_url(self):
        s = Settings(api_backend=ApiBackend.LITELLM, litellm_base_url=None, litellm_model="m")
        assert _litellm_preflight_error(s) is None

    def test_error_when_proxy_down(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=urllib.error.URLError("refused")))
        s = Settings(api_backend=ApiBackend.LITELLM, litellm_base_url="http://127.0.0.1:9", litellm_model="m")
        err = _litellm_preflight_error(s)
        assert err is not None
        assert "not reachable" in err and "http://127.0.0.1:9" in err

    def test_none_when_reachable(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", MagicMock(return_value=MagicMock()))
        s = Settings(api_backend=ApiBackend.LITELLM, litellm_base_url="http://127.0.0.1:4000", litellm_model="m")
        assert _litellm_preflight_error(s) is None

    def test_none_on_http_error_means_server_up(self, monkeypatch):
        monkeypatch.setattr(
            urllib.request, "urlopen", MagicMock(side_effect=urllib.error.HTTPError("u", 404, "nf", {}, None))
        )
        s = Settings(api_backend=ApiBackend.LITELLM, litellm_base_url="http://127.0.0.1:4000", litellm_model="m")
        assert _litellm_preflight_error(s) is None
