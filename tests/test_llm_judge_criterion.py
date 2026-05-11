"""Tests for the llm_judge success criterion."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.criteria import CriterionRegistry, init_criteria
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    CommandTelemetry,
    FileExistsCriterion,
    LLMJudgeCriterion,
    TurnRecord,
)
from coder_eval.sandbox import Sandbox


def _make_mock_llm(content: str) -> MagicMock:
    """Return a MagicMock whose .invoke() returns an object with a .content string."""
    response = MagicMock()
    response.content = content
    llm = MagicMock()
    llm.invoke.return_value = response
    return llm


def _make_turn(agent_output: str = "", commands: list[CommandTelemetry] | None = None) -> TurnRecord:
    return TurnRecord(
        iteration=1,
        user_input="test",
        agent_output=agent_output,
        commands=commands or [],
    )


def _make_cmd(tool_name: str, params: dict[str, Any], status: str = "success", seq: int = 0) -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=f"tool_{seq}",
        timestamp=datetime.now(),
        parameters=params,
        result_status=status,
        sequence_number=seq,
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    """Create a real Sandbox rooted at tmp_path (no setup needed for file_exists/get_file_content)."""
    from coder_eval.models import SandboxConfig

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="judge_test")
    sb.sandbox_dir = tmp_path
    return sb


@pytest.fixture(autouse=True)
def _ensure_registry_initialized():
    """Ensure the criteria registry has discovered llm_judge before each test."""
    init_criteria(validate=False)


# --- happy path + scoring edge cases ---


def test_judge_happy_path_files_only(sandbox: Sandbox, tmp_path: Path) -> None:
    """Single file, no trajectory toggles — mocked LLM returns a valid verdict."""
    (tmp_path / "main.py").write_text("print('hello')")

    criterion = LLMJudgeCriterion(
        description="grade main.py",
        prompt="Is this code valid?",
        files=["main.py"],
    )
    mock_llm = _make_mock_llm('{"score": 0.85, "rationale": "mostly correct"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        checker = SuccessChecker(sandbox, init_registry=False)
        result = checker.check(criterion)

    assert result.score == 0.85
    assert result.error is None
    assert "mostly correct" in (result.details or "")


def test_judge_score_clamped_high(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm('{"score": 1.7, "rationale": "too high"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 1.0


def test_judge_score_clamped_low(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm('{"score": -0.3, "rationale": "too low"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 0.0


def test_judge_parse_failure(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm("not json at all")
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "Failed to parse" in result.error


def test_judge_non_numeric_score(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm('{"score": "great", "rationale": "oops"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "not a number" in result.error


def test_judge_missing_score_key(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm('{"rationale": "forgot score"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "missing" in result.error


@pytest.mark.parametrize("raw_score", ["NaN", "Infinity", "-Infinity"])
def test_judge_rejects_non_finite_score(sandbox: Sandbox, raw_score: str) -> None:
    """Non-finite scores (NaN, +/-Infinity) must NOT pass through clamping.

    NaN comparisons return False, so `max(0.0, min(1.0, nan))` silently yields
    1.0 — i.e., a perfect score for a garbage verdict. Reject explicitly instead.
    """
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm(f'{{"score": {raw_score}, "rationale": "oops"}}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "finite" in result.error


# --- prompt content / context assembly ---


def test_judge_missing_file_marker(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "present.py").write_text("x = 1")

    criterion = LLMJudgeCriterion(
        description="x",
        prompt="grade",
        files=["present.py", "missing.py"],
    )
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "partial"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    call_args = mock_llm.invoke.call_args
    messages = call_args.args[0]
    user_msg = messages[1]["content"]
    assert "--- FILE: missing.py ---\n<file not found>" in user_msg
    assert "missing_files: ['missing.py']" in (result.details or "")


def test_judge_llm_exception_maps_to_score_zero(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = MagicMock()
    mock_llm.invoke.side_effect = RuntimeError("gateway down")
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "gateway down" in result.error


def test_judge_include_reference_true_keeps_reference_in_prompt_only(sandbox: Sandbox) -> None:
    sentinel = "SENTINEL_REFERENCE_STRING_42"
    criterion = LLMJudgeCriterion(
        description="x",
        prompt="grade",
        include_reference=True,
    )
    mock_llm = _make_mock_llm('{"score": 0.7, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, reference_code=sentinel)

    call_args = mock_llm.invoke.call_args
    user_msg = call_args.args[0][1]["content"]
    assert sentinel in user_msg
    assert sentinel not in (result.details or "")


def test_judge_include_reference_true_no_reference_set(sandbox: Sandbox) -> None:
    """Toggle on but no reference provided — judge still runs, no error, no reference section."""
    criterion = LLMJudgeCriterion(
        description="x",
        prompt="grade",
        include_reference=True,
    )
    mock_llm = _make_mock_llm('{"score": 0.4, "rationale": "no ref"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, reference_code=None)

    call_args = mock_llm.invoke.call_args
    user_msg = call_args.args[0][1]["content"]
    assert "REFERENCE SOLUTION" not in user_msg
    assert result.error is None


def test_judge_include_reference_false_omits_reference(sandbox: Sandbox) -> None:
    sentinel = "ANOTHER_SENTINEL_987"
    # include_reference defaults to True now; opt out explicitly when the reference
    # is for non-judge consumers (reference_comparison) and shouldn't reach the LLM.
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=False)
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion, reference_code=sentinel)

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert sentinel not in user_msg


def test_judge_include_agent_output_true(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_agent_output=True)
    turn = _make_turn(agent_output="I did X")
    mock_llm = _make_mock_llm('{"score": 0.6, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=[turn])

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert "AGENT OUTPUT (UNTRUSTED DATA" in user_msg
    assert "I did X" in user_msg


def test_judge_include_tool_calls_true(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_tool_calls=True)
    cmd = _make_cmd(tool_name="Bash", params={"command": "ls"})
    turn = _make_turn(commands=[cmd])
    mock_llm = _make_mock_llm('{"score": 0.55, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=[turn])

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert "AGENT TOOL CALLS" in user_msg
    assert "Bash" in user_msg
    assert "ls" in user_msg


def test_judge_include_dialog_renders_all_turns(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_dialog=True)
    turns = [
        TurnRecord(iteration=1, user_input="add a button", agent_output="added"),
        TurnRecord(iteration=2, user_input="make it red", agent_output="done"),
    ]
    mock_llm = _make_mock_llm('{"score": 0.8, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=turns)

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert "DIALOG" in user_msg
    assert "simulated user" in user_msg  # rubric guard against false hallucination calls
    assert "[Turn 1] USER:\nadd a button" in user_msg
    assert "[Turn 1] AGENT:\nadded" in user_msg
    assert "[Turn 2] USER:\nmake it red" in user_msg
    assert "[Turn 2] AGENT:\ndone" in user_msg


def test_judge_include_dialog_omitted_when_false(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    turn = TurnRecord(iteration=1, user_input="add a button", agent_output="added")
    mock_llm = _make_mock_llm('{"score": 0.8, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=[turn])

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert "DIALOG" not in user_msg
    assert "add a button" not in user_msg


def test_judge_include_dialog_no_turns_records_degraded_note(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_dialog=True)
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=None)

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert "DIALOG" not in user_msg
    assert "include_dialog" in (result.details or "")


def test_judge_trajectory_toggles_without_turn_records(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(
        description="x",
        prompt="grade",
        include_agent_output=True,
        include_tool_calls=True,
    )
    mock_llm = _make_mock_llm('{"score": 0.3, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=None)

    assert result.error is None
    details = result.details or ""
    assert "notes:" in details
    assert "include_agent_output" in details
    assert "include_tool_calls" in details


def test_judge_trajectory_toggles_with_empty_commands(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_tool_calls=True)
    turn = _make_turn(commands=[])
    mock_llm = _make_mock_llm('{"score": 0.4, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=[turn])

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    # When commands are empty, summarize_commands returns None -> the whole section is omitted.
    assert "AGENT TOOL CALLS" not in user_msg


# --- truncation ---


def test_judge_file_truncation(sandbox: Sandbox, tmp_path: Path) -> None:
    big = "x" * 50_000
    (tmp_path / "big.py").write_text(big)
    limit = 1000
    criterion = LLMJudgeCriterion(
        description="x",
        prompt="grade",
        files=["big.py"],
        max_file_chars=limit,
    )
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion)

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert "... (truncated, orig 50000 chars)" in user_msg
    # The rendered file payload should carry exactly `limit` characters of content before the marker.
    block_start = user_msg.index("--- FILE: big.py ---\n") + len("--- FILE: big.py ---\n")
    marker_idx = user_msg.index("\n... (truncated", block_start)
    payload = user_msg[block_start:marker_idx]
    assert len(payload) == limit
    assert set(payload) == {"x"}


def test_judge_agent_output_truncation(sandbox: Sandbox) -> None:
    limit = 100
    long_output = "y" * 5_000
    criterion = LLMJudgeCriterion(
        description="x",
        prompt="grade",
        include_agent_output=True,
        max_file_chars=limit,
    )
    turn = _make_turn(agent_output=long_output)
    mock_llm = _make_mock_llm('{"score": 0.2, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=[turn])

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert "... (truncated, orig 5000 chars)" in user_msg


# --- integration: weighted scoring + registry ---


def test_judge_counts_toward_weighted_score(sandbox: Sandbox, tmp_path: Path) -> None:
    """A passing file_exists (weight=1) + llm_judge returning 0.5 (weight=2) => 2/3."""
    from coder_eval.models import EvaluationResult

    (tmp_path / "present.py").write_text("x = 1")

    fe = FileExistsCriterion(path="present.py", description="must exist", weight=1.0)
    judge = LLMJudgeCriterion(
        description="rate it",
        prompt="grade this code",
        files=["present.py"],
        weight=2.0,
    )
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "half"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        checker = SuccessChecker(sandbox, init_registry=False)
        results = checker.check_all([fe, judge])

    evaluation = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type="claude-code",
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=1,
        environment_info={},
        success_criteria_results=results,
    )
    evaluation.calculate_weighted_score([fe, judge])
    total_weight = fe.weight + judge.weight
    expected = (fe.weight * 1.0 + judge.weight * 0.5) / total_weight
    assert evaluation.weighted_score == pytest.approx(expected)


def test_judge_registry_autodiscovered() -> None:
    init_criteria(validate=True)
    assert "llm_judge" in CriterionRegistry.list_types()


def test_judge_reference_not_in_details(sandbox: Sandbox) -> None:
    sentinel = "REFERENCE_LEAK_CANARY_XYZ"
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_llm = _make_mock_llm('{"score": 0.9, "rationale": "great"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, reference_code=sentinel)

    # Explicit leak check across every field CriterionResult exposes.
    for field_value in (result.description, result.details, result.error):
        assert field_value is None or sentinel not in field_value


def test_judge_parse_error_scrubs_reference_from_error_field(sandbox: Sandbox) -> None:
    """Parse errors can echo the raw score value — must be scrubbed before persisting.

    If the judge returns ``{"score": "<reference code>", ...}``, the parser's
    diagnostic includes the raw score value. That diagnostic lands in ``error`` and
    must be scrubbed the same way ``details`` is.
    """
    sentinel = "REFERENCE_LEAK_VIA_ERROR_456"
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_llm = _make_mock_llm(f'{{"score": "{sentinel}", "rationale": "ok"}}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, reference_code=sentinel)

    for field_value in (result.details, result.error):
        assert field_value is None or sentinel not in field_value


def test_judge_reference_not_leaked_on_parse_failure(sandbox: Sandbox) -> None:
    """If the model echoes the reference in an unparseable response, details must not leak it."""
    sentinel = "REFERENCE_CANARY_ECHOED_789"
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True)
    # Unparseable response that mentions the reference — simulates a misbehaving model.
    mock_llm = _make_mock_llm(f"Sorry, here is what you gave me: {sentinel}. no json")
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, reference_code=sentinel)

    assert result.score == 0.0
    for field_value in (result.description, result.details, result.error):
        assert field_value is None or sentinel not in field_value


# --- route dispatch ---


def test_judge_no_route_falls_back_to_llmgw(sandbox: Sandbox) -> None:
    """No route -> LLMGW path; Bedrock and Anthropic invokers are NOT called."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm('{"score": 0.6, "rationale": "ok"}')
    with (
        patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm) as m_llmgw,
        patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge") as m_bedrock,
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge") as m_anthropic,
    ):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 0.6
    assert m_llmgw.call_count == 1
    assert m_bedrock.call_count == 0
    assert m_anthropic.call_count == 0


