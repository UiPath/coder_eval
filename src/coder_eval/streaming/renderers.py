"""Rich terminal renderer for streaming events."""

import logging
import threading

from rich.console import Console
from rich.markup import escape

from coder_eval.formatting import format_payload, format_token_usage
from coder_eval.models import ResultSummary
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentStartEvent,
    CriteriaCheckEvent,
    CriterionSummary,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)


logger = logging.getLogger(__name__)


# Budgets tuned so that a typical JSON payload (agent params + one tool
# result) fits without truncation but runaway stdout still stays bounded.
_MAX_PARAMS_LEN = 800
_MAX_RESULT_LEN = 800


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if it exceeds max_len."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _format_result_error(summary: ResultSummary | None) -> str | None:
    """Compact SDK error detail from a ResultSummary, or None when not an error.

    Surfaces the diagnostic detail of an ``is_error`` ResultMessage even on
    exits the agent treats as clean (a benign error-subtype result, or a
    ``max_turns`` short-circuit) — paths where ``crash_reason`` is unset
    because the turn never raised. Both renderers share this so the detail is
    rendered identically on the console and in task.log.
    """
    if summary is None or not summary.is_error:
        return None
    parts: list[str] = []
    if summary.subtype:
        parts.append(f"subtype={summary.subtype}")
    if summary.stop_reason:
        parts.append(f"stop_reason={summary.stop_reason}")
    if summary.result:
        parts.append(f"result={_truncate(summary.result, _MAX_RESULT_LEN)}")
    return ", ".join(parts) if parts else "(no detail)"


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
        if self._verbosity == "minimal" and isinstance(event, (ToolStartEvent, ToolEndEvent, TextChunkEvent)):
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
        if isinstance(event, AgentStartEvent):
            model = f" (model={escape(event.model)})" if event.model else ""
            return f"[bold]--- Iteration {event.iteration}{model} ---[/bold]"

        # TurnStartEvent is intentionally not rendered on the console: the Rich
        # view is a terse live feed and Claude emits one per API call (noisy).
        # The full turn tree lives in task.log via LoggingStreamRenderer.

        if isinstance(event, ToolStartEvent):
            params_str = escape(format_payload(event.tool.parameters, max_chars=_MAX_PARAMS_LEN))
            return f"[cyan]>>> TOOL: {escape(event.tool.tool_name)}[/cyan] | {params_str}"

        if isinstance(event, ToolEndEvent):
            # result_summary is stored WHOLE (untruncated) at capture; cap it here
            # for the terse live feed so a large command output doesn't flood the
            # console. The reported char count reflects the true (full) length.
            full = event.tool.result_summary or ""
            preview = escape(_truncate(full, _MAX_RESULT_LEN))
            if event.status == ToolEndStatus.OK:
                tag = "[green]<<< OK[/green]"
            elif event.status == ToolEndStatus.UNRESOLVED:
                tag = "[yellow]<<< UNRESOLVED:[/yellow]"
            else:
                tag = f"[red]<<< {escape(event.status.value.upper())}:[/red]"
            return f"{tag} ({len(full)} chars) {preview}"

        if isinstance(event, TextChunkEvent):
            return f"[dim]{escape(event.text)}[/dim]"

        if isinstance(event, AgentEndEvent):
            usage_str = escape(format_token_usage(event.usage))
            line = (
                f"[bold]--- Turn complete: {len(event.messages)} msgs, "
                f"{event.duration_seconds:.1f}s, {usage_str} ---[/bold]"
            )
            if event.max_turns_exhausted:
                line += " [yellow](max_turns exhausted)[/yellow]"
            if event.crashed and event.crash_reason:
                line += f"\n[red]    reason: {escape(event.crash_reason)}[/red]"
            error_detail = _format_result_error(event.result_summary)
            if error_detail is not None:
                line += f"\n[red]    detail: {escape(error_detail)}[/red]"
            return line

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


class LoggingStreamRenderer:
    """Logs streaming events to the task logger (for task.log file capture).

    Converts stream events into DEBUG log lines, making them available for
    task.log persistence. This is the single, agent-agnostic place where the
    event stream becomes task.log lines — agents emit events and get logging
    for free, with no per-agent message-dumping.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def on_event(self, event: StreamEvent) -> None:
        """Log a streaming event to the task logger."""
        line = self._format_event(event)
        if line is None:
            return

        with self._lock:
            logger.debug(line)

    def _format_event(self, event: StreamEvent) -> str | None:
        """Format a single event into a log line (plain text, no Rich markup).

        Logs lifecycle + tool boundaries; skips TextChunkEvent to avoid
        cluttering task.log with streaming text chunks.
        """
        if isinstance(event, AgentStartEvent):
            model = f" (model={event.model})" if event.model else ""
            return f"[{event.task_id}] --- Iteration {event.iteration}{model} ---"

        if isinstance(event, TurnStartEvent):
            model = f" model={event.model}" if event.model else ""
            return f"[{event.task_id}] >>> Turn start: id={event.turn_id}{model}"

        if isinstance(event, ToolStartEvent):
            params_str = format_payload(event.tool.parameters, max_chars=_MAX_PARAMS_LEN)
            return (
                f"[{event.task_id}] >>> TOOL CALL: {event.tool.tool_name} "
                f"| id={event.tool.tool_id} | params={params_str}"
            )

        if isinstance(event, ToolEndEvent):
            return (
                f"[{event.task_id}] <<< TOOL RESULT [{event.status.value.upper()}]: "
                f"id={event.tool.tool_id} | {event.tool.result_summary or ''}"
            )

        if isinstance(event, TextChunkEvent):
            # Skip text chunks to avoid cluttering logs with streaming text
            return None

        if isinstance(event, TurnEndEvent):
            tok = format_token_usage(event.tokens) if event.tokens is not None else "n/a"
            return f"[{event.task_id}] --- Turn end [{event.status.value}]: {tok} ---"

        if isinstance(event, AgentEndEvent):
            usage_str = format_token_usage(event.usage)
            line = (
                f"[{event.task_id}] --- Agent complete [{event.status.value}]: "
                f"{len(event.messages)} msgs, {event.duration_seconds:.1f}s, {usage_str} ---"
            )
            if event.max_turns_exhausted:
                line += " (max_turns exhausted)"
            if event.crashed and event.crash_reason:
                line += f"\n[{event.task_id}]     reason: {event.crash_reason}"
            error_detail = _format_result_error(event.result_summary)
            if error_detail is not None:
                line += f"\n[{event.task_id}]     detail: {error_detail}"
            return line

        if isinstance(event, CriteriaCheckEvent):
            details = " | ".join(event.details) if event.details else f"{event.passed}/{event.total}"
            return (
                f"[{event.task_id}] Criteria: {event.passed}/{event.total} passed "
                f"(score: {event.weighted_score:.3f}) [{details}]"
            )

        return None
