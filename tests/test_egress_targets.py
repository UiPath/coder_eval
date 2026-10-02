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
    AgentJudgeCriterion,
    ApiBackend,
    ApiRouteContext,
    CheckerContext,
    DockerDriverConfig,
    FileExistsCriterion,
    LLMJudgeCriterion,
    NoulQuestion,
    SandboxConfig,
    SimulationConfig,
    SystemOneJudgeCriterion,
    TaskDefinition,
    normalize_egress_target,
    parse_agent_config,
)
from coder_eval.plugins import ensure_plugins_loaded


DIRECT = ["api.anthropic.com:443", "platform.claude.com:443"]
BEDROCK = ["bedrock-runtime.eu-north-1.amazonaws.com:443", "bedrock.eu-north-1.amazonaws.com:443"]
OPENAI, GEMINI, OPENROUTER = "api.openai.com:443", "generativelanguage.googleapis.com:443", "openrouter.ai:443"
LITELLM = ApiBackend.LITELLM
BR = {"backend": ApiBackend.BEDROCK, "region": "eu-north-1"}
CC, CODEX, PI, OPENCODE = {"type": "claude-code"}, {"type": "codex"}, {"type": "pi"}, {"type": "opencode"}
LL_URL, CX_URL, REGION = "LITELLM_BASE_URL", "CODEX_BASE_URL", "AWS_REGION"
JUDGE = LLMJudgeCriterion(description="j", prompt="p")
QUESTIONS = {"q": NoulQuestion(instructions="i")}
SYSTEM_ONE = [
    SystemOneJudgeCriterion(description="j", questions=QUESTIONS),
    SystemOneJudgeCriterion(description="k", questions=QUESTIONS, base_url="https://gw.example:9443/v1"),
]
DIRECT_ROUTE = ApiRouteContext(route=ApiBackend.DIRECT)
EGRESS_LOG = "coder_eval.isolation.egress"


def _targets(
    agent: dict[str, Any] | None = CC,
    *,
    backend: ApiBackend = ApiBackend.DIRECT,
    region: str | None = None,
    env: dict[str, str] | None = None,
    criteria: list[Any] | None = None,
    simulation: bool | None = None,
    route: ApiRouteContext | None = None,
    bedrock_token: str | None = "tok",
    **docker: Any,
) -> list[str]:
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(**(agent or CC)),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(network="llm_only", **docker)),
        success_criteria=criteria or [FileExistsCriterion(description="c", path="x.txt")],
    )
    if agent is None:
        task.agent = None
    if simulation is not None:
        task.simulation = SimulationConfig(enabled=simulation, persona="p", goal="g")
    if route is not None:
        task.checker_context = CheckerContext(api_route=route)
    settings = Settings.model_construct(
        api_backend=backend,
        aws_region=region,
        aws_bearer_token_bedrock=bedrock_token if region else None,
        anthropic_api_key="key",
        litellm_base_url="http://gateway.invalid",
        litellm_auth_token="tok",
        litellm_model="m",
    )
    return resolve_egress_targets(task, env=env or {}, settings=settings)


HDI = ["host.docker.internal:4000"]
CASES: dict[str, tuple[dict[str, Any] | None, dict[str, Any], list[str]]] = {
    "direct-claude-code": (CC, {}, DIRECT),
    "bedrock-region-lowercased": (CC, {"backend": ApiBackend.BEDROCK, "region": "EU-North-1"}, BEDROCK),
    "unresolved-agent": (None, {}, []),
    "litellm-loopback": (CC, {"backend": LITELLM, "env": {LL_URL: "http://localhost:4000"}}, HDI),
    "litellm-ipv6-loopback": (CC, {"backend": LITELLM, "env": {LL_URL: "http://[::1]:4000"}}, HDI),
    "litellm-remote-port": (CC, {"backend": LITELLM, "env": {LL_URL: "https://l.corp:8443/v1"}}, ["l.corp:8443"]),
    "codex-base-url": (CODEX, {"env": {CX_URL: "https://x.openai.azure.com/v1"}}, ["x.openai.azure.com:443"]),
    "codex-plain-http": (CODEX, {"env": {CX_URL: "http://gw.example/v1"}}, ["gw.example:80"]),
    "claude-code-ignores-codex-url": (CC, {"env": {CX_URL: "https://x.openai.azure.com"}}, DIRECT),
    "replaced-passthrough": (CODEX, {"env": {CX_URL: "https://x.corp"}, "env_passthrough": []}, [OPENAI]),
    "forwarded-url-vars": (CC, {"env": {"MY_URL": "https://s.example"}, "env_passthrough_extra": ["MY_URL"]}, DIRECT),
    "user-allowlist": (CC, {"egress_allowlist": ["pypi.org", "API.anthropic.com"]}, sorted([*DIRECT, "pypi.org:443"])),
    "codex-off-bedrock": (CODEX, BR, [OPENAI]),
    "antigravity-off-bedrock": ({"type": "antigravity"}, BR, [GEMINI]),
    "pi-off-bedrock": (PI, BR, [OPENROUTER]),
    "delegate-off-bedrock": ({"type": "delegate"}, BR, []),
    "pi-anthropic": ({**PI, "model": "anthropic/claude-haiku-4-5"}, {}, ["api.anthropic.com:443"]),
    "pi-openrouter": ({**PI, "model": "openrouter/moonshotai/kimi-k3"}, {}, [OPENROUTER]),
    "pi-openai": ({**PI, "model": "openai/gpt-5"}, {}, [OPENAI]),
    "pi-google": ({**PI, "model": "google/gemini-3"}, {}, [GEMINI]),
    "pi-unknown-prefix": ({**PI, "model": "unknown/x"}, {}, [OPENROUTER]),
    "pi-no-prefix": ({**PI, "model": "no-prefix"}, {}, [OPENROUTER]),
    "opencode-no-model": (OPENCODE, {"backend": LITELLM}, []),
    "system-one-judge": (CC, {"backend": LITELLM, "criteria": SYSTEM_ONE}, ["api.typesafe.ai:443", "gw.example:9443"]),
    "judge-adds-backend": ({"type": "antigravity"}, {**BR, "criteria": [JUDGE]}, sorted([*BEDROCK, GEMINI])),
    "disabled-judge": (CODEX, {"criteria": [LLMJudgeCriterion(description="j", prompt="p", enabled=False)]}, [OPENAI]),
    "judge-route-override": (
        CODEX,
        {**BR, "criteria": [AgentJudgeCriterion(description="j", prompt="p")], "route": DIRECT_ROUTE},
        sorted([*DIRECT, OPENAI]),
    ),
    "simulation-enabled": (CODEX, {"simulation": True}, sorted([*DIRECT, OPENAI])),
    "simulation-disabled": (CODEX, {"simulation": False}, [OPENAI]),
    "litellm-simulator-pinned": (CODEX, {"backend": LITELLM, "simulation": True}, sorted([*DIRECT, OPENAI])),
}


