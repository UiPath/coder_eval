"""Guardrail: every ``ApiRoute`` member must be handled at every route-matching seam.

The route union is matched via ``match``/``isinstance`` at several seams:
``_build_sdk_env``, ``_format_routing``, ``ROUTE_NAMES``, the ``llm_judge``
dispatch (``_invoke_tool_channel``), and ``Orchestrator._record_route_environment_info``.
A new route added without extending each seam would silently no-op
(``_record_route_environment_info``), silently mis-score (the judge dispatch), or
raise (``_build_sdk_env``). This test iterates the union and forces a fixture +
per-seam check for every member, so adding a 4th route fails here until each seam
is extended. (``_record_route_environment_info`` uses ``if/elif isinstance`` — not
a ``match`` — so pyright does NOT flag a missing route there; this runtime test is
its only guard.)
"""

from __future__ import annotations

import typing
from types import SimpleNamespace
from unittest.mock import MagicMock

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.criteria import llm_judge
from coder_eval.criteria.llm_judge import _invoke_tool_channel
from coder_eval.models import ROUTE_NAMES, ApiRoute, BedrockRoute, DirectRoute, LiteLLMRoute
from coder_eval.orchestrator import Orchestrator, _format_routing


# One minimal instance per ApiRoute member. The set-equality assertion below
# forces this dict to grow whenever the union does.
_INSTANCES: list[object] = [
    DirectRoute(),
    BedrockRoute(region="eu-north-1", model="x"),
    LiteLLMRoute(model="m"),
]


def test_fixtures_cover_every_union_member():
    """Fails when a new ApiRoute member is added without a fixture here."""
    assert {type(r) for r in _INSTANCES} == set(typing.get_args(ApiRoute))


def test_every_route_has_a_route_name():
    for r in _INSTANCES:
        assert type(r) in ROUTE_NAMES


def test_build_sdk_env_handles_every_route():
    """_build_sdk_env raises AssertionError on an unhandled route — must not for any member."""
    for r in _INSTANCES:
        env, _model = ClaudeCodeAgent._build_sdk_env(r)  # type: ignore[arg-type]
        assert isinstance(env, dict)


def test_format_routing_handles_every_route():
    """No silent no-op: each route formats to a non-empty string led by its ROUTE_NAMES value."""
    for r in _INSTANCES:
        out = _format_routing(r)  # type: ignore[arg-type]
        assert out and out.startswith(ROUTE_NAMES[type(r)])


async def test_invoke_tool_channel_handles_every_route(monkeypatch):
    """The llm_judge dispatch must handle every route type — an unhandled member
    would fall past all cases and raise UnboundLocalError. Network-touching arms
    (Bedrock/Direct) are stubbed so this only checks dispatch coverage."""

    async def _stub_bedrock(**_: object) -> dict[str, object]:
        return {}

    async def _stub_anthropic(**_: object) -> dict[str, object]:
        return {}

    async def _stub_litellm(**_: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(llm_judge, "invoke_bedrock_judge_async", _stub_bedrock)
    monkeypatch.setattr(llm_judge, "invoke_anthropic_judge_async", _stub_anthropic)
    monkeypatch.setattr(llm_judge, "invoke_litellm_judge_async", _stub_litellm)
    monkeypatch.setattr(llm_judge, "extract_verdict_from_anthropic_response", lambda _resp: (None, "stub"))
    monkeypatch.setattr(llm_judge, "token_usage_from_anthropic_dict", lambda _resp, **_kwargs: None)
    criterion = MagicMock()
    for r in _INSTANCES:
        result = await _invoke_tool_channel(criterion=criterion, model="m", route=r, system_msg="s", user_msg="u")  # type: ignore[arg-type]
        # (verdict, parse_error, raw_text, response_usage) — a 4-tuple means the
        # route matched an explicit arm rather than falling through.
        assert isinstance(result, tuple) and len(result) == 4


def test_record_route_environment_info_handles_every_route():
    """Orchestrator._record_route_environment_info records a route-specific
    dimension for every route (not just the generic api_routing key). A new route
    missing from its if/elif would record only api_routing — caught here since
    pyright can't check the isinstance chain."""
    for r in _INSTANCES:
        fake = SimpleNamespace(route=r, eval_route=r, result=SimpleNamespace(environment_info={}), agent=None)
        Orchestrator._record_route_environment_info(fake)  # type: ignore[arg-type]
        env = fake.result.environment_info
        assert env.get("api_routing") == ROUTE_NAMES[type(r)]
        assert len(env) > 1, f"{type(r).__name__} recorded no route-specific env info"