def test_judge_bedrock_route_uses_bedrock_invoker(sandbox: Sandbox) -> None:
    from coder_eval.models.routing import BedrockRoute

    route = BedrockRoute(bearer_token="t", region="eu-north-1")
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with (
        patch(
            "coder_eval.criteria.llm_judge.invoke_bedrock_judge", return_value='{"score":0.7,"rationale":"ok"}'
        ) as m_bedrock,
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge") as m_anthropic,
        patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model") as m_llmgw,
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert result.score == 0.7
    m_bedrock.assert_called_once()
    kwargs = m_bedrock.call_args.kwargs
    assert kwargs["route"] is route
    assert kwargs["model"] == criterion.model
    assert kwargs["temperature"] == criterion.temperature
    assert kwargs["max_tokens"] == criterion.max_tokens
    assert m_anthropic.call_count == 0
    assert m_llmgw.call_count == 0


def test_judge_direct_route_uses_anthropic_invoker(sandbox: Sandbox) -> None:
    from coder_eval.models.routing import DirectRoute

    route = DirectRoute()
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with (
        patch(
            "coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value='{"score":0.5,"rationale":"ok"}'
        ) as m_anthropic,
        patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge") as m_bedrock,
        patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model") as m_llmgw,
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert result.score == 0.5
    m_anthropic.assert_called_once()
    assert m_anthropic.call_args.kwargs["route"] is route
    assert m_bedrock.call_count == 0
    assert m_llmgw.call_count == 0


def test_judge_proxy_route_uses_anthropic_invoker(sandbox: Sandbox) -> None:
    from coder_eval.models.routing import ProxyRoute

    route = ProxyRoute(port=12345)
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with (
        patch(
            "coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value='{"score":0.4,"rationale":"ok"}'
        ) as m_anthropic,
        patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge") as m_bedrock,
        patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model") as m_llmgw,
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert result.score == 0.4
    m_anthropic.assert_called_once()
    assert m_anthropic.call_args.kwargs["route"] is route
    assert m_bedrock.call_count == 0
    assert m_llmgw.call_count == 0


def test_judge_bedrock_invoke_runtime_error_maps_to_score_zero(sandbox: Sandbox) -> None:
    from coder_eval.models.routing import BedrockRoute

    route = BedrockRoute(bearer_token="t", region="eu-north-1")
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with patch(
        "coder_eval.criteria.llm_judge.invoke_bedrock_judge",
        side_effect=RuntimeError("Bedrock invoke failed: 403 forbidden"),
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "Bedrock invoke failed" in result.error


def test_judge_anthropic_invoke_runtime_error_maps_to_score_zero(sandbox: Sandbox) -> None:
    from coder_eval.models.routing import DirectRoute

    route = DirectRoute()
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with patch(
        "coder_eval.criteria.llm_judge.invoke_anthropic_judge",
        side_effect=RuntimeError("Anthropic API connection refused"),
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "connection refused" in result.error


def test_judge_bedrock_route_threads_model_unchanged(sandbox: Sandbox) -> None:
    """Translation happens INSIDE the helper, not at the dispatch site."""
    from coder_eval.models.routing import BedrockRoute

    route = BedrockRoute(bearer_token="t", region="eu-north-1")
    criterion = LLMJudgeCriterion(description="x", prompt="grade", model="anthropic.claude-opus-4-6-v1")
    with patch(
        "coder_eval.criteria.llm_judge.invoke_bedrock_judge",
        return_value='{"score":0.9,"rationale":"ok"}',
    ) as m_bedrock:
        SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert m_bedrock.call_args.kwargs["model"] == "anthropic.claude-opus-4-6-v1"


# --- verbose verdict + transcript persistence ---


def test_judge_persists_findings(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    verdict = (
        '{"score": 0.7, "rationale": "ok", '
        '"findings": ["main.py:5 missing return — issue", "no docstrings — minor deviation"]}'
    )
    mock_llm = _make_mock_llm(verdict)
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    assert result.score == 0.7
    assert getattr(result, "findings", []) == [
        "main.py:5 missing return — issue",
        "no docstrings — minor deviation",
    ]


def test_judge_prompt_requires_findings(sandbox: Sandbox) -> None:
    """The system + user prompts must instruct the model to emit findings."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        SuccessChecker(sandbox, init_registry=False).check(criterion)

    messages = mock_llm.invoke.call_args.args[0]
    system_msg = messages[0]["content"]
    user_msg = messages[1]["content"]
    assert "findings" in system_msg.lower()
    assert "findings" in user_msg.lower()
    # Analysis was dropped — must NOT appear in the prompts.
    assert "analysis" not in system_msg.lower()
    assert "analysis" not in user_msg.lower()


def test_judge_transcript_captures_raw_verdict_by_default(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    raw = '{"score": 0.5, "rationale": "ok", "findings": ["a"]}'
    mock_llm = _make_mock_llm(raw)
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert transcript.raw_verdict == raw
    assert transcript.tool_calls == []  # llm_judge has no tool calls


def test_judge_capture_transcript_false_drops_transcript(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", capture_transcript=False)
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    assert getattr(result, "transcript", None) is None
    assert result.score == 0.5  # capture flag only gates the transcript, not the verdict


def test_judge_transcript_truncation_marks_truncated(sandbox: Sandbox) -> None:
    """raw_verdict longer than max_transcript_chars gets clipped and the flag flips."""
    big_finding = "x" * 5000
    raw = f'{{"score": 0.5, "rationale": "ok", "findings": ["{big_finding}"]}}'
    criterion = LLMJudgeCriterion(description="x", prompt="grade", max_transcript_chars=200)
    mock_llm = _make_mock_llm(raw)
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert transcript.truncated is True
    assert len(transcript.raw_verdict) < len(raw)


def test_judge_scrubs_reference_from_findings(sandbox: Sandbox) -> None:
    """If the model echoes the reference in findings/raw_verdict, those must be scrubbed."""
    sentinel = "REF_LEAK_VIA_FINDINGS"
    raw = f'{{"score": 0.5, "rationale": "ok", "findings": ["echoed {sentinel} in main.py"]}}'
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_llm = _make_mock_llm(raw)
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, reference_code=sentinel)

    for finding in getattr(result, "findings", []) or []:
        assert sentinel not in finding
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert sentinel not in transcript.raw_verdict


def test_judge_legacy_two_field_verdict_still_parses(sandbox: Sandbox) -> None:
    """A model that ignores the findings instructions and emits the old two-field
    form must still parse — findings defaults empty, score/rationale stand."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm('{"score": 0.42, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    assert result.score == 0.42
    assert result.error is None
    assert getattr(result, "findings", []) == []


def test_judge_parse_failure_still_carries_transcript(sandbox: Sandbox) -> None:
    """Even on parse failure we want the raw response captured for audit."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    mock_llm = _make_mock_llm("totally not json")
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    assert result.score == 0.0
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert "totally not json" in transcript.raw_verdict


# --- enabled flag (master skip) ---


def test_judge_enabled_false_short_circuits(sandbox: Sandbox) -> None:
    """enabled=False: no LLM call, returns a skipped result with score=1.0."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", enabled=False)
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "should not be called"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm) as factory:
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    assert result.score == 1.0
    assert "skipped" in (result.details or "").lower()
    assert "enabled=false" in (result.details or "").lower()
    factory.assert_not_called()
    mock_llm.invoke.assert_not_called()


def test_judge_enabled_default_true(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    assert criterion.enabled is True


# --- judge prompt + system prompt capture ---


def test_judge_transcript_captures_prompts(sandbox: Sandbox) -> None:
    """The rendered user message + system prompt land on the transcript so reviewers
    can see exactly what the judge was told."""
    criterion = LLMJudgeCriterion(description="x", prompt="rubric body XYZ")
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert "rubric body XYZ" in transcript.judge_prompt
    assert "GRADING PROMPT:" in transcript.judge_prompt
    assert "strict code reviewer" in transcript.judge_system_prompt.lower()


def test_judge_prompt_capture_scrubs_reference(sandbox: Sandbox) -> None:
    """The reference solution sits inside the judge prompt — must be scrubbed before persistence."""
    sentinel = "REF_LEAK_VIA_PROMPT_111"
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True)
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, reference_code=sentinel)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    # Confirm the reference reached the judge but is scrubbed from persisted prompt.
    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert sentinel in user_msg
    assert sentinel not in transcript.judge_prompt
    assert sentinel not in transcript.judge_system_prompt


# --- legacy in-table tests continue ---


def test_judge_agent_output_empty_does_not_emit_block(sandbox: Sandbox) -> None:
    """include_agent_output=True with empty agent_output must not produce an empty header block,
    but MUST record a degradation note so the caller can spot the missing context."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_agent_output=True)
    turn = _make_turn(agent_output="")
    mock_llm = _make_mock_llm('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.get_llmgw_chat_model", return_value=mock_llm):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion, turn_records=[turn])

    user_msg = mock_llm.invoke.call_args.args[0][1]["content"]
    assert "AGENT OUTPUT" not in user_msg
    details = result.details or ""
    assert "notes:" in details
    assert "latest agent output is empty" in details
