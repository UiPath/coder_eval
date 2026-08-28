"""Live integration test for the ``checker_context.api_route.route: litellm``
judge transport — hits a real gateway via ``litellm.acompletion()``.

Reuses the same ``CODEX_API_KEY``/``CODEX_BASE_URL``/``CODEX_MODEL`` secrets the
Codex live tests already exercise (a real OpenAI-protocol-compatible deployment)
instead of a dedicated new secret, so this regression check runs in CI for free.
Addresses the PR #137 review gap: "Nothing in the repo exercises the feature.
No task, no experiment variant." — this is the litellm-route equivalent of
``tests/test_codex_agent_live.py``, at the invoker level rather than a full
``coder-eval run`` (no sandbox / agent turn needed to exercise the transport).

Requirements:
  - The ``[litellm]`` extra installed (``uv sync --extra litellm``).
  - ``CODEX_API_KEY``, ``CODEX_BASE_URL``, ``CODEX_MODEL`` in the environment.

Run with: ``pytest -m live``.
"""

from __future__ import annotations

import os

import pytest


pytest.importorskip("litellm")

from coder_eval.evaluation.judge_litellm import invoke_litellm_judge_async
from coder_eval.evaluation.verdict_tool import SUBMIT_VERDICT_ANTHROPIC_TOOL, extract_verdict_from_openai_response
from coder_eval.models.routing import LiteLLMRoute


_live = pytest.mark.live


def _have_creds() -> bool:
    return bool(os.getenv("CODEX_API_KEY") and os.getenv("CODEX_BASE_URL") and os.getenv("CODEX_MODEL"))


_skip_reason = "Live litellm judge test needs [litellm] extra + CODEX_API_KEY/CODEX_BASE_URL/CODEX_MODEL"
pytestmark = [_live, pytest.mark.skipif(not _have_creds(), reason=_skip_reason)]


@_live
async def test_litellm_judge_route_scores_real_gateway_call():
    """Round-trips a real forced ``submit_verdict`` tool call through
    ``litellm.acompletion`` against the same gateway/model the Codex live tests
    use, with ``api_base``/``api_key`` resolved purely from ``env_params`` (no
    LITELLM_BASE_URL/LITELLM_AUTH_TOKEN involved) — the exact shape
    ``checker_context.api_route.route: litellm`` produces."""
    model = os.environ["CODEX_MODEL"]
    route = LiteLLMRoute(
        model=model,
        env_params={"api_base": "CODEX_BASE_URL", "api_key": "CODEX_API_KEY"},
    )
    response = await invoke_litellm_judge_async(
        route=route,
        model=model,
        system="You are a strict grader. Call the submit_verdict tool exactly once.",
        user=(
            'Score 1.0 if 2 + 2 = 4, else 0.0. Call submit_verdict with score=1.0, rationale="arithmetic is correct".'
        ),
        max_tokens=200,
        tool_spec=SUBMIT_VERDICT_ANTHROPIC_TOOL,
    )
    verdict, err = extract_verdict_from_openai_response(response)
    assert err is None, f"judge did not call submit_verdict: {err}"
    assert verdict is not None
    assert verdict.score == 1.0
