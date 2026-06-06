"""Tests for the Rich stream renderer."""

import io
from datetime import datetime

from rich.console import Console

from coder_eval.models import AgentUsage, CommandTelemetry, TokenUsage
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentStartEvent,
    CriteriaCheckEvent,
    CriterionSummary,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
)
from coder_eval.streaming.renderers import RichStreamRenderer


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
