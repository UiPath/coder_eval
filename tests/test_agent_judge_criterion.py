"""Checker-level integration tests for agent_judge.

Parser-detail coverage lives in test_judge_verdict.py.
Sandbox-copy + subprocess-lifecycle coverage lives in test_sub_agent_runner.py.
This file covers the orchestration glue: prompt assembly, result mapping,
backend routing, and config propagation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.criteria import init_criteria
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    AgentJudgeCriterion,
    TurnRecord,
)
from coder_eval.models.routing import DirectRoute, ProxyRoute
from coder_eval.sandbox import Sandbox


# ClaudeCodeAgent is now imported inside SubAgentRunner; tests patch the runner's binding.
_AGENT_PATCH_PATH = "coder_eval.evaluation.sub_agent.ClaudeCodeAgent"


def _make_turn(agent_output: str, duration: float = 1.5) -> TurnRecord:
    return TurnRecord(
        iteration=1,
        user_input="(judge prompt)",
        agent_output=agent_output,
        duration_seconds=duration,
    )


def _make_mock_agent(agent_output: str) -> MagicMock:
    agent = MagicMock()
    agent.start = AsyncMock(return_value=None)
    agent.communicate = AsyncMock(return_value=_make_turn(agent_output))
    agent.stop = AsyncMock(return_value=None)
    agent.kill = AsyncMock(return_value=None)
    return agent


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    from coder_eval.models import SandboxConfig

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="agent_judge_test")
    sb.sandbox_dir = tmp_path
    return sb


@pytest.fixture(autouse=True)
def _ensure_registry_initialized() -> None:
    init_criteria(validate=False)


@pytest.fixture
def direct_route() -> DirectRoute:
    """Default route for unit tests — equivalent to ``api_backend=DIRECT``."""
    return DirectRoute()


# --- happy path + scoring end-to-end ---


def test_agent_judge_happy_path(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="grade the project", prompt="Is this code valid?")
    mock_agent = _make_mock_agent('{"score": 0.85, "rationale": "looks good"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert result.score == 0.85
    assert result.error is None
    assert "looks good" in (result.details or "")
    assert "duration:" in (result.details or "")


def test_agent_judge_score_clamped_high(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": 1.7, "rationale": "too high"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 1.0


def test_agent_judge_score_clamped_low(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": -0.4, "rationale": "negative"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0


@pytest.mark.parametrize("raw_score", ["NaN", "Infinity", "-Infinity"])
def test_agent_judge_rejects_non_finite(sandbox: Sandbox, direct_route: DirectRoute, raw_score: str) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent(f'{{"score": {raw_score}, "rationale": "oops"}}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "finite" in result.error


def test_agent_judge_missing_score_key(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"rationale": "forgot score"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "missing" in result.error


def test_agent_judge_parse_failure_prefixes_untrusted(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """Non-JSON judge output surfaces with an UNTRUSTED_JUDGE_OUTPUT: prefix in details."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent("I ran xmllint and everything looks great")
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "Failed to parse" in result.error
    assert (result.details or "").startswith("UNTRUSTED_JUDGE_OUTPUT:")


# --- timeout mapping ---


