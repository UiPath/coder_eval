"""Live integration tests for CodexAgent.

These hit the real Codex SDK (which spawns a Codex app-server subprocess) and a
real model, so they are skipped by default and only run with: ``pytest -m live``.

Requirements:
  - The ``[codex]`` extra installed (``uv sync --extra codex``).
  - ``CODEX_API_KEY`` in the environment.
  - ``CODEX_BASE_URL`` to route to a custom OpenAI-/responses-compatible endpoint,
    and ``CODEX_MODEL`` to pin the model/deployment (required by that endpoint).

Purpose: catch regressions in the Codex integration on every PR — that a turn
completes, produces assistant text, runs a shell command captured as telemetry,
and edits a file captured as telemetry.
"""

import os

import pytest


pytest.importorskip("openai_codex")

from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.models import AgentConfig, AgentKind


_live = pytest.mark.live


def _have_api_key() -> bool:
    return bool(os.getenv("CODEX_API_KEY"))


_skip_reason = "Live Codex tests need [codex] extra + CODEX_API_KEY"
pytestmark = [_live, pytest.mark.skipif(not _have_api_key(), reason=_skip_reason)]


def _make_agent() -> CodexAgent:
    config = AgentConfig(
        type=AgentKind.CODEX,
        permission_mode="bypassPermissions",  # full access so it can run shell + write files
        model=os.getenv("CODEX_MODEL"),
    )
    return CodexAgent(config, instance_name="codex-live")


@_live
async def test_codex_live_produces_text(tmp_path):
    """A trivial prompt returns assistant text and a completed TurnRecord."""
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate(
            "Reply with exactly the word PONG and nothing else.",
            timeout=120,
        )
    finally:
        await agent.stop()

    assert record.crashed is False
    assert record.agent_output.strip()
    assert "pong" in record.agent_output.lower()


@_live
async def test_codex_live_runs_shell_command_captured_as_telemetry(tmp_path):
    """A shell command shows up in TurnRecord.commands as Bash telemetry."""
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate(
            "Run the shell command `echo coder-eval-live` and report its output.",
            timeout=120,
        )
    finally:
        await agent.stop()

    assert record.crashed is False
    bash_cmds = [c for c in record.commands if c.tool_name == "Bash"]
    assert bash_cmds, "expected at least one shell command in telemetry"


@_live
async def test_codex_live_edits_file_and_records_telemetry(tmp_path):
    """A file edit shows up on disk and the turn records command telemetry.

    Codex may write files either via an apply_patch ``fileChange`` (Write
    telemetry) or via a shell command like ``printf > file`` (Bash telemetry),
    so we assert the file exists and that *some* command telemetry was captured
    rather than pinning the tool name.
    """
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate(
            "Create a file named hello.txt in the current directory containing the text 'hi'.",
            timeout=120,
        )
    finally:
        await agent.stop()

    assert record.crashed is False
    assert (tmp_path / "hello.txt").exists(), "agent did not create the file"
    assert record.commands, "expected command telemetry for the file creation"


@_live
async def test_codex_live_token_usage_populated(tmp_path):
    """Token usage is captured from the SDK and attached to the TurnRecord."""
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate("Say hello.", timeout=120)
    finally:
        await agent.stop()

    assert record.token_usage is not None
    assert record.token_usage.input_tokens > 0
