"""Tests for the Rich + logging stream renderers."""

import io
import logging
from datetime import datetime

from rich.console import Console

from coder_eval.models import AgentUsage, CommandTelemetry, ResultSummary, TokenUsage
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    CriteriaCheckEvent,
    CriterionSummary,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnStartEvent,
)
from coder_eval.streaming.renderers import LoggingStreamRenderer, RichStreamRenderer


def _make_renderer(verbosity: str = "full", batch_mode: bool = False) -> tuple[RichStreamRenderer, io.StringIO]:
    """Create a renderer writing to a string buffer."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    renderer = RichStreamRenderer(console=console, verbosity=verbosity, batch_mode=batch_mode)
    return renderer, buf


def _tool(
    tool_name: str = "Bash", parameters: dict | None = None, result_summary: str | None = None
) -> CommandTelemetry:
    """Build a minimal CommandTelemetry for tool start/end events."""
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id="x",
        timestamp=datetime.now(),
        parameters=parameters or {},
        result_summary=result_summary,
    )


def test_agent_start_renders():
    """AgentStartEvent renders iteration info."""
    renderer, buf = _make_renderer()
    renderer.on_event(AgentStartEvent(task_id="t1", iteration=1, prompt="hello"))
    output = buf.getvalue()
    assert "Iteration 1" in output


def test_tool_start_renders():
    """ToolStartEvent renders tool name and params preview."""
    renderer, buf = _make_renderer()
    renderer.on_event(
        ToolStartEvent(
            task_id="t1",
            tool=_tool(tool_name="Bash", parameters={"command": "echo hi"}),
        )
    )
    output = buf.getvalue()
    assert "Bash" in output


def test_tool_end_renders_success():
    """ToolEndEvent renders OK for successful results."""
    renderer, buf = _make_renderer()
    renderer.on_event(
        ToolEndEvent(
            task_id="t1",
            tool=_tool(tool_name="Bash", result_summary="hi"),
            status=ToolEndStatus.OK,
        )
    )
    output = buf.getvalue()
    assert "OK" in output or "ok" in output.lower()


def test_tool_end_renders_error():
    """ToolEndEvent renders ERROR for failed results."""
    renderer, buf = _make_renderer()
    renderer.on_event(
        ToolEndEvent(
            task_id="t1",
            tool=_tool(tool_name="Bash", result_summary="file not found"),
            status=ToolEndStatus.ERROR,
        )
    )
    output = buf.getvalue()
    assert "ERROR" in output or "error" in output.lower()


def test_text_chunk_renders():
    """TextChunkEvent renders the assistant text."""
    renderer, buf = _make_renderer()
    renderer.on_event(TextChunkEvent(task_id="t1", text="I'll create the file."))
    output = buf.getvalue()
    assert "create the file" in output


def test_agent_end_renders():
    """AgentEndEvent renders summary stats (duration)."""
    renderer, buf = _make_renderer()
    renderer.on_event(
        AgentEndEvent(
            task_id="t1",
            iteration=1,
            duration_seconds=12.5,
            usage=AgentUsage(tokens=TokenUsage(input_tokens=1000, output_tokens=200)),
        )
    )
    output = buf.getvalue()
    assert "12.5" in output or "12.5s" in output


def test_criteria_check_renders():
    """CriteriaCheckEvent renders pass/fail summary."""
    renderer, buf = _make_renderer()
    renderer.on_event(
        CriteriaCheckEvent(
            task_id="t1",
            passed=3,
            total=4,
            weighted_score=0.875,
            details=["file_exists: PASS"],
        )
    )
    output = buf.getvalue()
    assert "3/4" in output


def test_minimal_verbosity_skips_tool_events():
    """Minimal verbosity only shows turn-level and criteria events."""
    renderer, buf = _make_renderer(verbosity="minimal")
    renderer.on_event(ToolStartEvent(task_id="t1", tool=_tool(tool_name="Bash")))
    renderer.on_event(TextChunkEvent(task_id="t1", text="some text"))
    output = buf.getvalue()
    assert output.strip() == ""  # Nothing rendered for tool/text events


def test_minimal_verbosity_shows_turn_events():
    """Minimal verbosity still shows agent start/end and criteria."""
    renderer, buf = _make_renderer(verbosity="minimal")
    renderer.on_event(AgentStartEvent(task_id="t1", iteration=1, prompt=""))
    renderer.on_event(CriteriaCheckEvent(task_id="t1", passed=2, total=2, weighted_score=1.0, details=[]))
    output = buf.getvalue()
    assert "Iteration 1" in output
    assert "2/2" in output


def test_batch_mode_prefixes_task_id():
    """Batch mode prepends [task_id] to output."""
    renderer, buf = _make_renderer(batch_mode=True)
    renderer.on_event(AgentStartEvent(task_id="my-task", iteration=1, prompt=""))
    output = buf.getvalue()
    assert "my-task" in output


def test_rich_markup_in_agent_output_is_escaped():
    """Agent output containing Rich markup sequences is escaped, not rendered."""
    renderer, buf = _make_renderer()
    # Tool name and params with Rich markup that should NOT be interpreted
    renderer.on_event(
        ToolStartEvent(
            task_id="t1",
            tool=_tool(tool_name="[bold]EvilTool[/bold]", parameters={"arg": "[red]malicious[/red]"}),
        )
    )
    output = buf.getvalue()
    # The literal brackets should appear in output (escaped), not as formatting
    assert "\\[bold]EvilTool" in output or "[bold]EvilTool" in output


def test_rich_markup_in_text_chunk_is_escaped():
    """TextChunkEvent text with Rich markup is escaped."""
    renderer, buf = _make_renderer()
    renderer.on_event(TextChunkEvent(task_id="t1", text="I'll use [bold]formatting[/bold] here"))
    output = buf.getvalue()
    assert "\\[bold]formatting" in output or "[bold]formatting" in output


def test_criteria_check_renders_detailed_pass_and_fail():
    """CriteriaCheckEvent with criteria summaries renders per-criterion lines."""
    renderer, buf = _make_renderer()
    renderer.on_event(
        CriteriaCheckEvent(
            task_id="t1",
            passed=1,
            total=2,
            weighted_score=0.375,
            criteria=[
                CriterionSummary(
                    criterion_type="run_command",
                    description="uip maestro flow validate passes",
                    score=1.0,
                    passed=True,
                ),
                CriterionSummary(
                    criterion_type="run_command",
                    description="Flow debug returns correct output",
                    score=0.0,
                    passed=False,
                    failure_reason="FAIL: 'mckinney' found in outputs",
                ),
            ],
        )
    )
    output = buf.getvalue()
    assert "1/2" in output
    assert "0.375" in output
    assert "PASS" in output
    assert "validate passes" in output
    assert "FAIL" in output
    assert "correct output" in output
    assert "mckinney" in output


def test_criteria_check_omits_reason_for_passing():
    """Passing criteria don't render a failure reason line."""
    renderer, buf = _make_renderer()
    renderer.on_event(
        CriteriaCheckEvent(
            task_id="t1",
            passed=1,
            total=1,
            weighted_score=1.0,
            criteria=[
                CriterionSummary(
                    criterion_type="file_exists",
                    description="Output file exists",
                    score=1.0,
                    passed=True,
                ),
            ],
        )
    )
    output = buf.getvalue()
    assert "PASS" in output
    assert ">" not in output  # No failure reason indicator


