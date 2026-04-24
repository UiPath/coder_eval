"""Shared prompt-context assembly for judge-style criteria.

Collects the context that both ``llm_judge`` and ``agent_judge`` feed to their
judge model: per-file blocks (with truncation + missing-file tracking),
optional reference solution, optional agent output, optional tool-call summary.

The builder returns *structured* data (``JudgeContext`` with typed blocks) so
each consumer can render its own prompt envelope — header wording differs per
judge (artifacts are live-available for agent_judge, text-only for llm_judge)
but retrieval/truncation/degradation logic is SSOT here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coder_eval.evaluation.summaries import summarize_commands


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox


logger = logging.getLogger(__name__)


def truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars, appending a marker when cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, orig {len(text)} chars)"


def scrub_reference(content: str, reference_code: str | None) -> str:
    """Redact any occurrence of ``reference_code`` in ``content``.

    No-op when ``reference_code`` is ``None`` or the empty string — guards
    against ``"".replace("", "<redacted>")`` ballooning the string.
    """
    if not reference_code:
        return content
    return content.replace(reference_code, "<reference redacted>")


@dataclass
class FileBlock:
    """A single attached file's rendered content, or ``None`` when the file was missing."""

    path: str
    content: str | None


@dataclass
class JudgeContext:
    """Structured judge prompt context.

    Consumers render their own envelope text from this — the builder intentionally
    does NOT produce the final user message so each judge can keep its own header
    wording while sharing retrieval/truncation/degradation logic.
    """

    files: list[FileBlock] = field(default_factory=list)
    reference: str | None = None
    agent_output: str | None = None
    tool_calls_summary: str | None = None
    missing_files: list[str] = field(default_factory=list)
    degraded_notes: list[str] = field(default_factory=list)


class JudgeContextBuilder:
    """Builds ``JudgeContext`` from criterion knobs + sandbox + turn records.

    Both ``LLMJudgeCriterion`` and ``AgentJudgeCriterion`` share the same context
    knobs (``files``, ``include_reference``, ``include_agent_output``,
    ``include_tool_calls``, ``max_file_chars``), so no adapter layer is needed —
    the builder is constructed from the criterion fields directly.
    """

    def __init__(
        self,
        *,
        files: list[str],
        include_reference: bool,
        include_agent_output: bool,
        include_tool_calls: bool,
        max_file_chars: int,
    ) -> None:
        self.files = list(files)  # defensive copy — caller's list may be mutated later
        self.include_reference = include_reference
        self.include_agent_output = include_agent_output
        self.include_tool_calls = include_tool_calls
        self.max_file_chars = max_file_chars

    def build(
        self,
        sandbox: Sandbox,
        reference_code: str | None,
        turn_records: list[TurnRecord] | None,
    ) -> JudgeContext:
        ctx = JudgeContext()
        self._collect_files(sandbox, ctx)
        self._collect_reference(reference_code, ctx)
        self._collect_trajectory(turn_records, ctx)
        return ctx

    def _collect_files(self, sandbox: Sandbox, ctx: JudgeContext) -> None:
        for path in self.files:
            if not sandbox.file_exists(path):
                ctx.missing_files.append(path)
                ctx.files.append(FileBlock(path=path, content=None))
                continue
            try:
                content = sandbox.get_file_content(path)
            except Exception as e:
                logger.debug("judge_context: failed to read %s: %s", path, e)
                # File existed, read failed — not tracked as "missing".
                ctx.files.append(FileBlock(path=path, content=f"<error reading file: {e}>"))
                continue
            ctx.files.append(FileBlock(path=path, content=truncate(content, self.max_file_chars)))

    def _collect_reference(self, reference_code: str | None, ctx: JudgeContext) -> None:
        if not self.include_reference:
            return
        if reference_code:
            ctx.reference = reference_code
            return
        # Silent omission matches legacy behavior — some tasks deliberately run without a reference.
        logger.debug("judge_context: include_reference=True but reference not set")

    def _collect_trajectory(self, turn_records: list[TurnRecord] | None, ctx: JudgeContext) -> None:
        latest = turn_records[-1] if turn_records else None

        if self.include_agent_output:
            if latest is None:
                ctx.degraded_notes.append("include_agent_output requested but no turn records available")
            elif latest.agent_output:
                ctx.agent_output = truncate(latest.agent_output, self.max_file_chars)
            else:
                ctx.degraded_notes.append("include_agent_output requested but latest agent output is empty")

        if self.include_tool_calls:
            if latest is None:
                ctx.degraded_notes.append("include_tool_calls requested but no turn records available")
            else:
                # summarize_commands returns None for empty/no-op command lists —
                # omit silently (a zero-command turn isn't a degradation).
                summary = summarize_commands(latest.commands)
                if summary is not None:
                    ctx.tool_calls_summary = summary


def format_details(score: float, rationale: str, missing_files: list[str], degraded_notes: list[str]) -> str:
    """Render the ``CriterionResult.details`` payload common to both judges."""
    lines = [f"score={score:.3f}", f"rationale: {rationale}"]
    if missing_files:
        lines.append(f"missing_files: {missing_files}")
    if degraded_notes:
        lines.append(f"notes: {'; '.join(degraded_notes)}")
    return "\n".join(lines)
