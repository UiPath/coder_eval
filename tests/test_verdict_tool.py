"""Unit tests for the typed verdict tool channel."""

from __future__ import annotations

from typing import Any

import pytest

from coder_eval.evaluation.verdict_tool import (
    SUBMIT_VERDICT_ANTHROPIC_TOOL,
    SUBMIT_VERDICT_MCP_TOOL_NAME,
    SUBMIT_VERDICT_TOOL_NAME,
    VerdictCapture,
    _build_submit_verdict_tool,
    build_submit_verdict_mcp_server,
    extract_verdict_from_anthropic_response,
    extract_verdict_from_capture,
)
from coder_eval.models import JudgeVerdict


def _anthropic_response(payload: Any) -> dict[str, Any]:
    """Wrap a verdict-shaped ``input`` payload in an Anthropic tool_use response."""
    return {"content": [{"type": "tool_use", "name": SUBMIT_VERDICT_TOOL_NAME, "input": payload}]}


# --- VerdictCapture + SDK extractor ---


def test_capture_stores_validated_verdict() -> None:
    capture = VerdictCapture()
    capture.verdict = JudgeVerdict(score=0.7, rationale="ok")
    verdict, err = extract_verdict_from_capture(capture)
    assert err is None
    assert verdict is not None
    assert verdict.score == 0.7


def test_capture_overwrites_on_second_call() -> None:
    capture = VerdictCapture()
    capture.verdict = JudgeVerdict(score=0.3, rationale="first")
    capture.verdict = JudgeVerdict(score=0.9, rationale="second")
    verdict, err = extract_verdict_from_capture(capture)
    assert err is None
    assert verdict is not None
    assert verdict.score == 0.9


def test_capture_records_error_on_invalid_args() -> None:
    capture = VerdictCapture()
    capture.error = "score field is not a number: 'foo'"
    verdict, err = extract_verdict_from_capture(capture)
    assert verdict is None
    assert err == "score field is not a number: 'foo'"


def test_extract_sdk_returns_did_not_call_when_capture_empty() -> None:
    verdict, err = extract_verdict_from_capture(VerdictCapture())
    assert verdict is None
    assert err == "Judge did not call submit_verdict"


# --- Anthropic / Bedrock extractor ---


def test_extract_anthropic_picks_last_tool_use_block() -> None:
    response = {
        "content": [
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "name": SUBMIT_VERDICT_TOOL_NAME, "input": {"score": 0.2, "rationale": "first"}},
            {"type": "tool_use", "name": SUBMIT_VERDICT_TOOL_NAME, "input": {"score": 0.7, "rationale": "second"}},
        ]
    }
    verdict, err = extract_verdict_from_anthropic_response(response)
    assert err is None
    assert verdict is not None
    assert verdict.score == 0.7


def test_extract_anthropic_ignores_other_tool_names() -> None:
    response = {
        "content": [
            {"type": "tool_use", "name": "other_tool", "input": {"score": 0.99}},
            {"type": "tool_use", "name": SUBMIT_VERDICT_TOOL_NAME, "input": {"score": 0.5, "rationale": "real"}},
        ]
    }
    verdict, err = extract_verdict_from_anthropic_response(response)
    assert err is None
    assert verdict is not None
    assert verdict.score == 0.5


def test_extract_anthropic_returns_did_not_call_when_no_tool_use_block() -> None:
    response = {"content": [{"type": "text", "text": "no tool here"}]}
    verdict, err = extract_verdict_from_anthropic_response(response)
    assert verdict is None
    assert err == "Judge did not call submit_verdict"


def test_extract_anthropic_validates_input_against_judge_verdict_model() -> None:
    response = {
        "content": [
            {"type": "tool_use", "name": SUBMIT_VERDICT_TOOL_NAME, "input": {"score": float("nan"), "rationale": "x"}}
        ]
    }
    verdict, err = extract_verdict_from_anthropic_response(response)
    assert verdict is None
    assert err == "score field is not a finite number: nan"


def test_extract_anthropic_empty_content() -> None:
    verdict, err = extract_verdict_from_anthropic_response({})
    assert verdict is None
    assert err == "Judge did not call submit_verdict"


def test_extract_anthropic_distinguishes_invalid_input_from_did_not_call() -> None:
    """A tool_use block with non-dict input must NOT collapse into the "did not call" diagnostic."""
    response = {"content": [{"type": "tool_use", "name": SUBMIT_VERDICT_TOOL_NAME, "input": "not a dict"}]}
    verdict, err = extract_verdict_from_anthropic_response(response)
    assert verdict is None
    assert err == "submit_verdict input must be an object"


# --- _format_validation_error preserves legacy vocabulary ---


def _err_for(payload: dict[str, Any]) -> str:
    """Drive a verdict-shaped dict through the anthropic extractor and read the error string."""
    _, err = extract_verdict_from_anthropic_response(_anthropic_response(payload))
    assert err is not None
    return err