@pytest.mark.parametrize(("agent", "kwargs", "expected"), list(CASES.values()), ids=list(CASES))
def test_resolved_targets(agent: dict[str, Any] | None, kwargs: dict[str, Any], expected: list[str]):
    assert _targets(agent, **kwargs) == expected


@pytest.mark.parametrize(
    ("agent", "kwargs", "variable"),
    [
        *(
            (CODEX, {"env": {CX_URL: url}}, CX_URL)
            for url in ["https://u:secret@[::2]", "http://[secret", "https://secret:70000"]
        ),
        (CC, {"backend": LITELLM, "env": {LL_URL: "http://localhost:secret"}}, LL_URL),
        (CC, {"backend": ApiBackend.BEDROCK, "region": "secret region"}, REGION),
    ],
)
def test_unallowlistable_value_raises_without_echoing_it(agent: dict[str, Any], kwargs: dict[str, Any], variable: str):
    with pytest.raises(ValueError, match=variable) as excinfo:
        _targets(agent, **kwargs)
    assert "secret" not in "".join(traceback.format_exception(excinfo.value))


@pytest.mark.parametrize(
    ("agent", "kwargs", "logger", "named", "hidden"),
    [
        (CC, {"backend": ApiBackend.BEDROCK}, EGRESS_LOG, REGION, None),
        (CC, {"backend": LITELLM}, EGRESS_LOG, LL_URL, None),
        (CC, {"bedrock_token": None, **BR}, EGRESS_LOG, "AWS_BEARER_TOKEN_BEDROCK", None),
        (CODEX, {"env": {CX_URL: "http://127.0.0.1:8080"}}, "coder_eval.models.sandbox", CX_URL, "8080"),
    ],
    ids=["bedrock-no-region", "litellm-no-url", "unconfigured-backend", "non-litellm-loopback"],
)
def test_unusable_route_warns_and_adds_nothing(agent, kwargs, logger, named, hidden, caplog):
    with caplog.at_level(logging.WARNING, logger=logger):
        assert _targets(agent, **kwargs) == []
    assert named in caplog.text
    assert hidden is None or hidden not in caplog.text


@pytest.mark.parametrize(("region", "expected"), [("eu-north-1", BEDROCK), (None, DIRECT)])
def test_litellm_agent_judge_and_simulator_get_the_pinned_claude_backend(region: str | None, expected: list[str]):
    env = {LL_URL: "https://gw.corp/v1"}
    targets = _targets(backend=LITELLM, region=region, env=env, criteria=[JUDGE], simulation=True)
    assert targets == sorted(["gw.corp:443", *expected])


@pytest.mark.parametrize(
    ("route", "env", "host"),
    [
        ({"params": {"api_base": "https://judge.example/v1"}}, {}, "judge.example:443"),
        ({"env_params": {"api_base": "JUDGE_BASE"}}, {"JUDGE_BASE": "https://j2.example"}, "j2.example:443"),
    ],
)
def test_litellm_judge_route_uses_its_own_api_base(route: dict[str, Any], env: dict[str, str], host: str):
    judge_route = ApiRouteContext(route=LITELLM, model="azure/x", **route)
    targets = _targets(CODEX, env=env, criteria=[JUDGE], route=judge_route, env_passthrough_extra=["JUDGE_BASE"])
    assert targets == [OPENAI, host]


def test_every_registered_agent_config_answers_with_normalized_targets():
    ensure_plugins_loaded()
    registrations = AgentRegistry.registrations()
    assert registrations
    for registration in registrations:
        hosts = registration.config_class.model_construct().egress_hosts({})
        assert isinstance(hosts, tuple), registration.config_class.__name__
        assert all(normalize_egress_target(host) == host for host in hosts), registration.config_class.__name__


def test_docker_run_error_is_one_class_in_both_modules():
    assert docker_runner.DockerRunError is errors.DockerRunError
    assert issubclass(errors.EgressSetupError, errors.DockerRunError)
