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
    """Initialized LLM reviewer with its ``_llm`` replaced by a MagicMock callable.

    After Phase 1 ``LLMReviewer._llm`` is a ``Callable[[str], str]``. Tests set
    ``reviewer._llm.return_value`` / ``.side_effect`` directly.
    """
    reviewer = LLMReviewer(reviewer_config)
    reviewer._llm = MagicMock()
    return reviewer


def test_evaluator_handles_no_json_in_response(reviewer, caplog):
    """Test that pure text response without JSON returns None gracefully."""
    reviewer._llm.return_value = "This is just plain text with no JSON object."

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_invalid_json_syntax(reviewer, caplog):
    """Test that syntactically invalid JSON returns None gracefully."""
    reviewer._llm.return_value = '{"issues": "Test issue, "score": 0.5,}'

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_json_with_missing_required_fields(reviewer, caplog):
    """Test that JSON missing required fields returns None gracefully."""
    reviewer._llm.return_value = json.dumps({"score": 0.7, "should_continue": True})

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_json_with_wrong_field_types(reviewer, caplog):
    """Test that JSON with incorrect field types returns None gracefully."""
    reviewer._llm.return_value = json.dumps(
        {"issues": "Test issue", "score": "not_a_number", "next_steps": ["Fix X"], "should_continue": True}
    )

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_json_wrapped_in_markdown(reviewer):
    """Test that JSON wrapped in markdown code blocks is extracted correctly."""
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
    reviewer._llm.return_value = markdown_wrapped

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is not None
    assert result.issues == "Missing error handling"
    assert result.score == 0.6
    assert result.next_steps == ["Add try-except", "Validate input"]
    assert result.should_continue is True


def test_evaluator_handles_empty_response(reviewer, caplog):
    """Test that empty LLM response returns None gracefully."""
    reviewer._llm.return_value = ""

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("Failed to parse LLM response" in record.message for record in caplog.records)


def test_evaluator_handles_non_retryable_exception(reviewer, caplog):
    """Test that non-retryable errors (ValueError etc.) are caught gracefully.

    Non-retryable errors (parse/logic failures) return None so the orchestrator
    falls back to deterministic feedback. Retryable errors (timeouts, network)
    propagate so execute_with_retry can handle them.
    """
    reviewer._llm.side_effect = ValueError("bad config value")

    result = reviewer.review(
        task_description="Test task",
        agent_output="Agent output",
        current_iteration=1,
        max_iterations=3,
    )

    assert result is None
    assert any("LLM review failed" in record.message for record in caplog.records)


def test_evaluator_propagates_retryable_exception(reviewer):
    """Test that retryable errors (timeouts, network) propagate to the caller.

    This allows execute_with_retry to categorize and retry the operation.
    """
    reviewer._llm.side_effect = RuntimeError("LLM Gateway timeout")

    with pytest.raises(RuntimeError, match="LLM Gateway timeout"):
        reviewer.review(
            task_description="Test task",
            agent_output="Agent output",
            current_iteration=1,
            max_iterations=3,
        )


def test_evaluator_returns_none_when_disabled(reviewer_config):
    """Test that disabled reviewer returns None without calling LLM."""
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
    reviewer._llm.assert_not_called()


def test_evaluator_succeeds_with_valid_json(reviewer):
    """Test happy path where LLM returns properly formatted JSON."""
    reviewer._llm.return_value = json.dumps(
        {
            "issues": "Logic error in calculate() function",
            "score": 0.75,
            "next_steps": ["Fix line 42", "Add unit test"],
            "should_continue": True,
        }
    )

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
