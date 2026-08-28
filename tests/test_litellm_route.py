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
    BedrockRoute,
    DirectRoute,
    LiteLLMRoute,
    TokenUsage,
    parse_agent_config,
)
from coder_eval.models.enums import ApiBackend
from coder_eval.models.routing import ROUTE_NAMES, resolve_evaluation_route, resolve_route
from coder_eval.pricing import _normalize_model, calculate_cost


def _make_agent(route, *, config_model: str | None = None) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE, model=config_model), route=route)


class TestResolveEvaluationRoute:
    """resolve_evaluation_route() pins the judge + simulated user to a constant
    Claude backend regardless of the agent's backend, so grading/simulation stay
    comparable across the models under test."""

    @staticmethod
    def _isolated_settings(monkeypatch, **kwargs):
        # Skip .env and clear the credential env vars so presence/absence is
        # driven purely by kwargs (config republishes .env into os.environ).
        for var in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        return Settings(_env_file=None, **kwargs)

    def test_bedrock_agent_route_is_reused_with_model_reset(self, monkeypatch):
        # Same region/other fields reused verbatim, but `model` is reset to None
        # (no checker_context override) rather than carrying over the agent's own
        # env-sourced model — see TestResolveEvaluationRouteJudgeFloor.
        route = BedrockRoute(region="eu-north-1", model="eu.anthropic.claude-sonnet-4-6")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.BEDROCK)
        ev = resolve_evaluation_route(settings, route)
        assert ev is not route
        assert ev == BedrockRoute(region="eu-north-1", model=None)

    def test_direct_agent_route_is_reused_with_model_reset(self, monkeypatch):
        route = DirectRoute(judge_transport="anthropic", model="gpt-4")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.DIRECT)
        ev = resolve_evaluation_route(settings, route)
        assert ev == DirectRoute(judge_transport="anthropic", model=None)

    def test_litellm_agent_pins_evaluation_to_bedrock_when_aws_creds_present(self, monkeypatch):
        agent = LiteLLMRoute(model="zai.glm-5")
        settings = self._isolated_settings(
            monkeypatch,
            api_backend=ApiBackend.LITELLM,
            aws_bearer_token_bedrock="aws-tok",
            aws_region="eu-north-1",
        )
        ev = resolve_evaluation_route(settings, agent)
        assert isinstance(ev, BedrockRoute)
        assert ev.region == "eu-north-1"
        # No checker_context override -> model is None, so llm_judge falls back
        # to DEFAULT_JUDGE_MODEL rather than an env-sourced value.
        assert ev.model is None

    def test_litellm_agent_falls_back_to_direct_when_only_anthropic_key(self, monkeypatch):
        agent = LiteLLMRoute(model="zai.glm-5")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.LITELLM, anthropic_api_key="sk-ant")
        ev = resolve_evaluation_route(settings, agent)
        assert isinstance(ev, DirectRoute)
        assert ev.judge_transport == "anthropic"

    def test_litellm_agent_unconfigured_yields_direct_with_no_transport(self, monkeypatch):
        # No Bedrock creds and no ANTHROPIC_API_KEY → DirectRoute(None), which makes
        # llm_judge fail with its clean "unconfigured" error rather than scoring 0.0.
        agent = LiteLLMRoute(model="zai.glm-5")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.LITELLM)
        ev = resolve_evaluation_route(settings, agent)
        assert isinstance(ev, DirectRoute)
        assert ev.judge_transport is None


