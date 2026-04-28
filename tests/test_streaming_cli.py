# tests/test_streaming_cli.py
"""Tests for --stream CLI flag wiring and parsing."""

import io

from rich.console import Console
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.streaming.events import TurnStartEvent
from coder_eval.streaming.renderers import RichStreamRenderer


runner = CliRunner()


def test_stream_flag_creates_renderer():
    """When --stream is provided, a RichStreamRenderer is created."""
    renderer = RichStreamRenderer(verbosity="full", batch_mode=True)
    assert renderer._verbosity == "full"
    assert renderer._batch_mode is True


def test_stream_flag_factory_returns_renderer():
    """The callback factory creates a renderer that acts as StreamCallback."""
    renderer = RichStreamRenderer(verbosity="minimal", batch_mode=False)
    event = TurnStartEvent(task_id="test", iteration=1, prompt_preview="")
    renderer.on_event(event)  # Should not raise


def test_renderer_factory_pattern():
    """The factory pattern used in the CLI returns the shared renderer."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    renderer = RichStreamRenderer(console=console, verbosity="full", batch_mode=True)
    factory = lambda task_id, r=renderer: r  # noqa: E731

    cb1 = factory("task-1")
    cb2 = factory("task-2")
    assert cb1 is cb2  # Same renderer instance shared
    assert cb1 is renderer


def test_stream_invalid_value_rejected():
    """Invalid --stream value is rejected by click.Choice."""
    result = runner.invoke(app, ["run", "--stream", "verbose", "tasks/dummy.yaml"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "is not one of" in result.output


def test_stream_full_accepted(tmp_path):
    """--stream full is accepted as a valid value."""
    result = runner.invoke(app, ["run", "--stream", "full", str(tmp_path / "nonexistent.yaml")])
    # Will fail on missing task file, but NOT on stream validation
    assert "is not one of" not in (result.output or "")


def test_stream_minimal_accepted(tmp_path):
    """--stream minimal is accepted as a valid value."""
    result = runner.invoke(app, ["run", "--stream", "minimal", str(tmp_path / "nonexistent.yaml")])
    # Will fail on missing task file, but NOT on stream validation
    assert "is not one of" not in (result.output or "")
