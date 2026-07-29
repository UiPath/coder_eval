"""Tests for the llm_judge success criterion."""

from __future__ import annotations

import json
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from coder_eval.criteria import CriterionRegistry, init_criteria
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    CommandTelemetry,
    DirectRoute,
    FileExistsCriterion,
    LLMJudgeCriterion,
    TurnRecord,
)
from coder_eval.sandbox import Sandbox


def _make_judge_response(content: str) -> dict:
    """Return an Anthropic-shaped response dict the judge's dict extractor parses.

    The judge now dispatches through ``invoke_anthropic_judge`` /
    ``invoke_bedrock_judge``, both of which return an Anthropic-native message
    dict (content blocks). ``extract_verdict_from_anthropic_response`` walks
    ``response["content"]`` for a ``submit_verdict`` ``tool_use`` block and
    validates its ``input`` against ``JudgeVerdict``.

    Legacy tests pass JSON-formatted strings; this helper parses them into a
    synthetic ``submit_verdict`` tool_use block so the criterion sees the same
    verdict. The parsed dict is passed through verbatim even when malformed
    (non-numeric / missing / NaN score) so the extractor produces the same
    "not a number" / "missing" / "finite" diagnostics the legacy parser did
    (``json.loads`` accepts ``NaN`` / ``Infinity``).

    Non-JSON content surfaces as a plain ``text`` block — the extractor then
    reports "Judge did not call submit_verdict", equivalent to the old
    no-tool-call path.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {"content": [{"type": "text", "text": content}]}
    return {"content": [{"type": "tool_use", "name": "submit_verdict", "input": data}]}


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
    """Single file, no trajectory toggles — mocked judge returns a valid verdict."""
    (tmp_path / "main.py").write_text("print('hello')")

    criterion = LLMJudgeCriterion(
        description="grade main.py",
        prompt="Is this code valid?",
        files=["main.py"],
    )
    resp = _make_judge_response('{"score": 0.85, "rationale": "mostly correct"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        checker = SuccessChecker(sandbox, init_registry=False, route=DirectRoute())
        result = checker.check(criterion)

    assert result.score == 0.85
    assert result.error is None
    assert "mostly correct" in (result.details or "")


def test_judge_score_clamped_high(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": 1.7, "rationale": "too high"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 1.0


def test_judge_score_clamped_low(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": -0.3, "rationale": "too low"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.0


def test_judge_no_tool_call_maps_to_score_zero(sandbox: Sandbox) -> None:
    """When the model emits text instead of a submit_verdict tool call, score=0 with diagnostic."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response("not json at all")
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.0
    assert result.error == "Judge did not call submit_verdict"


