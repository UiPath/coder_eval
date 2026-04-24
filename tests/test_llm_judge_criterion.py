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
    criterion = LLMJudgeCriterion(description="x", prompt="grade")  # default False
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
