"""The host-side allowlist derivation for ``network: llm_only`` and the agent-config hook."""

from __future__ import annotations

import logging
import traceback
from typing import Any

import pytest

from coder_eval.agents.registry import AgentRegistry
from coder_eval.config import Settings
from coder_eval.isolation import docker_runner, errors
from coder_eval.isolation.egress import resolve_egress_targets
from coder_eval.models import (
    ApiBackend,
    DockerDriverConfig,
    FileExistsCriterion,
    NoulQuestion,
    SandboxConfig,
    SystemOneJudgeCriterion,
    TaskDefinition,
    normalize_egress_target,
    parse_agent_config,
)
from coder_eval.plugins import ensure_plugins_loaded


DIRECT = ["api.anthropic.com:443", "platform.claude.com:443"]


def _settings(backend: ApiBackend = ApiBackend.DIRECT, region: str | None = None) -> Settings:
    return Settings.model_construct(api_backend=backend, aws_region=region)


def _task(agent: dict[str, Any] | None = None, *, criteria: list[Any] | None = None, **docker: Any) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(**(agent or {"type": "claude-code"})),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(network="llm_only", **docker)),
        success_criteria=criteria or [FileExistsCriterion(description="c", path="x.txt")],
    )


def test_direct_claude_code_needs_only_the_anthropic_hosts():
    assert resolve_egress_targets(_task(), env={}, settings=_settings()) == DIRECT


def test_bedrock_adds_runtime_and_control_plane_for_the_region():
    targets = resolve_egress_targets(_task(), env={}, settings=_settings(ApiBackend.BEDROCK, "eu-north-1"))
    assert targets == ["bedrock-runtime.eu-north-1.amazonaws.com:443", "bedrock.eu-north-1.amazonaws.com:443"]


def test_bedrock_without_region_warns_and_adds_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="coder_eval.isolation.egress"):
        targets = resolve_egress_targets(_task(), env={}, settings=_settings(ApiBackend.BEDROCK))
    assert targets == []
    assert "AWS_REGION" in caplog.text


def test_litellm_loopback_url_is_rewritten_to_the_host_alias():
    env = {"LITELLM_BASE_URL": "http://localhost:4000"}
    targets = resolve_egress_targets(_task(), env=env, settings=_settings(ApiBackend.LITELLM))
    assert targets == ["host.docker.internal:4000"]


def test_litellm_remote_url_keeps_its_port():
    env = {"LITELLM_BASE_URL": "https://litellm.corp:8443/v1"}
    targets = resolve_egress_targets(_task(), env=env, settings=_settings(ApiBackend.LITELLM))
    assert targets == ["litellm.corp:8443"]


def test_codex_without_base_url_needs_api_openai():
    targets = resolve_egress_targets(_task({"type": "codex"}), env={}, settings=_settings(ApiBackend.LITELLM))
    assert targets == ["api.openai.com:443"]


def test_codex_with_azure_base_url_uses_only_that_host():
    env = {"CODEX_BASE_URL": "https://x.openai.azure.com/openai/v1"}
    targets = resolve_egress_targets(_task({"type": "codex"}), env=env, settings=_settings(ApiBackend.LITELLM))
    assert targets == ["x.openai.azure.com:443"]


def test_non_litellm_loopback_url_is_skipped_with_a_warning(caplog):
    env = {"CODEX_BASE_URL": "http://127.0.0.1:8080"}
    with caplog.at_level(logging.WARNING, logger="coder_eval.isolation.egress"):
        targets = resolve_egress_targets(_task({"type": "codex"}), env=env, settings=_settings(ApiBackend.LITELLM))
    assert targets == []
    assert "CODEX_BASE_URL" in caplog.text
    assert "8080" not in caplog.text


def test_uipath_url_with_a_path_contributes_its_host():
    env = {"UIPATH_URL": "https://cloud.uipath.com/org/tenant"}
    assert "cloud.uipath.com:443" in resolve_egress_targets(_task(), env=env, settings=_settings())


def test_only_forwarded_url_vars_count():
    env = {"MY_SERVICE_URL": "https://svc.example.com", "HOME": "/root"}
    assert resolve_egress_targets(_task(), env=env, settings=_settings()) == DIRECT
    forwarded = _task(env_passthrough_extra=["MY_SERVICE_URL", "MISSING_URL"])
    assert resolve_egress_targets(forwarded, env=env, settings=_settings()) == sorted([*DIRECT, "svc.example.com:443"])


def test_plain_http_url_defaults_to_port_80():
    task = _task(env_passthrough_extra=["MIRROR_URL"])
    targets = resolve_egress_targets(task, env={"MIRROR_URL": "http://mirror.example"}, settings=_settings())
    assert "mirror.example:80" in targets


