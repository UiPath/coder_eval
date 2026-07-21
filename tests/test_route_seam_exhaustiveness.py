"""Guardrail: every ``ApiRoute`` member must be handled at every route-matching seam.

The route union is matched via ``match``/``isinstance`` at several seams
(``_build_sdk_env``, ``_format_routing``, ``ROUTE_NAMES``). A new route added
without extending each seam would silently no-op (``_format_routing`` /
recording) or raise (``_build_sdk_env``). This test iterates the union and
forces a fixture + per-seam check for every member, so adding a 4th route
fails here until each seam is extended.
"""

from __future__ import annotations

import typing

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import ROUTE_NAMES, ApiRoute, BedrockRoute, DirectRoute, LiteLLMRoute
from coder_eval.orchestrator import _format_routing


# One minimal instance per ApiRoute member. The set-equality assertion below
# forces this dict to grow whenever the union does.
_INSTANCES: list[object] = [
    DirectRoute(),
    BedrockRoute(bearer_token="t", region="eu-north-1", model="x"),
    LiteLLMRoute(base_url="http://localhost:4000", auth_token="k", model="m"),
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
