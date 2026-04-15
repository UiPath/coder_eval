"""Rich terminal renderer for streaming events."""

import json
import threading

from rich.console import Console
from rich.markup import escape

from coder_eval.streaming.events import (
    CriteriaCheckEvent,
    CriterionSummary,
    StreamEvent,
    TextChunkEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    TurnStartEvent,
)


_MAX_PARAMS_LEN = 120
_MAX_RESULT_LEN = 200


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if it exceeds max_len."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


class RichStreamRenderer:
    """Renders streaming events to a Rich console."""

    def __init__(
        self,
        console: Console | None = None,
        verbosity: str = "full",
        batch_mode: bool = False,
    ) -> None:
        self._console = console or Console(stderr=True)
        self._verbosity = verbosity
        self._batch_mode = batch_mode
        self._lock = threading.Lock()

    def on_event(self, event: StreamEvent) -> None:
        """Render a streaming event to the console."""
        if self._verbosity == "minimal" and isinstance(event, (ToolCallEvent, ToolResultEvent, TextChunkEvent)):
            return

        line = self._format_event(event)
        if line is None:
            return

        if self._batch_mode:
            line = f"[dim]\\[{escape(event.task_id)}][/dim] {line}"

        with self._lock:
            self._console.print(line, highlight=False)

    def _format_event(self, event: StreamEvent) -> str | None:
        """Format a single event into a Rich markup string."""
        if isinstance(event, TurnStartEvent):
            return f"[bold]--- Iteration {event.iteration}/{event.max_iterations} ---[/bold]"

        if isinstance(event, ToolCallEvent):
            params_str = escape(_truncate(json.dumps(event.parameters, default=str), _MAX_PARAMS_LEN))
            return f"[cyan]>>> TOOL: {escape(event.tool_name)}[/cyan] | {params_str}"

        if isinstance(event, ToolResultEvent):
            if event.success:
                preview = escape(_truncate(event.result_preview, _MAX_RESULT_LEN))
                return f"[green]<<< OK[/green] ({len(event.result_preview)} chars) {preview}"
            preview = escape(_truncate(event.result_preview, _MAX_RESULT_LEN))
            return f"[red]<<< ERROR:[/red] {preview}"

        if isinstance(event, TextChunkEvent):
            return f"[dim]{escape(event.text)}[/dim]"

        if isinstance(event, TurnCompleteEvent):
            return (
                f"[bold]--- Turn complete: {event.command_count} commands, "
                f"{event.duration_s:.1f}s, {escape(event.token_usage_str)} ---[/bold]"
            )

        if isinstance(event, CriteriaCheckEvent):
            score_color = "green" if event.passed == event.total else "yellow"
            header = (
                f"[{score_color}]Criteria: {event.passed}/{event.total} passed"
                + f" (score: {event.weighted_score:.3f})[/{score_color}]"
            )
            if event.criteria:
                return self._format_criteria_details(header, event.criteria)
            # Fallback to legacy flat details
            if event.details:
                header += f" \\[{escape(' | '.join(event.details))}]"
            return header

        return None

    @staticmethod
    def _format_criteria_details(header: str, criteria: list[CriterionSummary]) -> str:
        """Format criteria with per-criterion lines and failure reasons.

        For failed criteria, the first line of the failure reason is shown
        at normal brightness; subsequent lines are dimmed.
        """
        lines = [header]
        for c in criteria:
            if c.passed:
                lines.append(f"  [green]PASS[/green]  {escape(c.criterion_type)}  {escape(c.description)}")
            else:
                lines.append(f"  [red]FAIL[/red]  {escape(c.criterion_type)}  {escape(c.description)}")
                if c.failure_reason:
                    reason_lines = c.failure_reason.splitlines()
                    lines.append(f"        > {escape(reason_lines[0])}")
                    for extra in reason_lines[1:]:
                        lines.append(f"        [dim]{escape(extra)}[/dim]")
        return "\n".join(lines)