def test_non_http_url_var_is_ignored():
    task = _task(env_passthrough_extra=["DB_URL"])
    targets = resolve_egress_targets(task, env={"DB_URL": "postgres://db.example:5432/x"}, settings=_settings())
    assert targets == DIRECT


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@[::2]:443",
        "https://token:secret/path",
        "http://[secret",
        "https://secret.example:0",
        "https://secret.example:70000",
    ],
)
def test_unallowlistable_url_raises_without_echoing_the_value(url: str):
    task = _task(env_passthrough_extra=["X_URL"])
    with pytest.raises(ValueError, match="X_URL") as excinfo:
        resolve_egress_targets(task, env={"X_URL": url}, settings=_settings())
    assert "secret" not in "".join(traceback.format_exception(excinfo.value))


def test_bad_litellm_loopback_port_names_the_variable():
    env = {"LITELLM_BASE_URL": "http://localhost:" + "secret"}
    with pytest.raises(ValueError, match="LITELLM_BASE_URL") as excinfo:
        resolve_egress_targets(_task(), env=env, settings=_settings())
    assert "secret" not in "".join(traceback.format_exception(excinfo.value))


def test_ipv6_loopback_litellm_url_is_rewritten():
    env = {"LITELLM_BASE_URL": "http://[::1]:4000"}
    assert resolve_egress_targets(_task(), env=env, settings=_settings(ApiBackend.LITELLM)) == [
        "host.docker.internal:4000"
    ]


def test_url_vars_dropped_by_a_replaced_passthrough_do_not_count():
    env = {"LITELLM_BASE_URL": "https://litellm.corp", "CODEX_BASE_URL": "https://x.openai.azure.com"}
    task = _task({"type": "codex"}, env_passthrough=["CODEX_API_KEY"])
    assert resolve_egress_targets(task, env=env, settings=_settings(ApiBackend.LITELLM)) == ["api.openai.com:443"]


def test_bedrock_region_is_lowercased_and_a_bad_region_is_named():
    targets = resolve_egress_targets(_task(), env={}, settings=_settings(ApiBackend.BEDROCK, "EU-North-1"))
    assert "bedrock-runtime.eu-north-1.amazonaws.com:443" in targets
    with pytest.raises(ValueError, match="AWS_REGION"):
        resolve_egress_targets(_task(), env={}, settings=_settings(ApiBackend.BEDROCK, "eu north"))


def test_antigravity_adds_the_gemini_host():
    targets = resolve_egress_targets(_task({"type": "antigravity"}), env={}, settings=_settings(ApiBackend.LITELLM))
    assert targets == ["generativelanguage.googleapis.com:443"]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openrouter/moonshotai/kimi-k3", "openrouter.ai:443"),
        ("anthropic/claude-haiku-4-5", "api.anthropic.com:443"),
        ("openai/gpt-5", "api.openai.com:443"),
        ("google/gemini-3", "generativelanguage.googleapis.com:443"),
        ("unknown/x", "openrouter.ai:443"),
        ("no-prefix", "openrouter.ai:443"),
        (None, "openrouter.ai:443"),
    ],
)
def test_pi_maps_the_provider_prefix(model: str | None, expected: str):
    task = _task({"type": "pi", "model": model})
    assert resolve_egress_targets(task, env={}, settings=_settings(ApiBackend.LITELLM)) == [expected]


@pytest.mark.parametrize("kind", ["opencode", "delegate"])
def test_agents_without_own_hosts_add_nothing(kind: str):
    targets = resolve_egress_targets(_task({"type": kind}), env={}, settings=_settings(ApiBackend.LITELLM))
    assert targets == []


def test_the_none_agent_adds_nothing():
    assert parse_agent_config(type="none").egress_hosts({}) == ()


def test_unresolved_agent_contributes_nothing():
    task = _task()
    task.agent = None
    assert resolve_egress_targets(task, env={}, settings=_settings()) == DIRECT


def test_system_one_judge_default_and_custom_base_url():
    questions = {"q": NoulQuestion(instructions="i")}
    default = SystemOneJudgeCriterion(description="j", questions=questions)
    custom = SystemOneJudgeCriterion(description="k", questions=questions, base_url="https://gw.example:9443/v1")
    targets = resolve_egress_targets(_task(criteria=[default, custom]), env={}, settings=_settings(ApiBackend.LITELLM))
    assert targets == ["api.typesafe.ai:443", "gw.example:9443"]


def test_user_allowlist_merged_deduplicated_and_sorted():
    task = _task(egress_allowlist=["pypi.org", "API.anthropic.com", "pypi.org:443", "a.example"])
    targets = resolve_egress_targets(task, env={}, settings=_settings())
    assert targets == ["a.example:443", "api.anthropic.com:443", "platform.claude.com:443", "pypi.org:443"]


def test_every_registered_agent_config_answers_with_normalized_targets():
    ensure_plugins_loaded()
    registrations = AgentRegistry.registrations()
    assert registrations
    for registration in registrations:
        config = registration.config_class.model_construct()
        hosts = config.egress_hosts({})
        assert isinstance(hosts, tuple), registration.config_class.__name__
        assert all(normalize_egress_target(host) == host for host in hosts), registration.config_class.__name__


def test_docker_run_error_is_one_class_in_both_modules():
    assert docker_runner.DockerRunError is errors.DockerRunError
    assert issubclass(errors.EgressSetupError, errors.DockerRunError)
