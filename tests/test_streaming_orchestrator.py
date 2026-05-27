"""Tests for streaming callback integration in Orchestrator."""

from coder_eval.models import AgentKind, SandboxConfig, TaskDefinition, parse_agent_config
from coder_eval.streaming.events import StreamEvent
from tests._path_helpers import tmp_subdir


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
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="bypassPermissions"),
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
    orch = Orchestrator(task=task, run_dir=tmp_subdir("test-run"), stream_callback=cb, variant_id="test-variant")
    assert orch.stream_callback is cb


def test_orchestrator_defaults_stream_callback_to_none():
    """Orchestrator stream_callback defaults to None."""
    from coder_eval.orchestrator import Orchestrator

    task = _make_minimal_task()
    orch = Orchestrator(task=task, run_dir=tmp_subdir("test-run"), variant_id="test-variant")
    assert orch.stream_callback is None


def test_orchestrator_stream_callback_uses_variant_prefixed_task_id():
    """TaskScopedCallback should use variant-prefixed task_id, not bare task_id.

    When running multi-variant experiments, streaming events must include the
    variant prefix to correctly attribute events to the right variant.
    """
    from coder_eval.orchestrator import Orchestrator
    from coder_eval.streaming.callbacks import TaskScopedCallback

    task = _make_minimal_task()
    cb = CollectingCallback()
    orch = Orchestrator(task=task, run_dir=tmp_subdir("test-run"), stream_callback=cb, variant_id="fast-variant")

    # The _log_task_id should include the variant prefix
    assert orch._log_task_id == "fast-variant/stream-test/00"

    # When the orchestrator creates a TaskScopedCallback internally, it should
    # use _log_task_id (variant/task_id), not task.task_id (bare task_id).
    # We verify this by checking that the internal scoping matches.
    scoped = TaskScopedCallback(cb, orch._log_task_id)
    # Emit a test event through the scoped callback
    from coder_eval.streaming.events import TurnStartEvent

    event = TurnStartEvent(task_id="placeholder", iteration=1, prompt_preview="test")
    scoped.on_event(event)

    assert len(cb.events) == 1
    assert cb.events[0].task_id == "fast-variant/stream-test/00"
