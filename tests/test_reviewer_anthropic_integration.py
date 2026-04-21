"""Integration test for LLM reviewer with Anthropic API backend.

These tests hit the real Anthropic API with the key from ANTHROPIC_API_KEY.
They are skipped by default unless a real-looking key is present, and can
be selected explicitly with: pytest -m live
"""

import os

import pytest

from coder_eval.evaluation.reviewer import LLMReviewer
from coder_eval.models import LLMReviewerConfig


def _can_run_live() -> bool:
    """Return True only when a real-looking Anthropic key is available.

    CI sets a dummy placeholder (``sk-ant-test-dummy-...``) to exercise
    non-live code paths; real keys issued by Anthropic start with
    ``sk-ant-api``. Skipping on shape avoids hitting the API with a key
    that will 401.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return key.startswith("sk-ant-api")


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _can_run_live(),
        reason="ANTHROPIC_API_KEY not set or not a real Anthropic key (CI dummies skipped)",
    ),
]


class TestReviewerAnthropicBackend:
    """Tests that exercise the real Anthropic API."""

    def test_backend_selection_prefers_anthropic(self):
        """When ANTHROPIC_API_KEY is set, Anthropic backend is selected over LLMGW."""
        config = LLMReviewerConfig(enabled=True, model="anthropic.claude-sonnet-4-6")
        reviewer = LLMReviewer(config)
        assert reviewer._backend == "anthropic"

    def test_review_returns_valid_decision(self):
        """Call the real API and verify we get a valid LLMDecision back."""
        config = LLMReviewerConfig(
            enabled=True,
            model="anthropic.claude-sonnet-4-6",
            temperature=0.0,
            max_tokens=500,
        )
        reviewer = LLMReviewer(config)

        decision = reviewer.review(
            task_description="Create a Python script that prints 'hello world'",
            agent_output="The agent created hello.py with: print('hello world')",
            current_iteration=1,
            max_iterations=1,
        )

        assert decision is not None
        assert 0.0 <= decision.score <= 1.0
        assert isinstance(decision.issues, str)
        assert len(decision.issues) > 0
        assert isinstance(decision.should_continue, bool)
        assert isinstance(decision.next_steps, list)

    def test_review_with_poor_output_scores_low(self):
        """A clearly incomplete agent output should score below 0.8."""
        config = LLMReviewerConfig(
            enabled=True,
            model="anthropic.claude-sonnet-4-6",
            temperature=0.0,
            max_tokens=500,
        )
        reviewer = LLMReviewer(config)

        decision = reviewer.review(
            task_description="Create a REST API with 5 endpoints, authentication, and database integration",
            agent_output="I created a file called app.py but it's empty.",
            current_iteration=1,
            max_iterations=3,
        )

        assert decision is not None
        assert decision.score < 0.8
        assert decision.should_continue is True

    def test_review_with_good_output_scores_high(self):
        """A clearly complete agent output should score above 0.7."""
        config = LLMReviewerConfig(
            enabled=True,
            model="anthropic.claude-sonnet-4-6",
            temperature=0.0,
            max_tokens=500,
        )
        reviewer = LLMReviewer(config)

        decision = reviewer.review(
            task_description="Create a Python function that adds two numbers",
            agent_output=(
                "Created math_utils.py:\n"
                "```python\n"
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n"
                "```\n"
                "The function takes two integers and returns their sum. "
                "Includes type hints for clarity."
            ),
            current_iteration=1,
            max_iterations=1,
        )

        assert decision is not None
        assert decision.score > 0.7

    def test_gateway_model_name_mapping(self):
        """Gateway-style model names (anthropic.xxx) should be stripped to Anthropic IDs."""
        config = LLMReviewerConfig(
            enabled=True,
            model="anthropic.claude-sonnet-4-6",
            temperature=0.0,
            max_tokens=100,
        )
        reviewer = LLMReviewer(config)

        # Should work without error — the gateway prefix is stripped
        decision = reviewer.review(
            task_description="Test",
            agent_output="Done",
            current_iteration=1,
            max_iterations=1,
        )
        assert decision is not None