def test_agent_judge_turn_timeout_maps_to_zero(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    from coder_eval.errors.timeout import TurnTimeoutError

    criterion = AgentJudgeCriterion(description="x", prompt="grade", turn_timeout=30)
    mock_agent = _make_mock_agent("irrelevant")
    mock_agent.communicate.side_effect = TurnTimeoutError(30.0, task_id="t", iteration=1)

    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    assert result.score == 0.0
    assert result.error is not None
    assert "TurnTimeoutError" in result.error
    assert "timed out" in (result.details or "")


# --- config flow-through ---


def test_agent_judge_config_propagates(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        model="claude-sonnet-4-6",
        max_turns=3,
        turn_timeout=45,
        permission_mode="plan",
        allowed_tools=["Read", "Grep"],
        disallowed_tools=["Bash"],
    )
    mock_agent = _make_mock_agent('{"score": 1.0, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    (agent_config,) = mock_cls.call_args.args
    assert agent_config.model == "claude-sonnet-4-6"
    assert agent_config.permission_mode == "plan"
    assert agent_config.allowed_tools == ["Read", "Grep"]
    assert agent_config.disallowed_tools == ["Bash"]
    assert agent_config.setting_sources == []
    # max_turns and turn_timeout are passed as call-time args to communicate(),
    # not stored on AgentConfig.
    mock_agent.communicate.assert_awaited_once()
    kwargs = mock_agent.communicate.call_args.kwargs
    assert kwargs["timeout"] == 45.0
    assert kwargs["max_turns"] == 3


def test_agent_judge_rejects_turn_timeout_below_ten() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="turn_timeout"):
        AgentJudgeCriterion(description="x", prompt="grade", turn_timeout=5)


def test_agent_judge_default_ignores_include_claude_and_mcp() -> None:
    c = AgentJudgeCriterion(description="x", prompt="grade")
    assert ".claude" in c.ignore_patterns
    assert ".mcp.json" in c.ignore_patterns


# --- PROXY routing ---


def test_agent_judge_proxy_backend_routes_through_proxy(sandbox: Sandbox) -> None:
    """When SuccessChecker carries a ProxyRoute, the judge's sub-agent is
    constructed with that exact route — pointing at the orchestrator's
    running LLMGW proxy port."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": 0.9, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent) as mock_cls:
        result = SuccessChecker(sandbox, init_registry=False, route=ProxyRoute(port=12345)).check(criterion)

    assert result.error is None
    assert result.score == 0.9
    assert mock_cls.call_args.kwargs["route"] == ProxyRoute(port=12345)


def test_agent_judge_with_no_route_raises(sandbox: Sandbox) -> None:
    """agent_judge requires a route; without one the precondition assert surfaces
    as an error result (caught by @handle_criterion_errors)."""
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    with patch(_AGENT_PATCH_PATH) as mock_cls:
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    assert result.score == 0.0
    assert result.error is not None
    assert "agent_judge requires a route" in result.error
    mock_cls.assert_not_called()


def test_success_checker_threads_route_to_check(sandbox: Sandbox) -> None:
    """The route passed to SuccessChecker reaches arbitrary checkers as the
    ``route`` kwarg of ``BaseCriterion.check`` — not just agent_judge."""
    from coder_eval.criteria.file_exists import FileExistsChecker
    from coder_eval.models import FileExistsCriterion

    route = ProxyRoute(port=999)
    checker = SuccessChecker(sandbox, init_registry=False, route=route)
    criterion = FileExistsCriterion(description="x", path="anywhere")

    with patch.object(FileExistsChecker, "_check_impl", wraps=FileExistsChecker()._check_impl) as spy:
        checker.check(criterion)

    assert spy.call_args.kwargs["route"] == route


# --- prompt / context assembly ---


def test_agent_judge_missing_file_marker(sandbox: Sandbox, tmp_path: Path, direct_route: DirectRoute) -> None:
    (tmp_path / "present.py").write_text("x = 1")
    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        files=["present.py", "missing.py"],
    )
    mock_agent = _make_mock_agent('{"score": 0.5, "rationale": "partial"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    user_msg = mock_agent.communicate.call_args.args[0]
    assert "--- FILE: missing.py ---\n<file not found>" in user_msg
    assert "--- FILE: present.py ---" in user_msg


def test_agent_judge_parse_error_scrubs_reference_from_error_field(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """Parse errors can echo the raw score value — must be scrubbed before persisting.

    Regression for a review finding: if the judge returns a malformed verdict that
    stuffs the reference sentinel into the score field, the parser surfaces the raw
    value in its diagnostic. That diagnostic lands in `error` and must be scrubbed.
    """
    sentinel = "REFERENCE_LEAK_VIA_ERROR_777"
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_agent = _make_mock_agent(f'{{"score": "{sentinel}", "rationale": "ok"}}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(
            criterion, reference_code=sentinel
        )

    for field_value in (result.details, result.error):
        assert field_value is None or sentinel not in field_value


def test_agent_judge_include_reference_scrubbed_from_details(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    """Reference is shown to the judge but scrubbed from persisted details.

    Policy matches ``llm_judge`` — a misbehaving judge that echoes the reference in
    its rationale should not leak it into on-disk ``CriterionResult.details``.
    """
    sentinel = "SENTINEL_REFERENCE_999"
    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        include_reference=True,
    )
    mock_agent = _make_mock_agent(f'{{"score": 0.7, "rationale": "matches {sentinel}"}}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(
            criterion, reference_code=sentinel
        )

    user_msg = mock_agent.communicate.call_args.args[0]
    assert sentinel in user_msg
    assert sentinel not in (result.details or "")


def test_agent_judge_include_reference_false_omits_reference(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    sentinel = "SHOULD_NOT_APPEAR_42"
    criterion = AgentJudgeCriterion(description="x", prompt="grade")  # include_reference defaults False
    mock_agent = _make_mock_agent('{"score": 0.5, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, reference_code=sentinel)
    user_msg = mock_agent.communicate.call_args.args[0]
    assert sentinel not in user_msg


def test_agent_judge_include_agent_output_and_tool_calls(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    from datetime import datetime

    from coder_eval.models import CommandTelemetry

    command = CommandTelemetry(
        tool_name="Bash",
        tool_id="t1",
        timestamp=datetime.now(),
        parameters={"command": "uip rpa get-errors"},
        result_status="success",
        sequence_number=0,
    )
    turn = TurnRecord(
        iteration=1,
        user_input="create xaml",
        agent_output="Created the file successfully",
        commands=[command],
    )
    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        include_agent_output=True,
        include_tool_calls=True,
    )
    mock_agent = _make_mock_agent('{"score": 0.8, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, turn_records=[turn])

    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert "AGENT OUTPUT (UNTRUSTED" in user_msg
    assert "Created the file successfully" in user_msg
    assert "AGENT TOOL CALLS (UNTRUSTED" in user_msg
    assert "uip rpa get-errors" in user_msg


def test_agent_judge_include_dialog_renders_all_turns(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    turns = [
        TurnRecord(iteration=1, user_input="add a button", agent_output="added"),
        TurnRecord(iteration=2, user_input="make it red", agent_output="done"),
    ]
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_dialog=True)
    mock_agent = _make_mock_agent('{"score": 0.8, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, turn_records=turns)

    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert "DIALOG" in user_msg
    assert "simulated user" in user_msg
    assert "[Turn 1] USER:\nadd a button" in user_msg
    assert "[Turn 1] AGENT:\nadded" in user_msg
    assert "[Turn 2] USER:\nmake it red" in user_msg
    assert "[Turn 2] AGENT:\ndone" in user_msg


def test_agent_judge_include_dialog_no_turns_records_degraded_note(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    criterion = AgentJudgeCriterion(description="x", prompt="grade", include_dialog=True)
    mock_agent = _make_mock_agent('{"score": 0.5, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)

    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert "DIALOG" not in user_msg
    assert "include_dialog requested but no turn records available" in (result.details or "")


def test_agent_judge_include_dialog_omitted_when_false(sandbox: Sandbox, direct_route: DirectRoute) -> None:
    turn = TurnRecord(iteration=1, user_input="add a button", agent_output="added")
    criterion = AgentJudgeCriterion(description="x", prompt="grade")
    mock_agent = _make_mock_agent('{"score": 0.8, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion, turn_records=[turn])

    user_msg: str = mock_agent.communicate.call_args.args[0]
    assert "DIALOG" not in user_msg
    assert "add a button" not in user_msg


def test_agent_judge_surfaces_degraded_notes_when_turn_records_missing(
    sandbox: Sandbox, direct_route: DirectRoute
) -> None:
    criterion = AgentJudgeCriterion(
        description="x",
        prompt="grade",
        include_agent_output=True,
        include_tool_calls=True,
    )
    mock_agent = _make_mock_agent('{"score": 0.9, "rationale": "ok"}')
    with patch(_AGENT_PATCH_PATH, return_value=mock_agent):
        result = SuccessChecker(sandbox, init_registry=False, route=direct_route).check(criterion)
    assert result.score == 0.9
    details = result.details or ""
    assert "include_agent_output requested but no turn records available" in details
    assert "include_tool_calls requested but no turn records available" in details
    assert "duration:" in details


# --- integration (opt-in, real SDK) ---


@pytest.mark.integration
def test_agent_judge_integration_real_sdk(tmp_path: Path) -> None:
    """Smoke test that actually spawns a Claude Code SDK agent."""
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("sk-ant-test-"):
        pytest.skip("Needs a real ANTHROPIC_API_KEY to talk to the SDK")

    from coder_eval.models import SandboxConfig

    (tmp_path / "hello.txt").write_text("Hello, world!\n")
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="agent_judge_integration")
    sb.sandbox_dir = tmp_path

    criterion = AgentJudgeCriterion(
        description="integration smoke",
        prompt=(
            "Check hello.txt. If it contains exactly 'Hello, world!' return score=1.0, "
            "otherwise 0.0. Reply with ONLY the JSON verdict."
        ),
        files=["hello.txt"],
        model="claude-haiku-4-5-20251001",
        max_turns=6,
        turn_timeout=90,
        allowed_tools=["Read"],
    )
    result = SuccessChecker(sb, init_registry=False, route=DirectRoute()).check(criterion)

    assert result.error is None, f"integration judge failed: {result.error}\n{result.details}"
    assert result.score >= 0.7
