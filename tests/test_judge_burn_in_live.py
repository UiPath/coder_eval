"""Live burn-in tests for the typed verdict tool channel.

Exercises the ``submit_verdict`` channel against the three real backends:
LLMGW (LangChain ``.bind_tools()``), Bedrock (httpx + Anthropic-native tools),
and the Claude Code SDK (in-process MCP server). Each test ``pytest.skip``s
when the required credentials are not present, so the file is safe to run
in CI without a credential set.

Run just these tests:

    uv run pytest tests/test_judge_burn_in_live.py -m live -v

Skip them in the default suite:

    uv run pytest -m "not live"

Cost: each test makes one real LLM call. Models are pinned to Sonnet 4.6 /
Haiku 4.5 to keep per-run cost low.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from coder_eval.criteria import init_criteria
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    AgentJudgeCriterion,
    LLMJudgeCriterion,
    SandboxConfig,
    parse_agent_config,
)
from coder_eval.models.routing import BedrockRoute, DirectRoute
from coder_eval.sandbox import Sandbox


# Load credentials from the repo-root .env so credential checks below find them
# without requiring the caller to source .env first. ``override=False`` honors
# any pre-set environment variables (e.g. in CI).
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


# All tests in this file run only when explicitly selected. The autouse
# credential checks guarantee they ``pytest.skip`` rather than fail on a CI
# host without secrets.
pytestmark = pytest.mark.live


_JUDGE_PROMPT = (
    "The file ``hello.txt`` exists in your working directory. "
    "If its contents start with the string 'Hello' (case-sensitive), score 1.0. "
    "Otherwise score 0.0. Cite the file path and what you observed."
)


@pytest.fixture
def hello_sandbox(tmp_path: Path) -> Sandbox:
    (tmp_path / "hello.txt").write_text("Hello, world!\n")
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="burn-in")
    sb.sandbox_dir = tmp_path
    return sb


@pytest.fixture(autouse=True)
def _ensure_registry_initialized() -> None:
    init_criteria(validate=False)


def test_llm_judge_llmgw_tool_channel(hello_sandbox: Sandbox) -> None:
    """LLMGW route: ``.bind_tools(...)`` + forced ``tool_choice='submit_verdict'``."""
    if not os.environ.get("LLMGW_URL"):
        pytest.skip("LLMGW_URL not set")

    criterion = LLMJudgeCriterion(
        description="burn-in / LLMGW",
        prompt=_JUDGE_PROMPT,
        files=["hello.txt"],
        model="anthropic.claude-sonnet-4-6",
        temperature=0.0,
        max_tokens=400,
    )
    result = SuccessChecker(hello_sandbox, init_registry=False, route=DirectRoute(judge_transport="llmgw")).check(
        criterion
    )

    assert result.error is None, f"LLMGW judge failed: {result.error}\n{result.details}"
    assert result.score >= 0.7, f"score below threshold: {result.score}"


def test_llm_judge_bedrock_tool_channel(hello_sandbox: Sandbox) -> None:
    """Bedrock route: httpx POST with Anthropic-native ``tools`` + ``tool_choice``."""
    bearer = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    region = os.environ.get("AWS_REGION")
    if not bearer or not region:
        pytest.skip("AWS_BEARER_TOKEN_BEDROCK / AWS_REGION not set")

    criterion = LLMJudgeCriterion(
        description="burn-in / Bedrock",
        prompt=_JUDGE_PROMPT,
        files=["hello.txt"],
        model="anthropic.claude-sonnet-4-6",
        temperature=0.0,
        max_tokens=400,
    )
    result = SuccessChecker(
        hello_sandbox,
        init_registry=False,
        route=BedrockRoute(bearer_token=bearer, region=region),
    ).check(criterion)

    assert result.error is None, f"Bedrock judge failed: {result.error}\n{result.details}"
    assert result.score >= 0.7, f"score below threshold: {result.score}"


def test_agent_judge_sdk_tool_channel(hello_sandbox: Sandbox) -> None:
    """Claude Code SDK route: in-process ``submit_verdict`` MCP server."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("sk-ant-test-"):
        pytest.skip("ANTHROPIC_API_KEY not set (or placeholder)")

    criterion = AgentJudgeCriterion(
        description="burn-in / SDK",
        prompt=_JUDGE_PROMPT,
        max_turns=6,
        turn_timeout=120,
        agent=parse_agent_config(
            type="claude-code",
            model="claude-haiku-4-5-20251001",
            allowed_tools=["Read", "Bash"],
        ),
    )
    result = SuccessChecker(hello_sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    assert result.error is None, f"SDK judge failed: {result.error}\n{result.details}"
    assert result.score >= 0.7, f"score below threshold: {result.score}"
