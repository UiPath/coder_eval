"""LLM-based prompt rephrasing via UiPath LLM Gateway.

Provides `create_rephrase_fn()` which returns a callback compatible with
`apply_prompt_mutations()`.
"""

from __future__ import annotations

import logging
from typing import Any

from ..evaluation.llmgw import get_llmgw_chat_model
from ..models import PromptRephrase, RephraseFn


logger = logging.getLogger(__name__)


def create_rephrase_fn() -> RephraseFn:
    """Create a rephrase callback using UiPath LLM Gateway.

    The LLM Gateway client is cached per (model, temperature, max_tokens)
    to avoid re-creating clients for identical configs.

    Returns:
        Callable that takes (current_prompt, PromptRephrase) and returns rephrased text.

    Raises:
        RuntimeError: If uipath_llmgw_client is not installed.
    """
    cache: dict[tuple[str, float, int], Any] = {}

    def _get_llm(mutation: PromptRephrase) -> Any:
        key = (mutation.model, mutation.temperature, mutation.max_tokens)
        if key not in cache:
            cache[key] = get_llmgw_chat_model(
                model=mutation.model,
                temperature=mutation.temperature,
                max_tokens=mutation.max_tokens,
            )
        return cache[key]

    def rephrase(prompt: str, mutation: PromptRephrase) -> str:
        llm = _get_llm(mutation)
        system_msg = (
            "You are a prompt rewriter. Rephrase the given prompt according to the instructions. "
            "Return ONLY the rephrased prompt text, nothing else — no preamble, no explanation."
        )
        user_msg = f"Instructions: {mutation.instructions}\n\nPrompt to rephrase:\n{prompt}"
        try:
            response = llm.invoke(
                [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ]
            )
        except Exception as e:
            raise RuntimeError(f"Prompt rephrase failed (model='{mutation.model}'): {e}") from e
        content = response.content
        return content if isinstance(content, str) else str(content)

    return rephrase
