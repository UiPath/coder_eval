"""Shared formatters for evaluation-related summaries."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from coder_eval.models import CommandTelemetry


def summarize_commands(commands: list[CommandTelemetry]) -> str | None:
    """Build a concise one-line-per-command summary of agent tool calls.

    Used by the llm_judge criterion checker.

    Args:
        commands: Ordered list of ``CommandTelemetry`` entries (typically
            ``turn_record.commands``).

    Returns:
        Formatted multi-line string, or ``None`` when ``commands`` is empty.
    """
    if not commands:
        return None

    lines = []
    for i, cmd in enumerate(commands, 1):
        status = cmd.result_status or "unknown"
        # Extract the most useful parameter for each tool type
        detail = ""
        params = cmd.parameters
        if cmd.tool_name == "Bash" and "command" in params:
            detail = f" `{params['command'][:120]}`"
        elif cmd.tool_name in ("Read", "Write", "Edit", "Glob") and "file_path" in params:
            detail = f" {params['file_path']}"
        elif cmd.tool_name == "Grep" and "pattern" in params:
            detail = f" pattern={params['pattern'][:60]}"
        elif cmd.tool_name in ("Task", "Agent"):
            detail = f" ({params.get('description', '')[:60]})"

        result_preview = ""
        if cmd.result_summary:
            result_preview = f" → {cmd.result_summary[:80]}"

        lines.append(f"  {i}. [{status}] {cmd.tool_name}{detail}{result_preview}")

    return "\n".join(lines)