class TestResolveEvaluationRouteJudgeFloor:
    """Regression coverage for PR #137 review Axis 8 ('the judge loses
    DEFAULT_JUDGE_MODEL as its floor'): resolve_evaluation_route must never bake
    the agent's own env-sourced model into the eval route's `model` unless a real
    checker_context.api_route.model override was given — otherwise an unpinned
    llm_judge silently starts grading with a different model whenever the
    agent's model changes (BEDROCK_MODEL), breaking before/after comparability.
    """

    @staticmethod
    def _isolated_settings(monkeypatch, **kwargs):
        for var in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION", "ANTHROPIC_API_KEY", "BEDROCK_MODEL"):
            monkeypatch.delenv(var, raising=False)
        return Settings(_env_file=None, **kwargs)

    def test_bedrock_agent_route_no_override_strips_agent_model(self, monkeypatch):
        # Simulates an agent route built with a real BEDROCK_MODEL (e.g. opus),
        # reused for the eval side with no checker_context override -> the eval
        # route's model must be None so llm_judge falls back to DEFAULT_JUDGE_MODEL,
        # not the agent's model.
        agent_route = BedrockRoute(region="eu-north-1", model="eu.anthropic.claude-opus-4-1")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.BEDROCK)
        ev = resolve_evaluation_route(settings, agent_route)
        assert isinstance(ev, BedrockRoute)
        assert ev.model is None

    def test_bedrock_agent_route_with_override_qualifies_model(self, monkeypatch):
        agent_route = BedrockRoute(region="eu-north-1", model="eu.anthropic.claude-opus-4-1")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.BEDROCK)
        ev = resolve_evaluation_route(settings, agent_route, model_override="claude-haiku-4-5")
        assert isinstance(ev, BedrockRoute)
        assert ev.model == "eu.anthropic.claude-haiku-4-5"

    def test_direct_agent_route_no_override_strips_agent_model(self, monkeypatch):
        agent_route = DirectRoute(judge_transport="anthropic", model="gpt-4")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.DIRECT)
        ev = resolve_evaluation_route(settings, agent_route)
        assert isinstance(ev, DirectRoute)
        assert ev.model is None

    def test_litellm_agent_pin_to_bedrock_no_override_strips_bedrock_model(self, monkeypatch):
        # Agent on LiteLLM (open-weight); AWS creds present with a real
        # BEDROCK_MODEL configured for some unrelated purpose. No override ->
        # the pinned eval route must NOT inherit BEDROCK_MODEL.
        agent_route = LiteLLMRoute(model="zai.glm-5")
        settings = self._isolated_settings(
            monkeypatch,
            api_backend=ApiBackend.LITELLM,
            aws_bearer_token_bedrock="aws-tok",
            aws_region="eu-north-1",
            bedrock_model="claude-opus-4-1",
        )
        ev = resolve_evaluation_route(settings, agent_route)
        assert isinstance(ev, BedrockRoute)
        assert ev.model is None

    def test_litellm_agent_pin_to_bedrock_with_override(self, monkeypatch):
        agent_route = LiteLLMRoute(model="zai.glm-5")
        settings = self._isolated_settings(
            monkeypatch,
            api_backend=ApiBackend.LITELLM,
            aws_bearer_token_bedrock="aws-tok",
            aws_region="eu-north-1",
        )
        ev = resolve_evaluation_route(settings, agent_route, model_override="claude-haiku-4-5")
        assert isinstance(ev, BedrockRoute)
        assert ev.model == "eu.anthropic.claude-haiku-4-5"


