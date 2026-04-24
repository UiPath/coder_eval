"""Unit tests for the conversation.log utterance extractor.

Covers ``coder_eval.orchestrator._extract_utterance`` — a pure function that
collapses ClaudeCodeAgent's tagged message format into a clean utterance for
the per-task ``conversation.log``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coder_eval.orchestration.task_loader import load_task
from coder_eval.orchestrator import Orchestrator, _extract_utterance


def test_empty_input_returns_empty() -> None:
    assert _extract_utterance("") == ""


def test_untagged_text_passes_through_unchanged() -> None:
    raw = "Start a composer session."
    assert _extract_utterance(raw) == raw


def test_multiline_untagged_text_passes_through_unchanged() -> None:
    raw = "First line.\nSecond line.\nThird line."
    assert _extract_utterance(raw) == raw


def test_result_success_strips_its_label() -> None:
    raw = "[ASSISTANT] thinking...\n[RESULT - SUCCESS] final answer"
    assert _extract_utterance(raw) == "final answer"


def test_result_success_wins_over_assistant_when_both_present() -> None:
    """The SDK duplicates the final assistant text in ResultMessage.result —
    we prefer the RESULT payload so conversation.log doesn't repeat itself."""
    raw = "[ASSISTANT] Here is the answer.\n[RESULT - SUCCESS] Here is the answer."
    assert _extract_utterance(raw) == "Here is the answer."


def test_result_error_keeps_label_prefix() -> None:
    """Asymmetry: error results keep their label so the error state is visible."""
    raw = "[ASSISTANT] trying\n[RESULT - ERROR] tool failed: timeout"
    assert _extract_utterance(raw) == "[RESULT - ERROR] tool failed: timeout"


def test_only_assistant_blocks_fall_back_to_assistant() -> None:
    raw = "[ASSISTANT] Reasoning step one.\n[ASSISTANT] Reasoning step two."
    assert _extract_utterance(raw) == "Reasoning step one.\n\nReasoning step two."


def test_tool_use_lines_are_dropped() -> None:
    raw = "[ASSISTANT] Let me check.\n[TOOL USE] Read\n[RESULT - SUCCESS] Here is the file."
    assert _extract_utterance(raw) == "Here is the file."


def test_multiline_tagged_content_preserved() -> None:
    raw = "[RESULT - SUCCESS] line one\nline two\nline three"
    assert _extract_utterance(raw) == "line one\nline two\nline three"


def test_unknown_bracket_tag_is_preserved_as_content() -> None:
    """`[NOTE]`, `[TODO]`, pylint codes, markdown footnotes etc. must NOT be
    classified as message tags — they are content and must survive."""
    raw = "[RESULT - SUCCESS] See [NOTE] about edge case and [E501] lint issue."
    assert _extract_utterance(raw) == "See [NOTE] about edge case and [E501] lint issue."


def test_pre_tag_content_superseded_by_result() -> None:
    """When a RESULT block is present it supersedes all ASSISTANT content,
    including any pre-tag prefix. RESULT is the SDK's canonical final
    utterance; ASSISTANT lines are chain-of-thought the RESULT already
    incorporates."""
    raw = "Free-form prefix that matters.\n[RESULT - SUCCESS] final"
    assert _extract_utterance(raw) == "final"


def test_pre_tag_content_preserved_on_assistant_fallback() -> None:
    """On the ASSISTANT-fallback path (no RESULT present), pre-tag content
    survives as an implicit ASSISTANT block so stray pre-tag text does
    not silently disappear."""
    raw = "Free-form prefix that matters.\n[ASSISTANT] tagged content."
    assert _extract_utterance(raw) == "Free-form prefix that matters.\n\ntagged content."


def test_simulator_duplicate_collapse_skip_case() -> None:
    """Real case from the bug: simulator says `skip`, output is tagged twice
    (once as ASSISTANT, once as RESULT). Should collapse to a single `skip`."""
    raw = "[ASSISTANT] skip\n[RESULT - SUCCESS] skip"
    assert _extract_utterance(raw) == "skip"


def test_multiple_result_blocks_joined_with_blank_line() -> None:
    raw = "[RESULT - SUCCESS] first\n[RESULT - SUCCESS] second"
    assert _extract_utterance(raw) == "first\n\nsecond"


@pytest.mark.parametrize(
    "tag",
    [
        "[USER]",
        "[SYSTEM]",
        "[DEBUG]",
        "[INFO]",
        "[1]",
    ],
)
def test_non_enumerated_tags_are_content_not_markers(tag: str) -> None:
    raw = f"{tag} this line should pass through verbatim"
    assert _extract_utterance(raw) == raw


# ---------------------------------------------------------------------------
# Orchestrator._log_conversation — format lock-in
# ---------------------------------------------------------------------------


def _make_orchestrator(tmp_path: Path) -> Orchestrator:
    """Construct an Orchestrator against an existing task for format tests.

    ``hello_date`` is the lightest existing task in the repo — we only touch
    ``_log_conversation``, not the dialog loop, so the task's shape doesn't
    matter.
    """
    task, _ = load_task(Path("tasks/hello_date.yaml"))
    run_dir = tmp_path / "conversation_log_fmt"
    run_dir.mkdir(parents=True)
    return Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")


def test_log_conversation_writes_header_and_body(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch._log_conversation("USER", 1, "Start a composer session.")

    content = orch.conversation_log_path.read_text(encoding="utf-8")
    assert content == "=== USER (turn 1) ===\nStart a composer session.\n\n"


def test_log_conversation_appends_metadata(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch._log_conversation("USER", 1, "hi", metadata="pinned initial_prompt")

    content = orch.conversation_log_path.read_text(encoding="utf-8")
    assert content == "=== USER (turn 1) — pinned initial_prompt ===\nhi\n\n"


def test_log_conversation_runs_extract_utterance_on_body(tmp_path: Path) -> None:
    """The writer must strip ClaudeCodeAgent tags — that's its whole job."""
    orch = _make_orchestrator(tmp_path)
    raw = "[ASSISTANT] thinking\n[RESULT - SUCCESS] final answer"
    orch._log_conversation("AGENT", 1, raw, metadata="1.0s")

    content = orch.conversation_log_path.read_text(encoding="utf-8")
    assert content == "=== AGENT (turn 1) — 1.0s ===\nfinal answer\n\n"


def test_log_conversation_appends_multiple_turns(tmp_path: Path) -> None:
    """Subsequent calls append (no clobber), separated by a blank line."""
    orch = _make_orchestrator(tmp_path)
    orch._log_conversation("USER", 1, "hello")
    orch._log_conversation("AGENT", 1, "hi back")

    content = orch.conversation_log_path.read_text(encoding="utf-8")
    assert content == ("=== USER (turn 1) ===\nhello\n\n=== AGENT (turn 1) ===\nhi back\n\n")


def test_log_conversation_rstrips_trailing_whitespace_in_body(tmp_path: Path) -> None:
    """Body rstrip is load-bearing — preserves exactly one blank line between
    utterances regardless of how much trailing whitespace the source has."""
    orch = _make_orchestrator(tmp_path)
    orch._log_conversation("USER", 1, "content with trailing\n\n\n\n")

    content = orch.conversation_log_path.read_text(encoding="utf-8")
    assert content == "=== USER (turn 1) ===\ncontent with trailing\n\n"
