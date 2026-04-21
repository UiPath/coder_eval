"""Prompt mutation models and application function.

Prompt mutations are ordered transforms applied to a task's base initial_prompt
at variant resolution time. They enable A/B testing of prompt phrasing without
duplicating task definitions.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from coder_eval.models.gateway import DEFAULT_GATEWAY_MODEL


class PromptPrefix(BaseModel):
    """Prepend text to the prompt."""

    type: Literal["prefix"] = "prefix"
    content: str = Field(description="Text to prepend before the base prompt")
    separator: str = Field(default="\n\n", description="Separator between prefix and base prompt")


class PromptSuffix(BaseModel):
    """Append text to the prompt."""

    type: Literal["suffix"] = "suffix"
    content: str = Field(description="Text to append after the base prompt")
    separator: str = Field(default="\n\n", description="Separator between base prompt and suffix")


class PromptReplace(BaseModel):
    """Find and replace text in the prompt."""

    type: Literal["replace"] = "replace"
    pattern: str = Field(description="Text or regex pattern to find")
    replacement: str = Field(description="Replacement text")
    regex: bool = Field(default=False, description="Whether pattern is a regular expression")


class PromptTemplate(BaseModel):
    """Substitute template variables in the prompt using {variable_name} syntax."""

    type: Literal["template"] = "template"
    variables: dict[str, str] = Field(description="Mapping of variable names to values")


class PromptRephrase(BaseModel):
    """Rephrase the prompt using an LLM via UiPath LLM Gateway.

    Sends the current prompt text to an LLM along with rewriting instructions.
    The LLM returns a rephrased version. This is inherently non-deterministic;
    use low temperature for more consistent results.

    Uses the same LLM Gateway + LangChain integration as LLMReviewer.
    """

    type: Literal["rephrase"] = "rephrase"
    instructions: str = Field(description="Instructions for how the LLM should rephrase the prompt")
    model: str = Field(
        default=DEFAULT_GATEWAY_MODEL,
        description="Gateway model name (e.g., anthropic.claude-3-5-sonnet-20240620-v1:0, gpt-4o-2024-08-06)",
    )
    temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="Temperature for rephrasing (lower = more deterministic)",
    )
    max_tokens: int = Field(
        default=4096,
        gt=0,
        description="Maximum tokens in the rephrase response",
    )


type RephraseFn = Callable[[str, PromptRephrase], str]

PromptMutation = Annotated[
    PromptPrefix | PromptSuffix | PromptReplace | PromptTemplate | PromptRephrase,
    Field(discriminator="type"),
]


def apply_prompt_mutations(
    base_prompt: str,
    mutations: list[PromptMutation],
    rephrase_fn: RephraseFn | None = None,
) -> str:
    """Apply an ordered list of mutations to a base prompt string.

    Mutations are applied sequentially — each operates on the result of the previous.

    Args:
        base_prompt: The original prompt text.
        mutations: Ordered list of mutation operations.
        rephrase_fn: Callback for PromptRephrase mutations. Takes (current_prompt, mutation)
            and returns the rephrased prompt string. Required only when the mutations list
            contains a rephrase mutation.

    Returns:
        The transformed prompt string.

    Raises:
        re.error: If a regex replace has an invalid pattern.
        ValueError: If a rephrase mutation is encountered but rephrase_fn is None.
    """
    result = base_prompt
    for m in mutations:
        match m:
            case PromptPrefix():
                result = m.content + m.separator + result
            case PromptSuffix():
                result = result + m.separator + m.content
            case PromptReplace():
                if m.regex:
                    result = re.sub(m.pattern, m.replacement, result)
                else:
                    result = result.replace(m.pattern, m.replacement)
            case PromptTemplate():
                for key, val in m.variables.items():
                    result = result.replace(f"{{{key}}}", val)
            case PromptRephrase():
                if rephrase_fn is None:
                    raise ValueError("rephrase mutation requires rephrase_fn callback")
                result = rephrase_fn(result, m)
    return result
