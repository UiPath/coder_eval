"""Live integration tests for DelegateAgent.

Hit a real Delegate backend through a real Node subprocess; skipped by default,
only run with ``pytest -m live``. Needs Node + a real ``@uipath/delegate-sdk``
install, UiPath auth (``AUTH_TOKEN``/``TENANT_ID``/``ORG_ID`` or a saved login),
a backend (``DELEGATE_ENV``/``DELEGATE_BACKEND_URL``), and optionally
``DELEGATE_MODEL``.

Also the mechanism for empirically confirming this agent's ``# UNVERIFIED``
field-name guesses (see ``.claude/notes/agents.md`` § Delegate agent) against a
real payload — a failure here is the first place to check them.
"""

import os
import shutil
from pathlib import Path

import pytest

from coder_eval.agents.delegate_agent import DelegateAgent, _resolve_sdk_entry
from coder_eval.errors import AgentConfigError
from coder_eval.models import AgentKind, parse_agent_config


_live = pytest.mark.live


def _have_prerequisites() -> bool:
    if shutil.which("node") is None:
        return False
    try:
        _resolve_sdk_entry()
    except AgentConfigError:
        return False
    has_auth = bool(os.getenv("AUTH_TOKEN")) or (Path.home() / ".aria" / "sdk-auth.json").is_file()
    has_backend = bool(os.getenv("DELEGATE_ENV") or os.getenv("DELEGATE_BACKEND_URL"))
    return has_auth and has_backend


_skip_reason = "Live Delegate tests need Node + @uipath/delegate-sdk + UiPath auth + a backend"
pytestmark = [_live, pytest.mark.skipif(not _have_prerequisites(), reason=_skip_reason)]


def _make_agent() -> DelegateAgent:
    config = parse_agent_config(
        type=AgentKind.DELEGATE,
        model=os.getenv("DELEGATE_MODEL", "virtuoso-1-5"),
        enable_computer_use=False,
    )
    return DelegateAgent(config, task_id="delegate-live")


@_live
async def test_delegate_live_produces_text(tmp_path):
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
async def test_delegate_live_runs_shell_command_captured_as_telemetry(tmp_path):
    """A shell command shows up in TurnRecord.commands."""
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
    assert record.commands, "expected at least one command in telemetry"


@_live
async def test_delegate_live_edits_file_and_records_telemetry(tmp_path):
    """A file edit shows up on disk and the turn records command telemetry."""
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
async def test_delegate_live_two_turns_reuse_session(tmp_path):
    """A second communicate() call reuses the SDK session established by the first."""
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        first = await agent.communicate("Remember the number 42. Reply OK.", timeout=120)
        assert first.crashed is False
        second = await agent.communicate(
            "What number did I ask you to remember? Reply with just the number.", timeout=120
        )
    finally:
        await agent.stop()

    assert second.crashed is False
    assert "42" in second.agent_output


@_live
async def test_delegate_live_token_usage_populated(tmp_path):
    """Token usage is captured from the SDK and attached to the TurnRecord.

    If this fails while the turn otherwise completes, the `_parse_usage`
    UNVERIFIED bucket-name guesses in delegate_agent.py are the first thing
    to check against the real payload.
    """
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate("Say hello.", timeout=120)
    finally:
        await agent.stop()

    assert record.crashed is False
    assert record.token_usage is not None
    assert record.token_usage.output_tokens > 0
