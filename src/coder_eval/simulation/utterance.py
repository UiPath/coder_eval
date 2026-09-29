"""Collapse a Claude Code agent's formatted transcript into one plain utterance.

``ClaudeCodeAgent`` reports ``agent_output`` as a tagged transcript, not as the
text the model said. Anything that treats that output as a message (the user
simulator's next turn, the conversation log) must collapse it first.
"""

from __future__ import annotations

import re


# Structural tags emitted by ClaudeCodeAgent._format_messages, which is the SSOT
# for the vocabulary. Other bracketed words (markdown footnotes, pylint codes,
# unknown SDK message types) are intentionally NOT matched — they pass through as
# content. The user simulator's next turn goes through this parser, so a tag it
# misses reaches the coding agent as literal text.
_UTTERANCE_TAG_RE = re.compile(r"^\[(ASSISTANT|RESULT - SUCCESS|RESULT - ERROR|TOOL USE)\](?: (.*))?$")


def extract_utterance(raw: str) -> str:
    """Collapse a ClaudeCodeAgent-formatted transcript to a clean utterance.

    Input looks like::

        [ASSISTANT] Sure, I'll do X.
        [TOOL USE] Read
        [RESULT - SUCCESS] Here is the answer...

    Prefers a non-empty ``[RESULT - ...]`` payload — the SDK's canonical final
    utterance, which duplicates the final assistant text; without this collapse
    every simulated-user turn reaches the coding agent twice, wrapped in tags. Falls back to
    concatenated ``[ASSISTANT]`` blocks, including any content appearing before
    the first tag. ``[TOOL USE]`` lines are dropped, and untagged input (a pinned
    ``initial_prompt``) is returned unchanged.

    Asymmetric on purpose: ``[RESULT - SUCCESS]`` strips its label, while
    ``[RESULT - ERROR]`` KEEPS its prefix so the error state stays visible in the
    log.
    """
    if not raw:
        return ""
    lines = raw.splitlines()
    if not any(_UTTERANCE_TAG_RE.match(ln) for ln in lines):
        return raw

    assistant_parts: list[str] = []
    result_parts: list[str] = []
    # Pre-tag content becomes an implicit ASSISTANT block (not dropped).
    current_tag: str = "ASSISTANT"
    current_buf: list[str] = []

    def _flush() -> None:
        text = "\n".join(current_buf).strip()
        if not text:
            return
        if current_tag == "ASSISTANT":
            assistant_parts.append(text)
        elif current_tag == "RESULT - SUCCESS":
            result_parts.append(text)
        elif current_tag == "RESULT - ERROR":
            result_parts.append(f"[RESULT - ERROR] {text}")
        # TOOL USE is dropped.

    for ln in lines:
        match = _UTTERANCE_TAG_RE.match(ln)
        if match:
            _flush()
            current_tag = match.group(1)
            current_buf = [match.group(2) or ""]
        else:
            current_buf.append(ln)
    _flush()

    if result_parts:
        return "\n\n".join(result_parts)
    if assistant_parts:
        return "\n\n".join(assistant_parts)
    return raw