def test_format_validation_error_score_missing() -> None:
    assert _err_for({"rationale": "no score here"}) == "score field missing in judge verdict"


def test_format_validation_error_score_not_numeric() -> None:
    assert _err_for({"score": "foo", "rationale": "x"}) == "score field is not a number: 'foo'"


def test_format_validation_error_score_not_finite() -> None:
    assert _err_for({"score": float("nan"), "rationale": "x"}) == "score field is not a finite number: nan"


def test_format_validation_error_rationale_wrong_type() -> None:
    # JudgeVerdict's rationale validator runs first only if score validates — pass a valid score.
    assert _err_for({"score": 0.5, "rationale": 123}) == "rationale field must be a string, got int"


def test_format_validation_error_rationale_empty_after_whitespace() -> None:
    assert _err_for({"score": 0.5, "rationale": "   "}) == "rationale field is empty after whitespace collapse"


def test_format_validation_error_joins_multiple_field_failures() -> None:
    """Both score and rationale fail in a single payload — errors join with '; '."""
    err = _err_for({"score": "foo", "rationale": 123})
    assert "score field is not a number: 'foo'" in err
    assert "rationale field must be a string, got int" in err
    assert "; " in err


def test_score_clamps_to_unit_interval_through_tool() -> None:
    """An out-of-range score (1.5) clamps to 1.0 — JudgeVerdict's validator contract."""
    verdict, err = extract_verdict_from_anthropic_response(_anthropic_response({"score": 1.5, "rationale": "x"}))
    assert err is None
    assert verdict is not None
    assert verdict.score == 1.0


# --- Tool spec shape sanity ---


def test_submit_verdict_tool_schema_marks_findings_optional() -> None:
    schema = JudgeVerdict.model_json_schema()
    assert schema.get("required") == ["score"], "only `score` should be required at the schema layer"
    assert "findings" in schema["properties"]
    assert "rationale" in schema["properties"]


def test_anthropic_tool_spec_has_input_schema() -> None:
    """Anthropic-native tool format uses ``input_schema``, not OpenAI's ``parameters``."""
    assert SUBMIT_VERDICT_ANTHROPIC_TOOL["name"] == "submit_verdict"
    assert "input_schema" in SUBMIT_VERDICT_ANTHROPIC_TOOL
    assert "parameters" not in SUBMIT_VERDICT_ANTHROPIC_TOOL


def test_mcp_tool_name_matches_convention() -> None:
    """``mcp__<server>__<tool>`` — the SDK's documented MCP tool name format."""
    assert SUBMIT_VERDICT_MCP_TOOL_NAME == "mcp__coder_eval_judge__submit_verdict"


# --- Server factory smoke ---


def test_build_submit_verdict_mcp_server_returns_sdk_server_config() -> None:
    capture = VerdictCapture()
    name, server = build_submit_verdict_mcp_server(capture)
    assert name == "coder_eval_judge"
    assert isinstance(server, dict)
    assert server["type"] == "sdk"
    assert server["name"] == "coder_eval_judge"
    assert "instance" in server


@pytest.mark.asyncio
async def test_submit_verdict_tool_writes_to_capture_on_valid_args() -> None:
    capture = VerdictCapture()
    sdk_tool = _build_submit_verdict_tool(capture)
    result = await sdk_tool.handler({"score": 0.6, "rationale": "good", "findings": ["a"]})
    assert capture.called_count == 1
    assert capture.error is None
    assert capture.verdict is not None
    assert capture.verdict.score == 0.6
    assert capture.verdict.findings == ["a"]
    assert "Verdict received" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_submit_verdict_tool_records_error_on_invalid_args() -> None:
    capture = VerdictCapture()
    sdk_tool = _build_submit_verdict_tool(capture)
    result = await sdk_tool.handler({"score": "foo", "rationale": "x"})
    assert capture.called_count == 1
    assert capture.verdict is None
    assert capture.error == "score field is not a number: 'foo'"
    assert "Invalid" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_submit_verdict_tool_overwrites_on_second_call() -> None:
    """LAST-call discipline at the @tool layer: a second valid call wins over the first."""
    capture = VerdictCapture()
    sdk_tool = _build_submit_verdict_tool(capture)
    await sdk_tool.handler({"score": 0.2, "rationale": "first"})
    await sdk_tool.handler({"score": 0.9, "rationale": "second"})
    assert capture.called_count == 2
    assert capture.verdict is not None
    assert capture.verdict.score == 0.9


@pytest.mark.asyncio
async def test_submit_verdict_tool_invalid_after_valid_clears_verdict() -> None:
    """If the judge calls the tool a second time with invalid args, the prior verdict is cleared."""
    capture = VerdictCapture()
    sdk_tool = _build_submit_verdict_tool(capture)
    await sdk_tool.handler({"score": 0.5, "rationale": "ok"})
    assert capture.verdict is not None
    await sdk_tool.handler({"score": "nope"})
    assert capture.verdict is None
    assert capture.error == "score field is not a number: 'nope'"