def test_rich_agent_start_includes_model():
    """AgentStartEvent renders the model when present."""
    renderer, buf = _make_renderer()
    renderer.on_event(AgentStartEvent(task_id="t1", iteration=2, prompt="", model="claude-opus-4-8"))
    assert "model=claude-opus-4-8" in buf.getvalue()


def test_rich_agent_end_surfaces_crash_and_max_turns():
    """AgentEndEvent on the console surfaces max_turns + crash reason + error detail."""
    renderer, buf = _make_renderer()
    renderer.on_event(
        AgentEndEvent(
            task_id="t1",
            status=AgentEndStatus.CRASHED,
            duration_seconds=1.0,
            max_turns_exhausted=True,
            crashed=True,
            crash_reason="CLI process failed (exit code 1)",
            result_summary=ResultSummary(is_error=True, subtype="error_during_execution", result="boom"),
        )
    )
    output = buf.getvalue()
    assert "max_turns exhausted" in output
    assert "reason:" in output and "exit code 1" in output
    assert "detail:" in output and "error_during_execution" in output


# --- LoggingStreamRenderer (task.log path) ---------------------------------


def _logged_lines(caplog) -> str:
    """Join all records emitted by the logging renderer's module logger."""
    return "\n".join(r.getMessage() for r in caplog.records)


