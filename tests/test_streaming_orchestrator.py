"""Tests for streaming callback integration in Orchestrator."""

from pathlib import Path

from coder_eval.models import AgentConfig, AgentKind, SandboxConfig, TaskDefinition
from coder_eval.streaming.events import StreamEvent


class CollectingCallback:
    """Collects streaming events for assertion."""

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)


def _make_minimal_task() -> TaskDefinition:
    """Create a minimal TaskDefinition for testing."""
    return TaskDefinition(
        task_id="stream-test",
        description="test task",
        initial_prompt="do something",
        agent=AgentConfig(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions"),
        sandbox=SandboxConfig(),
        success_criteria=[
            {"type": "file_exists", "description": "test file exists", "path": "test.txt"},
        ],
    )


def test_orchestrator_accepts_stream_callback():
    """Orchestrator __init__ accepts stream_callback parameter."""
    from coder_eval.orchestrator import Orchestrator

    task = _make_minimal_task()
    cb = CollectingCallback()
    orch = Orchestrator(task=task, run_dir=Path("/tmp/test-run"), stream_callback=cb)
    assert orch.stream_callback is cb


def test_orchestrator_defaults_stream_callback_to_none():
    """Orchestrator stream_callback defaults to None."""
    from coder_eval.orchestrator import Orchestrator

    task = _make_minimal_task()
    orch = Orchestrator(task=task, run_dir=Path("/tmp/test-run"))
    assert orch.stream_callback is None