def test_judge_non_numeric_score(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": "great", "rationale": "oops"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "not a number" in result.error


def test_judge_missing_score_key(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"rationale": "forgot score"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
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
    resp = _make_judge_response(f'{{"score": {raw_score}, "rationale": "oops"}}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
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
    resp = _make_judge_response('{"score": 0.5, "rationale": "partial"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert "--- FILE: missing.py ---\n<file not found>" in user_msg
    assert "missing_files: ['missing.py']" in (result.details or "")


def test_judge_llm_exception_maps_to_score_zero(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with patch(
        "coder_eval.criteria.llm_judge.invoke_anthropic_judge",
        side_effect=RuntimeError("gateway down"),
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
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
    resp = _make_judge_response('{"score": 0.7, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(
            criterion, reference_code=sentinel
        )

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert sentinel in user_msg
    assert sentinel not in (result.details or "")


def test_judge_include_reference_true_no_reference_set(sandbox: Sandbox) -> None:
    """Toggle on but no reference provided — judge still runs, no error, no reference section."""
    criterion = LLMJudgeCriterion(
        description="x",
        prompt="grade",
        include_reference=True,
    )
    resp = _make_judge_response('{"score": 0.4, "rationale": "no ref"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, reference_code=None)

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert "REFERENCE SOLUTION" not in user_msg
    assert result.error is None


def test_judge_include_reference_false_omits_reference(sandbox: Sandbox) -> None:
    sentinel = "ANOTHER_SENTINEL_987"
    # include_reference defaults to True now; opt out explicitly when the reference
    # is for non-judge consumers (reference_comparison) and shouldn't reach the LLM.
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=False)
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, reference_code=sentinel)

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert sentinel not in user_msg


def test_judge_include_agent_output_true(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_agent_output=True)
    turn = _make_turn(agent_output="I did X")
    resp = _make_judge_response('{"score": 0.6, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=[turn])

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert "AGENT OUTPUT (UNTRUSTED DATA" in user_msg
    assert "I did X" in user_msg


def test_judge_include_tool_calls_true(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_tool_calls=True)
    cmd = _make_cmd(tool_name="Bash", params={"command": "ls"})
    turn = _make_turn(commands=[cmd])
    resp = _make_judge_response('{"score": 0.55, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=[turn])

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert "AGENT TOOL CALLS" in user_msg
    assert "Bash" in user_msg
    assert "ls" in user_msg


def test_judge_include_dialog_renders_all_turns(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_dialog=True)
    turns = [
        TurnRecord(iteration=1, user_input="add a button", agent_output="added"),
        TurnRecord(iteration=2, user_input="make it red", agent_output="done"),
    ]
    resp = _make_judge_response('{"score": 0.8, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=turns)

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert "DIALOG" in user_msg
    assert "simulated user" in user_msg  # rubric guard against false hallucination calls
    assert "[Turn 1] USER:\nadd a button" in user_msg
    assert "[Turn 1] AGENT:\nadded" in user_msg
    assert "[Turn 2] USER:\nmake it red" in user_msg
    assert "[Turn 2] AGENT:\ndone" in user_msg


def test_judge_include_dialog_omitted_when_false(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    turn = TurnRecord(iteration=1, user_input="add a button", agent_output="added")
    resp = _make_judge_response('{"score": 0.8, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=[turn])

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert "DIALOG" not in user_msg
    assert "add a button" not in user_msg


def test_judge_include_dialog_no_turns_records_degraded_note(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_dialog=True)
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=None)

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert "DIALOG" not in user_msg
    assert "include_dialog" in (result.details or "")


def test_judge_trajectory_toggles_without_turn_records(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(
        description="x",
        prompt="grade",
        include_agent_output=True,
        include_tool_calls=True,
    )
    resp = _make_judge_response('{"score": 0.3, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=None)

    assert result.error is None
    details = result.details or ""
    assert "notes:" in details
    assert "include_agent_output" in details
    assert "include_tool_calls" in details


def test_judge_trajectory_toggles_with_empty_commands(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_tool_calls=True)
    turn = _make_turn(commands=[])
    resp = _make_judge_response('{"score": 0.4, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=[turn])

    user_msg = m_anthropic.call_args.kwargs["user"]
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
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    user_msg = m_anthropic.call_args.kwargs["user"]
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
    resp = _make_judge_response('{"score": 0.2, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=[turn])

    user_msg = m_anthropic.call_args.kwargs["user"]
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
    resp = _make_judge_response('{"score": 0.5, "rationale": "half"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        checker = SuccessChecker(sandbox, init_registry=False, route=DirectRoute())
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
    resp = _make_judge_response('{"score": 0.9, "rationale": "great"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(
            criterion, reference_code=sentinel
        )

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
    resp = _make_judge_response(f'{{"score": "{sentinel}", "rationale": "ok"}}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(
            criterion, reference_code=sentinel
        )

    for field_value in (result.details, result.error):
        assert field_value is None or sentinel not in field_value


def test_judge_reference_not_leaked_on_parse_failure(sandbox: Sandbox) -> None:
    """If the model echoes the reference in an unparseable response, details must not leak it."""
    sentinel = "REFERENCE_CANARY_ECHOED_789"
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True)
    # Unparseable response that mentions the reference — simulates a misbehaving model.
    resp = _make_judge_response(f"Sorry, here is what you gave me: {sentinel}. no json")
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(
            criterion, reference_code=sentinel
        )

    assert result.score == 0.0
    for field_value in (result.description, result.details, result.error):
        assert field_value is None or sentinel not in field_value


# --- route dispatch ---


def test_judge_no_route_is_unconfigured(sandbox: Sandbox) -> None:
    """No route -> UNCONFIGURED arm; neither invoker is called."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with (
        patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge") as m_bedrock,
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge") as m_anthropic,
    ):
        result = SuccessChecker(sandbox, init_registry=False).check(criterion)
    assert result.score == 0.0
    assert "unconfigured" in (result.details or "")
    assert m_bedrock.call_count == 0
    assert m_anthropic.call_count == 0


def _tool_use_block(score: float, rationale: str = "ok") -> dict:
    return {
        "content": [{"type": "tool_use", "name": "submit_verdict", "input": {"score": score, "rationale": rationale}}]
    }


def test_judge_bedrock_route_uses_bedrock_invoker(sandbox: Sandbox) -> None:
    from coder_eval.models.routing import BedrockRoute

    route = BedrockRoute(bearer_token="t", region="eu-north-1")
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with (
        patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge", return_value=_tool_use_block(0.7)) as m_bedrock,
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge") as m_anthropic,
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert result.score == 0.7
    m_bedrock.assert_called_once()
    kwargs = m_bedrock.call_args.kwargs
    assert kwargs["route"] is route
    assert kwargs["model"] == criterion.model
    assert kwargs["temperature"] == criterion.temperature
    assert kwargs["max_tokens"] == criterion.max_tokens
    assert kwargs["tool_spec"]["name"] == "submit_verdict"
    assert m_anthropic.call_count == 0


def test_judge_direct_route_uses_anthropic_invoker(sandbox: Sandbox) -> None:
    from coder_eval.models.routing import DirectRoute

    route = DirectRoute()
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with (
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=_tool_use_block(0.5)) as m_anthropic,
        patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge") as m_bedrock,
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert result.score == 0.5
    m_anthropic.assert_called_once()
    assert m_bedrock.call_count == 0


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


def test_judge_direct_route_with_no_transport_fails_with_clear_error(sandbox: Sandbox) -> None:
    """DirectRoute(judge_transport=None) → typed JudgeCriterionResult, no LLM calls.

    Covers the 'Direct backend with no usable judge transport' configuration.
    The early return must land as ``JudgeCriterionResult`` (not a base
    ``CriterionResult`` from ``@handle_criterion_errors``) so renderers /
    aggregators that switch on ``isinstance(cr, JudgeCriterionResult)`` see the
    uniform shape.
    """
    from coder_eval.models import JudgeCriterionResult
    from coder_eval.models.routing import DirectRoute

    route = DirectRoute(judge_transport=None)
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with (
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge") as m_anthropic,
        patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge") as m_bedrock,
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert isinstance(result, JudgeCriterionResult)
    assert result.score == 0.0
    assert "unconfigured" in (result.details or "")
    assert result.error is not None
    # The error should point users at the supported backends.
    assert "ANTHROPIC_API_KEY" in result.error
    assert "Bedrock" in result.error
    assert result.findings == []
    assert result.transcript is None
    assert m_anthropic.call_count == 0
    assert m_bedrock.call_count == 0


def test_judge_direct_route_no_transport_does_not_log_decorator_warning(
    sandbox: Sandbox, caplog: pytest.LogCaptureFixture
) -> None:
    """The early return path bypasses ``@handle_criterion_errors``.

    Regression guard: the decorator's ``except Exception`` block logs at ERROR
    when it catches an exception. The early-return arm must not trip that — it
    returns the result directly, so the only log entry should be the criterion's
    own ``logger.error`` describing the missing transport.
    """
    import logging

    from coder_eval.models.routing import DirectRoute

    route = DirectRoute(judge_transport=None)
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    with caplog.at_level(logging.ERROR, logger="coder_eval.criteria.base"):
        SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    # The decorator logs from coder_eval.criteria.base; the criterion logs from
    # coder_eval.criteria.llm_judge. The decorator path must NOT have fired.
    decorator_records = [r for r in caplog.records if r.name == "coder_eval.criteria.base"]
    assert decorator_records == [], f"unexpected decorator log records: {decorator_records}"


def test_judge_bedrock_route_threads_model_unchanged(sandbox: Sandbox) -> None:
    """Translation happens INSIDE the helper, not at the dispatch site."""
    from coder_eval.models.routing import BedrockRoute

    route = BedrockRoute(bearer_token="t", region="eu-north-1")
    criterion = LLMJudgeCriterion(description="x", prompt="grade", model="anthropic.claude-opus-4-6-v1")
    with patch(
        "coder_eval.criteria.llm_judge.invoke_bedrock_judge",
        return_value=_tool_use_block(0.9),
    ) as m_bedrock:
        SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert m_bedrock.call_args.kwargs["model"] == "anthropic.claude-opus-4-6-v1"


# --- verbose verdict + transcript persistence ---


def test_judge_empty_rationale_returns_judge_result_with_error(sandbox: Sandbox) -> None:
    """A model that emits whitespace-only rationale → JudgeCriterionResult(error=...).

    Locks in the ValidationError → JudgeCriterionResult(score=0.0) chain —
    the pre-fix code would silently emit ``rationale: `` (blank) in details.
    """
    from coder_eval.models import JudgeCriterionResult

    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": 0.5, "rationale": "  "}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    assert isinstance(result, JudgeCriterionResult)
    assert result.score == 0.0
    assert result.error is not None
    assert "empty" in result.error.lower()


def test_judge_persists_findings(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    verdict = (
        '{"score": 0.7, "rationale": "ok", '
        '"findings": ["main.py:5 missing return — issue", "no docstrings — minor deviation"]}'
    )
    resp = _make_judge_response(verdict)
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    assert result.score == 0.7
    assert getattr(result, "findings", []) == [
        "main.py:5 missing return — issue",
        "no docstrings — minor deviation",
    ]


def test_judge_prompt_requires_findings(sandbox: Sandbox) -> None:
    """The system + user prompts must instruct the model to emit findings."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    kwargs = m_anthropic.call_args.kwargs
    system_msg = kwargs["system"]
    user_msg = kwargs["user"]
    assert "findings" in system_msg.lower()
    assert "findings" in user_msg.lower()
    # Analysis was dropped — must NOT appear in the prompts.
    assert "analysis" not in system_msg.lower()
    assert "analysis" not in user_msg.lower()


def test_judge_transcript_captures_raw_verdict_by_default(sandbox: Sandbox) -> None:
    """Tool-channel transcript stores the JSON-dumped verdict (no internal spaces) as raw_verdict."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok", "findings": ["a"]}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert '"score":0.5' in transcript.raw_verdict
    assert '"rationale":"ok"' in transcript.raw_verdict
    assert '"findings":["a"]' in transcript.raw_verdict
    assert transcript.tool_calls == []  # llm_judge transcript carries no sub-agent tool calls


def test_judge_capture_transcript_false_drops_transcript(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", capture_transcript=False)
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    assert getattr(result, "transcript", None) is None
    assert result.score == 0.5  # capture flag only gates the transcript, not the verdict


def test_judge_transcript_truncation_marks_truncated(sandbox: Sandbox) -> None:
    """raw_verdict longer than max_transcript_chars gets clipped and the flag flips."""
    big_finding = "x" * 5000
    raw = f'{{"score": 0.5, "rationale": "ok", "findings": ["{big_finding}"]}}'
    criterion = LLMJudgeCriterion(description="x", prompt="grade", max_transcript_chars=200)
    resp = _make_judge_response(raw)
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert transcript.truncated is True
    assert len(transcript.raw_verdict) < len(raw)


def test_judge_scrubs_reference_from_findings(sandbox: Sandbox) -> None:
    """If the model echoes the reference in findings/raw_verdict, those must be scrubbed."""
    sentinel = "REF_LEAK_VIA_FINDINGS"
    raw = f'{{"score": 0.5, "rationale": "ok", "findings": ["echoed {sentinel} in main.py"]}}'
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True)
    resp = _make_judge_response(raw)
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(
            criterion, reference_code=sentinel
        )

    for finding in getattr(result, "findings", []) or []:
        assert sentinel not in finding
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert sentinel not in transcript.raw_verdict


def test_judge_legacy_two_field_verdict_still_parses(sandbox: Sandbox) -> None:
    """A model that ignores the findings instructions and emits the old two-field
    form must still parse — findings defaults empty, score/rationale stand."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": 0.42, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    assert result.score == 0.42
    assert result.error is None
    assert getattr(result, "findings", []) == []


def test_judge_no_tool_call_still_carries_transcript(sandbox: Sandbox) -> None:
    """Even when the model emits no submit_verdict call, the transcript records the diagnostic."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response("totally not json")
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    assert result.score == 0.0
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert "Judge did not call submit_verdict" in transcript.raw_verdict


# --- enabled flag (master skip) ---


def test_judge_enabled_false_short_circuits(sandbox: Sandbox) -> None:
    """enabled=False: no LLM call, returns a skipped result with score=1.0."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", enabled=False)
    resp = _make_judge_response('{"score": 0.5, "rationale": "should not be called"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    assert result.score == 1.0
    assert "skipped" in (result.details or "").lower()
    assert "enabled=false" in (result.details or "").lower()
    m_anthropic.assert_not_called()


def test_judge_enabled_default_true(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    assert criterion.enabled is True


# --- judge prompt + system prompt capture ---


def test_judge_transcript_captures_prompts(sandbox: Sandbox) -> None:
    """The rendered user message + system prompt land on the transcript so reviewers
    can see exactly what the judge was told."""
    criterion = LLMJudgeCriterion(description="x", prompt="rubric body XYZ")
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert "rubric body XYZ" in transcript.judge_prompt
    assert "GRADING PROMPT:" in transcript.judge_prompt
    assert "strict code reviewer" in transcript.judge_system_prompt.lower()


def test_judge_prompt_capture_scrubs_reference(sandbox: Sandbox) -> None:
    """The reference solution sits inside the judge prompt — must be scrubbed before persistence."""
    sentinel = "REF_LEAK_VIA_PROMPT_111"
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True)
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(
            criterion, reference_code=sentinel
        )

    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    # Confirm the reference reached the judge but is scrubbed from persisted prompt.
    user_msg = m_anthropic.call_args.kwargs["user"]
    assert sentinel in user_msg
    assert sentinel not in transcript.judge_prompt
    assert sentinel not in transcript.judge_system_prompt


# --- legacy in-table tests continue ---


def test_judge_agent_output_empty_does_not_emit_block(sandbox: Sandbox) -> None:
    """include_agent_output=True with empty agent_output must not produce an empty header block,
    but MUST record a degradation note so the caller can spot the missing context."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_agent_output=True)
    turn = _make_turn(agent_output="")
    resp = _make_judge_response('{"score": 0.5, "rationale": "ok"}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion, turn_records=[turn])

    user_msg = m_anthropic.call_args.kwargs["user"]
    assert "AGENT OUTPUT" not in user_msg
    details = result.details or ""
    assert "notes:" in details
    assert "latest agent output is empty" in details


# --- submit_verdict tool channel ---


def test_llm_judge_tool_channel_happy_path(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": 0.6, "rationale": "ok", "findings": ["a"]}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.6
    assert result.error is None


def test_llm_judge_tool_channel_did_not_call(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    # Model returned text only, no tool_use block.
    resp = {"content": [{"type": "text", "text": "no verdict here"}]}
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.0
    assert result.error == "Judge did not call submit_verdict"


def test_llm_judge_tool_channel_invalid_args(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = {"content": [{"type": "tool_use", "name": "submit_verdict", "input": {"score": "not numeric"}}]}
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "score field is not a number" in result.error


def test_llm_judge_tool_channel_last_call_wins(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = {
        "content": [
            {"type": "tool_use", "name": "submit_verdict", "input": {"score": 0.2, "rationale": "first"}},
            {"type": "tool_use", "name": "submit_verdict", "input": {"score": 0.9, "rationale": "second"}},
        ]
    }
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.9


def test_llm_judge_tool_channel_transcript_carries_structured_verdict(sandbox: Sandbox) -> None:
    """Tool channel transcript stores the JSON-dumped verdict, not the agent's raw text."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _make_judge_response('{"score": 0.42, "rationale": "headline", "findings": ["f1"]}')
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert '"score":0.42' in transcript.raw_verdict


def test_llm_judge_transport_unconfigured(sandbox: Sandbox) -> None:
    from coder_eval.models.routing import DirectRoute

    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute(judge_transport=None)).check(criterion)
    assert result.score == 0.0
    assert "transport unconfigured" in (result.details or "")


def test_llm_judge_tool_channel_bedrock(sandbox: Sandbox) -> None:
    """Bedrock route uses the Anthropic-native tool format and feeds the dict extractor."""
    from coder_eval.models.routing import BedrockRoute

    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    bedrock_response = {
        "content": [
            {"type": "tool_use", "name": "submit_verdict", "input": {"score": 0.81, "rationale": "ok"}},
        ]
    }
    with patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge", return_value=bedrock_response) as mock_invoke:
        result = SuccessChecker(
            sandbox, init_registry=False, route=BedrockRoute(bearer_token="t", region="us-east-1")
        ).check(criterion)
    assert result.score == 0.81
    # Confirm we passed the Anthropic-native tool spec.
    kwargs = mock_invoke.call_args.kwargs
    assert kwargs["tool_spec"]["name"] == "submit_verdict"
    assert "input_schema" in kwargs["tool_spec"]


def test_llm_judge_tool_channel_anthropic_direct(sandbox: Sandbox) -> None:
    """DirectRoute (anthropic transport) uses invoke_anthropic_judge returning a dict."""
    from coder_eval.models.routing import DirectRoute

    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    anthropic_dict = {
        "content": [
            {"type": "tool_use", "name": "submit_verdict", "input": {"score": 0.33, "rationale": "ok"}},
        ]
    }
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=anthropic_dict) as mock_invoke:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute(judge_transport="anthropic")).check(
            criterion
        )
    assert result.score == 0.33
    assert mock_invoke.call_args.kwargs["tool_spec"]["name"] == "submit_verdict"


def test_llm_judge_rejects_legacy_verdict_channel_field() -> None:
    """YAML still carrying ``verdict_channel`` raises a migration-friendly error."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="verdict_channel field was removed"):
        LLMJudgeCriterion.model_validate({"description": "x", "prompt": "grade", "verdict_channel": "text"})


# --- Phase 3: route-uniform judge token_usage capture ---


def _anthropic_response(*, score: float, usage: dict | None) -> dict:
    resp: dict = {
        "content": [
            {"type": "tool_use", "name": "submit_verdict", "input": {"score": score, "rationale": "ok"}},
        ]
    }
    if usage is not None:
        resp["usage"] = usage
    return resp


def test_judge_usage_direct_anthropic_from_response(sandbox: Sandbox) -> None:
    """DirectRoute (anthropic): token_usage comes from the response usage block."""
    from coder_eval.models import JudgeCriterionResult
    from coder_eval.models.routing import DirectRoute

    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _anthropic_response(score=0.7, usage={"input_tokens": 1234, "output_tokens": 56})
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute(judge_transport="anthropic")).check(
            criterion
        )
    assert isinstance(result, JudgeCriterionResult)
    assert result.token_usage is not None
    assert result.token_usage.input_tokens == 1234
    assert result.token_usage.output_tokens == 56


def test_judge_usage_bedrock_from_response(sandbox: Sandbox) -> None:
    """BedrockRoute: token_usage comes from the /invoke JSON usage block."""
    from coder_eval.models import JudgeCriterionResult
    from coder_eval.models.routing import BedrockRoute

    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _anthropic_response(
        score=0.6, usage={"input_tokens": 900, "output_tokens": 40, "cache_read_input_tokens": 100}
    )
    with patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge", return_value=resp):
        result = SuccessChecker(
            sandbox, init_registry=False, route=BedrockRoute(bearer_token="t", region="us-east-1")
        ).check(criterion)
    assert isinstance(result, JudgeCriterionResult)
    assert result.token_usage is not None
    assert result.token_usage.uncached_input_tokens == 900
    assert result.token_usage.cache_read_input_tokens == 100


def test_judge_usage_none_when_response_has_no_usage(sandbox: Sandbox) -> None:
    """No usage in the response and no proxy → token_usage stays None (distinct
    from a zero TokenUsage)."""
    from coder_eval.models import JudgeCriterionResult
    from coder_eval.models.routing import DirectRoute

    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    resp = _anthropic_response(score=0.7, usage=None)
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute(judge_transport="anthropic")).check(
            criterion
        )
    assert isinstance(result, JudgeCriterionResult)
    assert result.token_usage is None


# --- Phase 3: usage extractor unit tests ---


def test_token_usage_from_anthropic_dict() -> None:
    from coder_eval.evaluation.judge_usage import token_usage_from_anthropic_dict

    tu = token_usage_from_anthropic_dict(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 4,
            }
        }
    )
    assert tu is not None
    assert (tu.uncached_input_tokens, tu.output_tokens, tu.cache_creation_input_tokens, tu.cache_read_input_tokens) == (
        10,
        2,
        3,
        4,
    )
    # Missing / empty usage → None (not a zero TokenUsage).
    assert token_usage_from_anthropic_dict({}) is None
    assert token_usage_from_anthropic_dict({"usage": {}}) is None
    assert token_usage_from_anthropic_dict({"usage": {"input_tokens": 0, "output_tokens": 0}}) is None


def test_usage_extractors_degrade_non_numeric_values_without_raising() -> None:
    """A malformed (non-numeric) usage value must NOT raise — the extractor's
    contract is to return usage or None, never to fail the judge criterion.
    The bad field degrades to 0; valid sibling fields survive."""
    from coder_eval.evaluation.judge_usage import token_usage_from_anthropic_dict

    # One bad field, one good: bad → 0, good survives → non-empty result.
    tu = token_usage_from_anthropic_dict({"usage": {"input_tokens": "NaN", "output_tokens": 5}})
    assert tu is not None
    assert tu.input_tokens == 0
    assert tu.output_tokens == 5

    # A nested object is also non-coercible → degrades to 0 (here all-zero → None).
    assert token_usage_from_anthropic_dict({"usage": {"input_tokens": {}, "output_tokens": "x"}}) is None


# --- multi-sample judging (samples > 1) ---


def _sample_resp(
    score: float, rationale: str = "ok", findings: list[str] | None = None, usage: dict | None = None
) -> dict:
    """An Anthropic-shaped submit_verdict response for one judge sample."""
    verdict_input: dict[str, Any] = {"score": score, "rationale": rationale}
    if findings is not None:
        verdict_input["findings"] = findings
    resp: dict[str, Any] = {"content": [{"type": "tool_use", "name": "submit_verdict", "input": verdict_input}]}
    if usage is not None:
        resp["usage"] = usage
    return resp


def _no_verdict_resp(usage: dict | None = None) -> dict:
    """A text-only response — the model returned prose instead of the tool call."""
    resp: dict[str, Any] = {"content": [{"type": "text", "text": "no verdict here"}]}
    if usage is not None:
        resp["usage"] = usage
    return resp


class _SerialExecutor:
    """Inline-executing stand-in for ``ThreadPoolExecutor``, with real Futures.

    Concurrent dispatch makes a mock ``side_effect`` list's response-to-sample
    mapping scheduler-dependent (whichever worker thread calls first consumes
    the next list item). Ordering-pinning tests — sample_scores order,
    earliest-sample tie-breaks, the first-sample diagnostic — patch this
    executor so response N deterministically lands on sample N. Everything the
    production code touches (context manager, ``submit`` returning a ``Future``
    that carries a result or an exception) behaves identically.
    """

    def __init__(self, max_workers: int | None = None) -> None:
        self.max_workers = max_workers

    def __enter__(self) -> _SerialExecutor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


_SERIAL_EXECUTOR_PATCH = "coder_eval.criteria.llm_judge.ThreadPoolExecutor"


def test_judge_samples_defaults_to_single_invocation(sandbox: Sandbox) -> None:
    """Default samples=1: exactly one judge call, no aggregation artifacts in details."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade")
    assert criterion.samples == 1
    resp = _sample_resp(0.85, rationale="mostly correct")
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", return_value=resp) as m_anthropic:
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert m_anthropic.call_count == 1
    assert result.score == 0.85
    assert result.error is None
    assert "sample_scores" not in (result.details or "")


def test_judge_samples_bounds_validation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LLMJudgeCriterion(description="x", prompt="grade", samples=0)
    with pytest.raises(ValidationError):
        LLMJudgeCriterion(description="x", prompt="grade", samples=10)


def test_judge_multi_sample_scores_median_odd(sandbox: Sandbox) -> None:
    """samples=3: three invocations, median score wins, per-sample scores in details."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    responses = [_sample_resp(0.2, "low"), _sample_resp(0.9, "high"), _sample_resp(0.6, "mid")]
    with (
        patch(_SERIAL_EXECUTOR_PATCH, _SerialExecutor),
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses) as m_anthropic,
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert m_anthropic.call_count == 3
    assert result.score == 0.6
    assert result.error is None
    details = result.details or ""
    assert "rationale: mid" in details  # representative = the median sample
    assert "sample_scores: [0.200, 0.900, 0.600]" in details
    # All samples succeeded — the degraded note must not appear.
    assert "judge samples produced no verdict" not in details


def test_judge_multi_sample_reuses_identical_prompt(sandbox: Sandbox) -> None:
    """Every sample grades the same rendered prompt with the same invocation params."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    responses = [_sample_resp(0.5), _sample_resp(0.5), _sample_resp(0.5)]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses) as m_anthropic:
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    first_kwargs = m_anthropic.call_args_list[0].kwargs
    for call in m_anthropic.call_args_list[1:]:
        assert call.kwargs == first_kwargs


def test_judge_multi_sample_even_count_averages_middle(sandbox: Sandbox) -> None:
    """Even N: median averages the two middle scores; representative is the closest sample."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=4)
    responses = [_sample_resp(0.2, "r1"), _sample_resp(0.4, "r2"), _sample_resp(0.6, "r3"), _sample_resp(0.8, "r4")]
    with (
        patch(_SERIAL_EXECUTOR_PATCH, _SerialExecutor),
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses),
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == pytest.approx(0.5)
    # 0.4 and 0.6 are equidistant from the 0.5 median — the earlier sample wins.
    assert "rationale: r2" in (result.details or "")


def test_judge_multi_sample_tie_break_is_first_sample(sandbox: Sandbox) -> None:
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=2)
    responses = [_sample_resp(0.4, "first"), _sample_resp(0.6, "second")]
    with (
        patch(_SERIAL_EXECUTOR_PATCH, _SerialExecutor),
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses),
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == pytest.approx(0.5)
    assert "rationale: first" in (result.details or "")


def test_judge_multi_sample_partial_no_verdict_degrades(sandbox: Sandbox) -> None:
    """One sample returning no verdict must not zero the criterion — median of the valid ones."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    responses = [_sample_resp(0.8, "a"), _no_verdict_resp(), _sample_resp(0.6, "b")]
    with (
        patch(_SERIAL_EXECUTOR_PATCH, _SerialExecutor),
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses),
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == pytest.approx(0.7)
    assert result.error is None
    details = result.details or ""
    assert "1/3 judge samples produced no verdict" in details
    assert "median over 2 valid samples" in details
    assert "sample_scores: [0.800, 0.600]" in details


def test_judge_multi_sample_two_samples_one_failure_degrades(sandbox: Sandbox) -> None:
    """N=2 (the minimal multi-sample count) with one failure: the single valid score stands."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=2)
    responses = [_no_verdict_resp(), _sample_resp(0.8, "only valid")]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.8
    assert result.error is None
    details = result.details or ""
    assert "rationale: only valid" in details
    assert "1/2 judge samples produced no verdict" in details
    assert "median over 1 valid samples" in details


def test_judge_multi_sample_transport_exception_degrades(sandbox: Sandbox, caplog: pytest.LogCaptureFixture) -> None:
    """One sample's transport failure must not void the others — but it must stay loud.

    The swallowed exception is logged at WARNING with exc_info and its type name
    lands in the degraded note, so a systematic first-party bug that trips on a
    fraction of responses can never ship as a silently-shifted green median.
    """
    import logging

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    responses = [_sample_resp(0.8), RuntimeError("gateway down"), _sample_resp(0.6)]
    with (
        caplog.at_level(logging.WARNING, logger="coder_eval.criteria.llm_judge"),
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses),
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == pytest.approx(0.7)
    assert result.error is None
    details = result.details or ""
    assert "1/3 judge samples produced no verdict" in details
    assert "RuntimeError: gateway down" in details  # the failure is NAMED, not just counted
    warning_records = [
        r for r in caplog.records if r.levelno == logging.WARNING and "llm_judge sample failed" in r.getMessage()
    ]
    assert len(warning_records) == 1
    assert warning_records[0].exc_info is not None  # traceback preserved for triage


def test_judge_multi_sample_infra_error_degrades_when_verdicts_exist(sandbox: Sandbox) -> None:
    """An infra failure on one sample degrades (never scores 0.0) when other samples produced verdicts."""
    from coder_eval.errors import JudgeInfrastructureError

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    responses = [JudgeInfrastructureError("api error"), _sample_resp(0.5), _sample_resp(0.9)]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == pytest.approx(0.7)
    assert result.error is None


def test_judge_multi_sample_all_no_verdict_keeps_failure_path(sandbox: Sandbox) -> None:
    """Every sample failing to call submit_verdict keeps the single-sample failure semantics."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    responses = [_no_verdict_resp(), _no_verdict_resp(), _no_verdict_resp()]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.0
    assert result.error == "Judge did not call submit_verdict"


def test_judge_multi_sample_all_fail_reports_first_sample_diagnostic(sandbox: Sandbox) -> None:
    """When every sample fails to produce a verdict, the FIRST sample's diagnostic is reported."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=2)
    bad_args = {"content": [{"type": "tool_use", "name": "submit_verdict", "input": {"score": "not numeric"}}]}
    responses = [bad_args, _no_verdict_resp()]
    with (
        patch(_SERIAL_EXECUTOR_PATCH, _SerialExecutor),
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses),
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "score field is not a number" in result.error  # sample #1's diagnostic, not sample #2's


def test_judge_multi_sample_all_infra_errors_escalate(sandbox: Sandbox) -> None:
    """All samples failing on infrastructure escalates — judge infra failure is not an agent failure."""
    from coder_eval.errors import JudgeInfrastructureError

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=2)
    responses = [JudgeInfrastructureError("api error"), JudgeInfrastructureError("api error")]
    with (
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses),
        pytest.raises(JudgeInfrastructureError),
    ):
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)


def test_judge_multi_sample_mixed_all_fail_prefers_infra_escalation(sandbox: Sandbox) -> None:
    """No verdict anywhere + an infra failure in the mix: escalate rather than score 0.0."""
    from coder_eval.errors import JudgeInfrastructureError

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=2)
    responses = [_no_verdict_resp(), JudgeInfrastructureError("api error")]
    with (
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses),
        pytest.raises(JudgeInfrastructureError),
    ):
        SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)


def test_judge_multi_sample_all_unexpected_errors_map_to_score_zero(sandbox: Sandbox) -> None:
    """Non-infra exceptions on every sample reach @handle_criterion_errors, as with samples=1."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=2)
    responses = [RuntimeError("gateway down"), RuntimeError("gateway down")]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.0
    assert result.error is not None
    assert "gateway down" in result.error


def test_judge_multi_sample_token_usage_summed(sandbox: Sandbox) -> None:
    """Usage sums across every sample that returned a response — including no-verdict samples."""
    from coder_eval.models import JudgeCriterionResult

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    usage = {"input_tokens": 100, "output_tokens": 10}
    responses = [_sample_resp(0.8, usage=usage), _no_verdict_resp(usage=usage), _sample_resp(0.6, usage=usage)]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert isinstance(result, JudgeCriterionResult)
    assert result.token_usage is not None
    assert result.token_usage.uncached_input_tokens == 300
    assert result.token_usage.output_tokens == 30


def test_judge_multi_sample_representative_supplies_findings_and_transcript(sandbox: Sandbox) -> None:
    """Findings and the persisted transcript come from the representative sample, not a blend."""
    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    responses = [
        _sample_resp(0.2, "low", findings=["low finding"]),
        _sample_resp(0.6, "mid", findings=["mid finding"]),
        _sample_resp(0.9, "high", findings=["high finding"]),
    ]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert result.score == 0.6
    assert getattr(result, "findings", []) == ["mid finding"]
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert '"score":0.6' in transcript.raw_verdict


def test_judge_multi_sample_scrubs_reference_everywhere(sandbox: Sandbox) -> None:
    """Reference scrubbing covers details, findings, and the transcript with samples > 1."""
    sentinel = "REF_LEAK_MULTI_SAMPLE_321"
    criterion = LLMJudgeCriterion(description="x", prompt="grade", include_reference=True, samples=3)
    responses = [
        _sample_resp(0.4, f"echoed {sentinel}"),
        _sample_resp(0.5, f"echoed {sentinel}", findings=[f"finding with {sentinel}"]),
        _sample_resp(0.6, f"echoed {sentinel}"),
    ]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(
            criterion, reference_code=sentinel
        )
    assert result.score == 0.5
    assert sentinel not in (result.details or "")
    for finding in getattr(result, "findings", []) or []:
        assert sentinel not in finding
    transcript = getattr(result, "transcript", None)
    assert transcript is not None
    assert sentinel not in transcript.raw_verdict


def test_judge_multi_sample_bedrock_route(sandbox: Sandbox) -> None:
    """samples>1 on the Bedrock arm — the nightly's route, and the invoker whose
    every failure mode is a ``JudgeInfrastructureError`` (the escalate-vs-degrade branch)."""
    from coder_eval.errors import JudgeInfrastructureError
    from coder_eval.models.routing import BedrockRoute

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    responses = [_sample_resp(0.2, "low"), JudgeInfrastructureError("bedrock api down"), _sample_resp(0.6, "mid")]
    route = BedrockRoute(bearer_token="t", region="us-east-1")
    with (
        patch("coder_eval.criteria.llm_judge.invoke_bedrock_judge", side_effect=responses) as m_bedrock,
        patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge") as m_anthropic,
    ):
        result = SuccessChecker(sandbox, init_registry=False, route=route).check(criterion)
    assert m_bedrock.call_count == 3
    assert m_anthropic.call_count == 0
    # One infra failure with valid verdicts remaining degrades — never escalates.
    assert result.score == pytest.approx(0.4)
    assert result.error is None
    details = result.details or ""
    assert "1/3 judge samples produced no verdict" in details
    assert "JudgeInfrastructureError: bedrock api down" in details
    assert "sample_scores" in details


def test_judge_multi_sample_token_usage_sums_cache_buckets(sandbox: Sandbox) -> None:
    """Cache-token buckets sum across samples — N identical prompts are exactly
    what populates cache_read on every sample after the first."""
    from coder_eval.models import JudgeCriterionResult

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=3)
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_creation_input_tokens": 7,
        "cache_read_input_tokens": 900,
    }
    responses = [_sample_resp(0.8, usage=usage), _sample_resp(0.5, usage=usage), _sample_resp(0.6, usage=usage)]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert isinstance(result, JudgeCriterionResult)
    assert result.token_usage is not None
    assert result.token_usage.uncached_input_tokens == 300
    assert result.token_usage.output_tokens == 30
    assert result.token_usage.cache_creation_input_tokens == 21
    assert result.token_usage.cache_read_input_tokens == 2700


def test_judge_multi_sample_token_usage_none_when_unreported(sandbox: Sandbox) -> None:
    """No sample reported usage: token_usage stays None — 'unknown' is not 'zero'."""
    from coder_eval.models import JudgeCriterionResult

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=2)
    responses = [_sample_resp(0.8), _sample_resp(0.6)]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert isinstance(result, JudgeCriterionResult)
    assert result.score == pytest.approx(0.7)
    assert result.token_usage is None


def test_judge_multi_sample_all_fail_carries_usage_and_transcript(sandbox: Sandbox) -> None:
    """The all-parse-fail result still audits: summed usage + the first sample's transcript."""
    from coder_eval.models import JudgeCriterionResult

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=2)
    usage = {"input_tokens": 40, "output_tokens": 4}
    responses = [_no_verdict_resp(usage=usage), _no_verdict_resp(usage=usage)]
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=responses):
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
    assert isinstance(result, JudgeCriterionResult)
    assert result.score == 0.0
    assert result.error == "Judge did not call submit_verdict"
    # Cost was real for both samples even though neither produced a verdict.
    assert result.token_usage is not None
    assert result.token_usage.uncached_input_tokens == 80
    assert result.token_usage.output_tokens == 8
    # The transcript carries the first sample's fallback marker.
    assert result.transcript is not None
    assert "(no verdict" in result.transcript.raw_verdict


def test_judge_multi_sample_runs_concurrently(sandbox: Sandbox) -> None:
    """samples=N wall-clock stays ~ one sample, not N — the dispatch is concurrent.

    A sleeping fake pins the concurrency property itself: serial dispatch would
    take >= samples * delay, so the assertion fails if the executor ever goes
    back to a sequential loop.
    """
    import time

    delay = 0.25

    def _slow_judge(**kwargs: Any) -> dict:
        time.sleep(delay)
        return _sample_resp(0.5)

    criterion = LLMJudgeCriterion(description="x", prompt="grade", samples=4)
    with patch("coder_eval.criteria.llm_judge.invoke_anthropic_judge", side_effect=_slow_judge):
        start = time.monotonic()
        result = SuccessChecker(sandbox, init_registry=False, route=DirectRoute()).check(criterion)
        elapsed = time.monotonic() - start
    assert result.score == 0.5
    # Serial would be >= 4 * delay = 1.0s; concurrent is ~delay. The 3x bound
    # leaves generous headroom for slow CI without admitting serial dispatch.
    assert elapsed < 3 * delay