def test_logging_agent_start_includes_model(caplog):
    """AgentStartEvent log line carries iteration and model."""
    renderer = LoggingStreamRenderer()
    with caplog.at_level(logging.DEBUG, logger="coder_eval.streaming.renderers"):
        renderer.on_event(AgentStartEvent(task_id="t1", iteration=3, prompt="", model="claude-opus-4-8"))
    out = _logged_lines(caplog)
    assert "Iteration 3" in out
    assert "model=claude-opus-4-8" in out


def test_logging_turn_start_renders(caplog):
    """TurnStartEvent produces a turn-start log line with id and model."""
    renderer = LoggingStreamRenderer()
    with caplog.at_level(logging.DEBUG, logger="coder_eval.streaming.renderers"):
        renderer.on_event(TurnStartEvent(task_id="t1", turn_id="msg_123", model="claude-opus-4-8"))
    out = _logged_lines(caplog)
    assert "Turn start" in out
    assert "id=msg_123" in out
    assert "model=claude-opus-4-8" in out


def test_logging_text_chunk_is_skipped(caplog):
    """TextChunkEvent is intentionally not logged (avoids task.log clutter)."""
    renderer = LoggingStreamRenderer()
    with caplog.at_level(logging.DEBUG, logger="coder_eval.streaming.renderers"):
        renderer.on_event(TextChunkEvent(task_id="t1", text="streaming text"))
    assert len(caplog.records) == 0


def test_logging_agent_end_notes_max_turns(caplog):
    """A clean max_turns-exhausted exit is annotated without a crash reason."""
    renderer = LoggingStreamRenderer()
    with caplog.at_level(logging.DEBUG, logger="coder_eval.streaming.renderers"):
        renderer.on_event(
            AgentEndEvent(
                task_id="t1",
                status=AgentEndStatus.MAX_TURNS_EXHAUSTED,
                duration_seconds=2.0,
                max_turns_exhausted=True,
                crashed=False,
            )
        )
    out = _logged_lines(caplog)
    assert "max_turns exhausted" in out
    assert "reason:" not in out


def test_logging_agent_end_renders_crash_reason(caplog):
    """A crashed exit appends the crash reason on its own line."""
    renderer = LoggingStreamRenderer()
    with caplog.at_level(logging.DEBUG, logger="coder_eval.streaming.renderers"):
        renderer.on_event(
            AgentEndEvent(
                task_id="t1",
                status=AgentEndStatus.CRASHED,
                duration_seconds=2.0,
                crashed=True,
                crash_reason="Communication with agent failed: boom",
            )
        )
    out = _logged_lines(caplog)
    assert "Agent complete [crashed]" in out
    assert "reason: Communication with agent failed: boom" in out


def test_logging_agent_end_surfaces_noncrash_error_detail(caplog):
    """An is_error ResultSummary on a non-crash exit still surfaces detail."""
    renderer = LoggingStreamRenderer()
    with caplog.at_level(logging.DEBUG, logger="coder_eval.streaming.renderers"):
        renderer.on_event(
            AgentEndEvent(
                task_id="t1",
                status=AgentEndStatus.COMPLETED,
                duration_seconds=2.0,
                crashed=False,
                result_summary=ResultSummary(
                    is_error=True, subtype="error_max_turns", stop_reason="max_turns", result="hit the cap"
                ),
            )
        )
    out = _logged_lines(caplog)
    assert "detail:" in out
    assert "error_max_turns" in out
    assert "hit the cap" in out
    assert "reason:" not in out  # not crashed → no crash_reason line


def test_logging_agent_end_no_detail_when_not_error(caplog):
    """A clean COMPLETED exit with a non-error ResultSummary adds no detail line."""
    renderer = LoggingStreamRenderer()
    with caplog.at_level(logging.DEBUG, logger="coder_eval.streaming.renderers"):
        renderer.on_event(
            AgentEndEvent(
                task_id="t1",
                status=AgentEndStatus.COMPLETED,
                duration_seconds=2.0,
                crashed=False,
                result_summary=ResultSummary(is_error=False, subtype="success"),
            )
        )
    out = _logged_lines(caplog)
    assert "detail:" not in out
    assert "reason:" not in out
