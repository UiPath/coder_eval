"""Unit tests for the Anthropic / LLMGW invoker builders in reviewer.py.

These tests verify that text is correctly extracted from the Anthropic
content-block union (TextBlock / ThinkingBlock / ToolUseBlock) and that the
LLMGW invoker gracefully coerces both str and list-shaped content.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coder_eval.evaluation.reviewer import (
    LLMReviewer,
    _make_anthropic_invoker,
    _make_llmgw_invoker,
)
from coder_eval.models import LLMReviewerConfig


def _make_config() -> LLMReviewerConfig:
    return LLMReviewerConfig(
        enabled=True,
        model="anthropic.claude-sonnet-4-6",
        temperature=0.0,
        max_tokens=100,
    )


def test_anthropic_invoker_joins_text_blocks_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only TextBlock content is concatenated; thinking and tool_use are ignored."""
    captured: dict[str, Any] = {}

    class FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="thinking", text="internal reasoning"),
                    SimpleNamespace(type="text", text="Hello, "),
                    SimpleNamespace(type="tool_use", name="search", input={"q": "x"}),
                    SimpleNamespace(type="text", text="world!"),
                ]
            )

    class FakeAnthropic:
        def __init__(self) -> None:
            self.messages = FakeMessages()

    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropic)

    invoker = _make_anthropic_invoker(_make_config())
    result = invoker("what's up")

    assert result == "Hello, world!"
    # Model prefix "anthropic." should be stripped
    assert captured["model"] == "claude-sonnet-4-6"


def test_anthropic_invoker_returns_empty_when_no_text_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the response has only thinking/tool_use blocks, invoker returns ''."""

    class FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="thinking", text="…"),
                    SimpleNamespace(type="tool_use", name="x", input={}),
                ]
            )

    class FakeAnthropic:
        def __init__(self) -> None:
            self.messages = FakeMessages()

    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropic)

    invoker = _make_anthropic_invoker(_make_config())
    assert invoker("prompt") == ""


def test_llmgw_invoker_handles_str_and_list_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMGW invoker passes through str content and stringifies list content."""
    responses: list[Any] = [
        SimpleNamespace(content="ok"),
        SimpleNamespace(content=["ok", " more"]),
    ]

    class FakeChatModel:
        def invoke(self, prompt: str) -> Any:
            return responses.pop(0)

    def fake_get_chat_model(**kwargs: Any) -> FakeChatModel:
        return FakeChatModel()

    monkeypatch.setattr("uipath_llmgw_client.get_langchain_chat_model", fake_get_chat_model)

    invoker = _make_llmgw_invoker(_make_config())
    assert invoker("p1") == "ok"
    assert invoker("p2") == str(["ok", " more"])


def test_review_uses_callable_invoker(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMReviewer.review() treats _llm as a Callable[[str], str]."""
    config = _make_config()
    # Force the Anthropic backend path without actually constructing a client
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

    reviewer = LLMReviewer(config)
    reviewer._llm = lambda prompt: '{"issues":"x","score":0.5,"next_steps":[],"should_continue":false}'

    decision = reviewer.review(
        task_description="t",
        agent_output="o",
        current_iteration=1,
        max_iterations=1,
    )

    assert decision is not None
    assert decision.score == 0.5
    assert decision.issues == "x"
