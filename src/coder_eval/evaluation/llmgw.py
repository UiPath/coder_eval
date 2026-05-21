"""Shared UiPath LLM Gateway client factory.

Centralizes the ``uipath_llmgw_client`` import so that prompt rephrasing
and the llm_judge criterion build LangChain chat models the same way.

``uipath_llmgw_client`` ships in the optional ``[uipath]`` extra. Without
it installed, ``get_llmgw_chat_model`` raises ``RuntimeError`` and
``is_llmgw_client_installed`` returns ``False`` so callers (notably
``models.routing._resolve_direct_judge_transport``) can route around the
missing transport at startup rather than crashing mid-run.

``INSTALL_HINT`` is the canonical install-hint string for error messages.
"""

from __future__ import annotations

import importlib.util
from typing import Any


# Canonical install hint. Imported by criteria/llm_judge.py, criteria/uipath_eval.py,
# and models/routing.py so the package spec lives in one place — if the extra is
# ever renamed, every user-visible error/warning updates together.
INSTALL_HINT = (
    "uipath_llmgw_client is required. Install with: "
    "pip install 'coder-eval[uipath]' (or `uv sync --extra uipath` for a dev checkout)"
)


def is_llmgw_client_installed() -> bool:
    """True iff ``uipath_llmgw_client`` can be imported in the current interpreter.

    Used by ``models.routing._resolve_direct_judge_transport`` to avoid
    selecting the LLMGW judge transport when the optional extra is not
    installed, even if LLMGW credentials happen to be in the environment.

    Resolution is cached by Python's import machinery, so this is cheap to
    call once at startup. If the user installs the extra mid-process,
    callers should restart to pick up the new state.
    """
    try:
        return importlib.util.find_spec("uipath_llmgw_client") is not None
    except (ImportError, ValueError):
        # Defensive: a partially-installed package can make ``find_spec``
        # raise. Treat any such failure as "not available" rather than
        # propagating — routing must not crash here.
        return False


def get_llmgw_chat_model(model: str, temperature: float = 0.0, max_tokens: int = 1000) -> Any:
    """Build a LangChain chat model routed through the UiPath LLM Gateway.

    Args:
        model: Gateway model name (e.g., ``"anthropic.claude-sonnet-4-6"``).
        temperature: Sampling temperature (0.0 = deterministic).
        max_tokens: Maximum tokens in the response.

    Returns:
        A LangChain chat model whose ``.invoke(messages)`` returns a response
        with ``.content`` of type ``str | list | dict`` depending on the
        underlying provider. Callers must narrow with
        ``isinstance(response.content, str)`` before persisting.

    Raises:
        RuntimeError: If ``uipath_llmgw_client`` is not installed. The
            message points at the optional ``[uipath]`` extra
            (``INSTALL_HINT``).
    """
    try:
        from uipath_llmgw_client import get_langchain_chat_model
    except ImportError as e:
        raise RuntimeError(INSTALL_HINT) from e

    return get_langchain_chat_model(
        model=model,
        llmgw_client_type="normalized",
        temperature=temperature,
        max_tokens=max_tokens,
    )
