"""One tiny real turn per installed harness, through the ``communicate`` contract.

Skipped by default; runs only with ``pytest -m live``. Each case skips when its
harness is not installed or its credential is absent.
"""

import importlib.util
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from coder_eval.agents.registry import create_agent
from coder_eval.config import Settings
from coder_eval.models import (
    AgentKind,
    ApiBackend,
    ApiRoute,
    AssistantMessage,
    DirectRoute,
    TurnRecord,
    parse_agent_config,
    resolve_route,
)
from coder_eval.plugins import ensure_plugins_loaded
from coder_eval.streaming.events import AgentEndStatus, StreamEvent
from coder_eval.testing import assert_stream_balanced


pytestmark = pytest.mark.live

PROMPT = "Create a file named ok.txt containing ok, then reply DONE."
OPENROUTER_HAIKU = "openrouter/anthropic/claude-haiku-4.5"


@dataclass(frozen=True)
class Harness:
    kind: AgentKind
    model: str | None
    cli: str | None = None
    module: str | None = None
    credential: str | None = None
    # A mode the harness contract documents; None leaves an unsupported field unset.
    permission_mode: str | None = "bypassPermissions"


HARNESSES = [
    Harness(AgentKind.CLAUDE_CODE, "claude-haiku-4-5-20251001", cli="claude"),
    Harness(
        AgentKind.CODEX,
        os.getenv("CODEX_MODEL"),
        module="openai_codex",
        credential="CODEX_API_KEY",
        permission_mode=None,
    ),
    Harness(AgentKind.PI, OPENROUTER_HAIKU, cli="pi", credential="OPENROUTER_API_KEY"),
    Harness(AgentKind.OPENCODE, OPENROUTER_HAIKU, cli="opencode", credential="OPENROUTER_API_KEY"),
    Harness(AgentKind.ANTIGRAVITY, "gemini-3.5-flash-lite", module="google.antigravity", credential="GEMINI_API_KEY"),
]


@dataclass
class Recorder:
    events: list[StreamEvent] = field(default_factory=list)

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def _skip_unless_installed(harness: Harness) -> None:
    if harness.cli is not None and shutil.which(harness.cli) is None:
        pytest.skip(f"the '{harness.cli}' CLI is not on PATH")
    if harness.module is not None and not _importable(harness.module):
        pytest.skip(f"'{harness.module}' is not importable")
    if harness.credential is not None and not os.getenv(harness.credential):
        pytest.skip(f"{harness.credential} is not set")


def _claude_route(model: str | None) -> tuple[ApiRoute, str | None]:
    """The configured backend's route, as the orchestrator builds it; a Bedrock route keeps its own model."""
    settings = Settings()
    if settings.api_backend == ApiBackend.DIRECT:
        return DirectRoute(), model
    return resolve_route(settings), None if settings.api_backend == ApiBackend.BEDROCK else model


def _bucket_sum_ms(record: TurnRecord) -> float:
    generation_ms = sum(
        m.generation_duration_ms or 0.0
        for m in record.messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None
    )
    return (
        (record.harness_startup_ms or 0.0)
        + generation_ms
        + (record.tool_union_ms or 0.0)
        + (record.harness_teardown_ms or 0.0)
    )


@pytest.mark.parametrize("harness", HARNESSES, ids=[str(h.kind) for h in HARNESSES])
async def test_harness_completes_a_tiny_turn(harness: Harness, tmp_path: Path) -> None:
    _skip_unless_installed(harness)
    ensure_plugins_loaded()
    route: ApiRoute | None = None
    model = harness.model
    if harness.kind is AgentKind.CLAUDE_CODE:
        route, model = _claude_route(model)
    fields = {"model": model} | ({"permission_mode": harness.permission_mode} if harness.permission_mode else {})
    config = parse_agent_config(type=harness.kind, **fields)
    agent = create_agent(harness.kind, config, route)
    recorder = Recorder()
    try:
        await agent.start(str(tmp_path))
        outcome = await agent.communicate(PROMPT, iteration=1, timeout=180, stream_callback=recorder)
    finally:
        await agent.stop()

    record = outcome.record
    assert outcome.status is AgentEndStatus.COMPLETED, outcome.error
    assert (tmp_path / "ok.txt").exists(), "the agent did not create ok.txt"
    assert record.commands, "expected at least one command"
    assert PROMPT not in record.agent_output
    assert_stream_balanced(recorder.events)
    assert _bucket_sum_ms(record) <= record.duration_seconds * 1000 + 1000
