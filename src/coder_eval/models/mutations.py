"""Prompt mutation models and application function.

Prompt mutations are ordered transforms applied to a task's base initial_prompt
at variant resolution time. They enable A/B testing of prompt phrasing without
duplicating task definitions.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class PromptPrefix(BaseModel):
    """Prepend text to the prompt."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["prefix"] = "prefix"
    content: str = Field(description="Text to prepend before the base prompt")
    separator: str = Field(default="\n\n", description="Separator between prefix and base prompt")


class PromptSuffix(BaseModel):
    """Append text to the prompt."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["suffix"] = "suffix"
    content: str = Field(description="Text to append after the base prompt")
    separator: str = Field(default="\n\n", description="Separator between base prompt and suffix")


class PromptReplace(BaseModel):
    """Find and replace text in the prompt."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["replace"] = "replace"
    pattern: str = Field(description="Text or regex pattern to find")
    replacement: str = Field(description="Replacement text")
    regex: bool = Field(default=False, description="Whether pattern is a regular expression")


class PromptTemplate(BaseModel):
    """Substitute template variables in the prompt using {variable_name} syntax."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["template"] = "template"
    variables: dict[str, str] = Field(description="Mapping of variable names to values")


PromptMutation = Annotated[
    PromptPrefix | PromptSuffix | PromptReplace | PromptTemplate,
    Field(discriminator="type"),
]


def apply_prompt_mutations(
    base_prompt: str,
    mutations: list[PromptMutation],
) -> str:
    """Apply an ordered list of mutations to a base prompt string.

    Mutations are applied sequentially — each operates on the result of the previous.

    Args:
        base_prompt: The original prompt text.
        mutations: Ordered list of mutation operations.

    Returns:
        The transformed prompt string.

    Raises:
        re.error: If a regex replace has an invalid pattern.
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
    return result