class TestBackendOverride:
    """resolve_evaluation_route(backend_override=...) — the checker_context.api_route.route
    override path, dispatched through _resolve_backend_route. Zero coverage before this PR
    review (Axis 3 blocker)."""

    @staticmethod
    def _isolated_settings(monkeypatch, **kwargs):
        for var in (
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_REGION",
            "ANTHROPIC_API_KEY",
            "LITELLM_BASE_URL",
            "LITELLM_AUTH_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)
        return Settings(_env_file=None, **kwargs)

    def test_override_to_bedrock_builds_route_from_env(self, monkeypatch):
        agent_route = DirectRoute()
        settings = self._isolated_settings(
            monkeypatch,
            api_backend=ApiBackend.DIRECT,
            aws_bearer_token_bedrock="aws-tok",
            aws_region="eu-north-1",
        )
        ev = resolve_evaluation_route(settings, agent_route, backend_override="bedrock")
        assert isinstance(ev, BedrockRoute)
        assert ev.region == "eu-north-1"

    def test_override_to_bedrock_without_creds_raises(self, monkeypatch):
        agent_route = DirectRoute()
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.DIRECT)
        with pytest.raises(ValueError, match="requires AWS_BEARER_TOKEN_BEDROCK and AWS_REGION"):
            resolve_evaluation_route(settings, agent_route, backend_override="bedrock")

    def test_override_to_direct_builds_route_from_env(self, monkeypatch):
        agent_route = BedrockRoute(region="eu-north-1")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.BEDROCK, anthropic_api_key="sk-ant")
        ev = resolve_evaluation_route(settings, agent_route, backend_override="direct")
        assert isinstance(ev, DirectRoute)
        assert ev.judge_transport == "anthropic"

    def test_override_to_direct_without_key_raises(self, monkeypatch):
        agent_route = BedrockRoute(region="eu-north-1")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.BEDROCK)
        with pytest.raises(ValueError, match="requires ANTHROPIC_API_KEY"):
            resolve_evaluation_route(settings, agent_route, backend_override="direct")

    def test_override_to_litellm_builds_route_from_params_and_env_params(self, monkeypatch):
        """No dependency on settings.litellm_base_url/litellm_auth_token at all —
        the checker's litellm route is built entirely from params/env_params."""
        agent_route = BedrockRoute(region="eu-north-1")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.BEDROCK)
        ev = resolve_evaluation_route(
            settings,
            agent_route,
            backend_override="litellm",
            model_override="gpt-5.6-luna",
            params_override={"api_base": "http://gateway:4000"},
            env_params_override={"api_key": "MY_ENV_VAR"},
        )
        assert isinstance(ev, LiteLLMRoute)
        assert ev.model == "gpt-5.6-luna"
        assert ev.params == {"api_base": "http://gateway:4000"}
        assert ev.env_params == {"api_key": "MY_ENV_VAR"}

    def test_override_to_litellm_without_model_raises(self, monkeypatch):
        """There is no default open-weight/gateway model to fall back to."""
        agent_route = BedrockRoute(region="eu-north-1")
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.BEDROCK)
        with pytest.raises(ValueError, match=r"requires an explicit `checker_context\.api_route\.model`"):
            resolve_evaluation_route(settings, agent_route, backend_override="litellm")

    def test_unknown_backend_raises(self, monkeypatch):
        agent_route = DirectRoute()
        settings = self._isolated_settings(monkeypatch, api_backend=ApiBackend.DIRECT)
        with pytest.raises(ValueError, match="is not a known backend"):
            resolve_evaluation_route(settings, agent_route, backend_override="not-a-backend")


class TestCheckerContextModel:
    """CheckerContext/ApiRouteContext — the typed replacement for the old
    hand-validated open dict (previously 0% covered, per the PR #137 review).
    ``extra="forbid"`` + real field types now do what the hand-rolled
    ``validate_checker_context_shape`` used to."""

    @staticmethod
    def _validate(value):
        from coder_eval.models import CheckerContext

        return CheckerContext(**value)

    def test_accepts_empty(self):
        self._validate({})

    def test_accepts_route_and_model(self):
        cc = self._validate({"api_route": {"route": "bedrock", "model": "claude-haiku-4-5"}})
        assert cc.api_route is not None
        assert cc.api_route.route == ApiBackend.BEDROCK
        assert cc.api_route.model == "claude-haiku-4-5"

    def test_rejects_unknown_namespace(self):
        with pytest.raises(ValueError, match=r"[Ee]xtra"):
            self._validate({"api_rotue": {"route": "bedrock"}})

    def test_rejects_unknown_api_route_key(self):
        with pytest.raises(ValueError, match=r"[Ee]xtra"):
            self._validate({"api_route": {"rotue": "bedrock"}})

    def test_rejects_unknown_backend_name(self):
        with pytest.raises(ValueError):
            self._validate({"api_route": {"route": "not-a-backend"}})

    def test_accepts_params_and_env_params_with_litellm_route(self):
        cc = self._validate(
            {
                "api_route": {
                    "route": "litellm",
                    "params": {"aws_region_name": "eu-north-1"},
                    "env_params": {"api_key": "MY_ENV_VAR"},
                }
            }
        )
        assert cc.api_route is not None
        assert cc.api_route.params == {"aws_region_name": "eu-north-1"}
        assert cc.api_route.env_params == {"api_key": "MY_ENV_VAR"}

    def test_rejects_params_without_litellm_route(self):
        with pytest.raises(ValueError, match="require route: litellm"):
            self._validate({"api_route": {"route": "bedrock", "params": {"x": 1}}})

    def test_rejects_env_params_without_litellm_route(self):
        with pytest.raises(ValueError, match="require route: litellm"):
            self._validate({"api_route": {"env_params": {"api_key": "MY_ENV_VAR"}}})

    def test_rejects_non_dict_params(self):
        with pytest.raises(ValueError):
            self._validate({"api_route": {"route": "litellm", "params": "not-a-dict"}})

    def test_rejects_non_dict_env_params(self):
        with pytest.raises(ValueError):
            self._validate({"api_route": {"route": "litellm", "env_params": "not-a-dict"}})

    def test_rejects_non_string_env_params_values(self):
        with pytest.raises(ValueError):
            self._validate({"api_route": {"route": "litellm", "env_params": {"api_key": 123}}})

    def test_rejects_non_string_model(self):
        """A YAML `model: 5` must be rejected here, not str()-ified downstream."""
        with pytest.raises(ValueError):
            self._validate({"api_route": {"route": "bedrock", "model": 5}})


