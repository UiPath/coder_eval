"""Shared UiPath LLM Gateway client factory.

Centralizes the ``uipath_llmgw_client`` import so that LLMReviewer, prompt
rephrasing, and the llm_judge criterion all build LangChain chat models the
same way.
"""

from __future__ import annotations

from typing import Any


def llmgw_available() -> bool:
    """Return ``True`` when the ``uipath_llmgw_client`` package is importable.

    Used by callers (e.g. ``LLMReviewer.__init__``) that need to pick a backend
    without eagerly constructing a chat model (which may trigger auth).
    """
    try:
        import uipath_llmgw_client  # noqa: F401

        return True
    except ImportError:
        return False


def get_llmgw_chat_model(model: str, temperature: float = 0.0, max_tokens: int = 1000) -> Any:
    """Build a LangChain chat model routed through the UiPath LLM Gateway.

    The LangChain return type is intentionally untyped (``Any``): it is an
    implementation detail of ``uipath_llmgw_client`` and not part of the
    public surface of ``coder_eval``.

    Args:
        model: Gateway model name (e.g., ``"anthropic.claude-sonnet-4-6"``).
        temperature: Sampling temperature (0.0 = deterministic).
        max_tokens: Maximum tokens in the response.

    Raises:
        RuntimeError: If ``uipath_llmgw_client`` is not installed.
    """
    try:
        from uipath_llmgw_client import get_langchain_chat_model
    except ImportError as e:
        raise RuntimeError("uipath_llmgw_client is required. Install with: pip install uipath-llmgw-client") from e

    return get_langchain_chat_model(
        model=model,
        llmgw_client_type="normalized",
        temperature=temperature,
        max_tokens=max_tokens,
    )
