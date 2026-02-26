"""Tests for LLM reviewer handling of malformed JSON responses.

Tests ensure evaluator gracefully handles non-JSON or invalid JSON from LLM Gateway.
"""

import json
from unittest.mock import MagicMock

import pytest

from coder_eval.evaluation.reviewer import LLMReviewer
from coder_eval.models import LLMReviewerConfig


@pytest.fixture
def reviewer_config():
    """Standard LLM reviewer configuration for tests."""
    return LLMReviewerConfig(
        enabled=True,
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
    )


@pytest.fixture
def reviewer(reviewer_config):
    """Initialized LLM reviewer with mocked LLM Gateway client."""
    reviewer = LLMReviewer(reviewer_config)
    # Mock the LLM client (set private attr to bypass lazy property)
    reviewer._llm = MagicMock()
    return reviewer


def create_mock_response(content: str):
    """Create mock LLM response with given content."""
    response = MagicMock()
    response.content = content
    return response


def test_evaluator_handles_no_json_in_response(reviewer, caplog):
    """Test that pure text response without JSON returns None gracefully.

    Hypothesis: LLM may return plain text instead of JSON.
    Expected: Parse failure logged, None returned, no crash.

    Context: Lines 339-340 in evaluator.py check for JSON boundaries.
    """
    # Mock LLM returning plain text, no JSON
    reviewer.llm.invoke.return_value = create_mock_response("This is just plain text with no JSON object.")

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_invalid_json_syntax(reviewer, caplog):
    """Test that syntactically invalid JSON returns None gracefully.

    Hypothesis: LLM may return malformed JSON with syntax errors.
    Expected: JSON parse error caught, None returned, no crash.

    Context: Lines 343-350 handle json.loads() exceptions.
    """
    # Mock LLM returning invalid JSON (missing quote, trailing comma)
    invalid_json = '{"issues": "Test issue, "score": 0.5,}'
    reviewer.llm.invoke.return_value = create_mock_response(invalid_json)

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_json_with_missing_required_fields(reviewer, caplog):
    """Test that JSON missing required fields returns None gracefully.

    Hypothesis: LLM may return JSON that doesn't match LLMDecision schema.
    Expected: Pydantic validation error caught, None returned, no crash.

    Context: Line 345 creates LLMDecision, which validates required fields.
    """
    # Mock LLM returning valid JSON but missing required "issues" field
    incomplete_json = json.dumps({"score": 0.7, "should_continue": True})
    reviewer.llm.invoke.return_value = create_mock_response(incomplete_json)

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_json_with_wrong_field_types(reviewer, caplog):
    """Test that JSON with incorrect field types returns None gracefully.

    Hypothesis: LLM may return JSON with score as string instead of float.
    Expected: Type validation error caught, None returned, no crash.
    """
    # Mock LLM returning JSON with wrong types (score as string)
    wrong_types_json = json.dumps(
        {"issues": "Test issue", "score": "not_a_number", "next_steps": ["Fix X"], "should_continue": True}
    )
    reviewer.llm.invoke.return_value = create_mock_response(wrong_types_json)

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_json_wrapped_in_markdown(reviewer):
    """Test that JSON wrapped in markdown code blocks is extracted correctly.

    Hypothesis: LLM may return JSON inside markdown code blocks.
    Expected: JSON extracted and parsed successfully.

    Context: Lines 336-342 find JSON boundaries ignoring surrounding text.
    """
    # Mock LLM returning JSON wrapped in markdown
    markdown_wrapped = """Sure, here is the review:

```json
{
    "issues": "Missing error handling",
    "score": 0.6,
    "next_steps": ["Add try-except", "Validate input"],
    "should_continue": true
}
```

Hope this helps!"""
    reviewer.llm.invoke.return_value = create_mock_response(markdown_wrapped)

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    # Should succeed - JSON extraction handles surrounding text
    assert result is not None
    assert result.issues == "Missing error handling"
    assert result.score == 0.6
    assert result.next_steps == ["Add try-except", "Validate input"]
    assert result.should_continue is True


def test_evaluator_handles_empty_response(reviewer, caplog):
    """Test that empty LLM response returns None gracefully.

    Hypothesis: LLM may return empty string on timeout or error.
    Expected: Parse failure logged, None returned, no crash.
    """
    # Mock LLM returning empty response
    reviewer.llm.invoke.return_value = create_mock_response("")

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_llm_gateway_exception(reviewer, caplog):
    """Test that LLM Gateway exceptions are caught gracefully.

    Hypothesis: LLM Gateway may raise exceptions (timeout, auth error).
    Expected: Exception caught in review(), warning logged, None returned.

    Context: Lines 223-235 wrap llm.invoke() in try-except.
    """
    # Mock LLM Gateway raising exception
    reviewer.llm.invoke.side_effect = Exception("LLM Gateway timeout")

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("LLM review failed" in record.message for record in caplog.records)


def test_evaluator_returns_none_when_disabled(reviewer_config):
    """Test that disabled reviewer returns None without calling LLM.

    Hypothesis: When LLM reviewer is disabled, no LLM calls should be made.
    Expected: None returned immediately, no LLM invocation.

    Context: Lines 208-209 check config.enabled.
    """
    # Create reviewer with disabled config
    disabled_config = LLMReviewerConfig(enabled=False, model="test-model")
    reviewer = LLMReviewer(disabled_config)
    reviewer._llm = MagicMock()

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    reviewer.llm.invoke.assert_not_called()


def test_evaluator_succeeds_with_valid_json(reviewer):
    """Test happy path where LLM returns properly formatted JSON.

    Hypothesis: Well-formed JSON should parse successfully.
    Expected: LLMDecision object returned with correct values.
    """
    valid_json = json.dumps(
        {
            "issues": "Logic error in calculate() function",
            "score": 0.75,
            "next_steps": ["Fix line 42", "Add unit test"],
            "should_continue": True,
        }
    )
    reviewer.llm.invoke.return_value = create_mock_response(valid_json)

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is not None
    assert result.issues == "Logic error in calculate() function"
    assert result.score == 0.75
    assert result.next_steps == ["Fix line 42", "Add unit test"]
    assert result.should_continue is True