class TestEvalRouteWiring:
    """The orchestrator must hand the simulated user simulator_route — resolved
    independently of eval_route/checker_context.api_route (that override is
    llm_judge-only and has no bearing on the simulator, a real Claude Code CLI
    subprocess) — and never the agent's (possibly open-weight) route — guards
    the simulation path the senior review flagged as untested."""

    async def test_simulator_receives_simulator_route_not_agent_or_eval_route(self, monkeypatch):
        from pathlib import Path
        from types import SimpleNamespace

        from coder_eval import orchestrator as orch_mod
        from coder_eval.orchestrator import Orchestrator

        eval_route = LiteLLMRoute(model="gpt-5.6-luna")  # llm_judge-only override
        simulator_route = BedrockRoute(region="eu-north-1", model="eu.anthropic.claude-sonnet-4-6")
        agent_route = LiteLLMRoute(model="zai.glm-5")
        captured: dict = {}

        class _SpySimulator:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def start(self):
                # Abort before the dialog loop; we only care which route was passed.
                raise RuntimeError("__stop_dialog__")

        monkeypatch.setattr(orch_mod, "UserSimulator", _SpySimulator)
        fake = SimpleNamespace(
            result=SimpleNamespace(simulation=None),
            task=SimpleNamespace(simulation=object(), agent=object(), description="d"),
            agent=object(),
            success_checker=object(),
            eval_route=eval_route,
            simulator_route=simulator_route,
            route=agent_route,
        )
        with pytest.raises(RuntimeError, match="__stop_dialog__"):
            await Orchestrator._simulation_dialog_loop(fake, initial_prompt="hi", sandbox_dir=Path("/tmp"))
        # If someone reverts to route=self.route this flips to the agent route; if
        # someone reverts to route=self.eval_route this flips to the litellm override.
        assert captured["route"] is simulator_route
        assert captured["route"] is not agent_route
        assert captured["route"] is not eval_route


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
        # base_url/auth_token are NOT stored on the route (read live from
        # settings by _build_sdk_env) -- only model/small_model live here.
        assert route.model == "deepseek.v3.2"

    def test_rejects_scheme_less_base_url(self):
        # resolve_route is the ONLY validation on the evaluate-only path (which
        # skips validate_api_keys), so it must reject a malformed URL itself —
        # otherwise environment_info records an empty host silently.
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url="localhost:4000",
            litellm_auth_token="sk-master",
            litellm_model="zai.glm-5",
        )
        with pytest.raises(ValueError, match="LITELLM_BASE_URL must be an http"):
            resolve_route(settings)

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

    def test_scheme_less_base_url_raises_naming_it(self):
        # "localhost:4000" has no http(s) scheme — must fail at validation with a
        # field-named message, not later as a raw urlopen ValueError.
        settings = Settings(
            api_backend=ApiBackend.LITELLM,
            litellm_base_url="localhost:4000",
            litellm_auth_token="sk-master",
            litellm_model="zai.glm-5",
        )
        with pytest.raises(ValueError, match="LITELLM_BASE_URL must be an http"):
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

    def test_custom_route_env_has_anthropic_vars_only(self, monkeypatch):
        from coder_eval.agents import claude_code_agent as claude_code_agent_mod

        monkeypatch.setattr(claude_code_agent_mod.settings, "litellm_auth_token", "sk-1")
        monkeypatch.setattr(claude_code_agent_mod.settings, "litellm_base_url", "http://x:4000")
        route = LiteLLMRoute(
            model="deepseek.v3.2",
            small_model="deepseek.v3.2",
        )
        env, model = ClaudeCodeAgent._build_sdk_env(route)
        assert env["ANTHROPIC_BASE_URL"] == "http://x:4000"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-1"
        assert env["ANTHROPIC_MODEL"] == "deepseek.v3.2"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "deepseek.v3.2"
        assert model == "deepseek.v3.2"
        # Inherited Bedrock creds are neutralized (blanked to ""), not merely
        # absent, so the CLI can't auto-select Bedrock-direct and bypass the proxy.
        assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
        assert env["AWS_BEARER_TOKEN_BEDROCK"] == ""
        assert "AWS_REGION" not in env

    def test_custom_route_no_model_omits_model_vars(self):
        route = LiteLLMRoute()
        env, model = ClaudeCodeAgent._build_sdk_env(route)
        assert model is None
        assert "ANTHROPIC_MODEL" not in env
        assert "ANTHROPIC_SMALL_FAST_MODEL" not in env

    def test_custom_route_forwards_path(self, monkeypatch):
        import os

        custom_path = f"/custom/bin{os.pathsep}/usr/bin"
        monkeypatch.setenv("PATH", custom_path)
        env, _ = ClaudeCodeAgent._build_sdk_env(LiteLLMRoute())
        assert env["PATH"] == custom_path

    def test_custom_route_neutralizes_inherited_anthropic_api_key(self, monkeypatch):
        """The SDK merges os.environ, so a stray x-api-key must be overridden to
        empty in options.env (not merely omitted) — else it would fight the
        bearer auth_token against the gateway."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked-key")
        env, _ = ClaudeCodeAgent._build_sdk_env(LiteLLMRoute())
        assert env["ANTHROPIC_API_KEY"] == ""

    def test_cost_log_tags_become_custom_headers(self):
        """cost_log_tags → ANTHROPIC_CUSTOM_HEADERS as newline-separated
        `Name: Value` pairs (the format Claude Code forwards verbatim), so the
        proxy-side cost log can join each call back to the run/task/turn."""
        route = LiteLLMRoute(model="deepseek/deepseek-v4-pro")
        tags = {"x-ce-run-id": "abc123", "x-ce-task-id": "calc/v1", "x-ce-iteration": "2"}
        env, _ = ClaudeCodeAgent._build_sdk_env(route, cost_log_tags=tags)
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == "x-ce-run-id: abc123\nx-ce-task-id: calc/v1\nx-ce-iteration: 2"

    def test_no_cost_log_tags_omits_custom_headers(self):
        route = LiteLLMRoute()
        env, _ = ClaudeCodeAgent._build_sdk_env(route)
        assert "ANTHROPIC_CUSTOM_HEADERS" not in env
        env2, _ = ClaudeCodeAgent._build_sdk_env(route, cost_log_tags={})
        assert "ANTHROPIC_CUSTOM_HEADERS" not in env2  # empty dict is a no-op

    def test_cost_log_tags_ignored_on_non_litellm_routes(self):
        """The tag is a LiteLLM-only concern; Bedrock/Direct must not emit it."""
        tags = {"x-ce-run-id": "abc123"}
        bedrock = BedrockRoute(region="eu-north-1", model="x")
        env_b, _ = ClaudeCodeAgent._build_sdk_env(bedrock, cost_log_tags=tags)
        assert "ANTHROPIC_CUSTOM_HEADERS" not in env_b
        env_d, _ = ClaudeCodeAgent._build_sdk_env(DirectRoute(), cost_log_tags=tags)
        assert "ANTHROPIC_CUSTOM_HEADERS" not in env_d

    def test_cost_log_tags_gated_on_agent_capability_not_route(self):
        """Regression: cost_log_tags is a Claude-only constructor kwarg, but the
        route that triggers it (LiteLLM) is agent-independent. The agent-agnostic
        create_agent factory must forward it ONLY to agents that declare
        supports_cost_log_tags — otherwise a none/codex/antigravity task crashes
        with TypeError under API_BACKEND=litellm."""
        from coder_eval.agents import AgentRegistry, create_agent
        from coder_eval.models import NoneAgentConfig
        from coder_eval.plugins import ensure_plugins_loaded

        ensure_plugins_loaded()
        # Capability contract the orchestrator gate reads.
        assert AgentRegistry.get(AgentKind.CLAUDE_CODE).agent_class.supports_cost_log_tags is True
        assert AgentRegistry.get(AgentKind.NONE).agent_class.supports_cost_log_tags is False

        route = LiteLLMRoute(model="deepseek/deepseek-v4-pro")
        # A none-agent constructs fine on a LiteLLM route (the gate omits the kwarg)...
        assert create_agent(AgentKind.NONE, NoneAgentConfig(type=AgentKind.NONE), route=route) is not None
        # ...and it WOULD crash if the kwarg were forwarded — exactly what the gate prevents.
        with pytest.raises(TypeError):
            create_agent(
                AgentKind.NONE, NoneAgentConfig(type=AgentKind.NONE), route=route, cost_log_tags={"x-ce-run-id": "r"}
            )

    def test_cost_log_tags_reject_header_injection(self):
        # A task_id/variant_id carrying a CR/LF would inject extra headers into every
        # SDK->proxy request; the seam must reject it (single-line ASCII only).
        route = LiteLLMRoute()
        with pytest.raises(ValueError, match="single-line ASCII"):
            ClaudeCodeAgent._build_sdk_env(route, cost_log_tags={"x-ce-task-id": "ok\nAuthorization: Bearer forged"})


class TestResolveEffectiveModelCustom:
    """_resolve_effective_model() on the LiteLLM route — no prefixing."""

    def test_config_model_synced_verbatim(self):
        route = LiteLLMRoute(model="deepseek.v3.2")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route, config_model="zai.glm-5")
        effective = agent._resolve_effective_model("zai.glm-5", env, route_model)
        assert effective == "zai.glm-5"
        assert env["ANTHROPIC_MODEL"] == "zai.glm-5"  # no eu./anthropic. prefix

    def test_route_model_used_when_config_none(self):
        route = LiteLLMRoute(model="deepseek.v3.2")
        env, route_model = ClaudeCodeAgent._build_sdk_env(route)
        agent = _make_agent(route)
        effective = agent._resolve_effective_model(None, env, route_model)
        assert effective == "deepseek.v3.2"

    def test_both_none_returns_none(self):
        route = LiteLLMRoute()
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


class TestRepriceWiring:
    """The finalize path (not just the static helper) reprices a litellm turn. A
    regression that skips the reprice would silently persist the SDK's Claude cost
    and disable the max_usd gate — the static-only tests above wouldn't catch it."""

    def _usage_after_finalize(self, effective_model: str | None) -> TokenUsage:
        from types import SimpleNamespace

        from coder_eval.agents.claude_code_agent import _ClaudeTurnState

        agent = _make_agent(
            LiteLLMRoute(model="zai.glm-5"),
            config_model="zai.glm-5",
        )
        stub = SimpleNamespace(
            _agent=agent,
            sdk_messages=[],
            sdk_result_usage=None,
            sdk_result_cost=None,
            # model_usage carries the SDK's Claude-priced estimate (3.68); the
            # reprice must override it from the litellm rate table.
            sdk_result_model_usage={"m": {"inputTokens": 1_000_000, "outputTokens": 1_000_000, "costUSD": 3.68}},
            effective_model=effective_model,
        )
        return _ClaudeTurnState._finalize_token_usage(stub)  # type: ignore[arg-type]

    def test_finalize_reprices_priced_litellm_model(self):
        usage = self._usage_after_finalize("zai.glm-5")
        # litellm rate (1.2 + 3.84), NOT the SDK's Claude estimate of 3.68.
        assert usage.total_cost_usd == pytest.approx(1.2 + 3.84)
        assert usage.uncached_input_tokens == 1_000_000  # token buckets untouched

    def test_finalize_unpriced_litellm_model_yields_none(self):
        # An unpriced model → None (not the misleading 3.68), so the orchestrator
        # skips the max_usd gate rather than gating on a wrong figure.
        usage = self._usage_after_finalize("some-unpriced-model")
        assert usage.total_cost_usd is None


class TestLitellmPreflight:
    """External-proxy reachability preflight — fail fast instead of hanging on a dead proxy."""

    def test_none_for_non_litellm_backend(self):
        s = Settings(api_backend=ApiBackend.BEDROCK, litellm_base_url="http://x:4000")
        assert _litellm_preflight_error(s) is None

    def test_none_when_no_base_url(self):
        s = Settings(api_backend=ApiBackend.LITELLM, litellm_base_url=None, litellm_model="m")
        assert _litellm_preflight_error(s) is None

    def test_scheme_less_base_url_returns_clean_error(self):
        # Regression: a scheme-less URL used to make urlopen raise a bare
        # ValueError that escaped as a traceback. Now it returns a clean message.
        s = Settings(api_backend=ApiBackend.LITELLM, litellm_base_url="localhost:4000", litellm_model="m")
        err = _litellm_preflight_error(s)
        assert err is not None and "http(s)" in err

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
