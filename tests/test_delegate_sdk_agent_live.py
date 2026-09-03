"""Live integration tests for DelegateSdkAgent.

These drive the *real* Delegate SDK: the agent spawns the published
``@uipath/delegate-stdio`` Node bundle as a subprocess, which talks
to a UiPath Autopilot backend with real auth and a real model. They are skipped
by default and only run with: ``pytest -m live``.

Requirements (all must be satisfied or the tests skip cleanly):
  - The host package installed and discoverable: ``DELEGATE_STDIO_PATH`` →
    the bundle, ``DELEGATE_STDIO_NODE_MODULES`` holding
    ``node_modules/@uipath/delegate-stdio``, or an install in the cwd
    or any ancestor (auto-located by walking up, the way Node resolves modules)
    (``npm install @uipath/delegate-stdio``).
  - ``node`` on PATH.
  - Auth: either ``AUTH_TOKEN`` in the environment, or a prior
    ``delegate-cli login`` that wrote ``~/.aria/sdk-auth.json``.
  - Optional: ``DELEGATE_SDK_ENV`` (cloud slug; defaults to ``alpha``),
    ``BACKEND_URL`` / ``INTEROP_URL`` to point at a specific backend, and
    ``DELEGATE_SDK_MODEL`` to pin the model (defaults to ``virtuoso-1-5`` — the
    SDK default; the alpha backend rejects the ``claude-sonnet-4-6`` inherited
    from ``experiments/default.yaml``).

Purpose: catch regressions in the Delegate SDK integration by exercising the
three host message paths a turn depends on — assistant ``message`` events
(``agent_output`` assembly), ``tool_call`` events (``CommandTelemetry``
parsing), and the final ``result`` message (token-usage forwarding).

NOTE: the Delegate agent reasons in the UiPath backend but executes its tools
(``WriteFile`` / ``ExecutePowershellCommand``) locally — the Node host ``chdir``s
into the task's sandbox working directory, so produced files land on local disk.
These bare-agent tests don't set up a sandbox working directory, so they
deliberately assert on the *turn record* (text, telemetry, usage) rather than on
local-disk effects; file effects are covered by the ``tasks/*_delegate_sdk*.yaml``
end-to-end tasks.
"""

import os
import shutil
from pathlib import Path

import pytest

from coder_eval.agents.delegate_sdk_agent import DelegateSdkAgent, _resolve_stdio_bundle
from coder_eval.errors import AgentConfigError
from coder_eval.models import DelegateSdkAgentConfig


_live = pytest.mark.live

_DEFAULT_MODEL = "virtuoso-1-5"


def _have_prereqs() -> tuple[bool, str]:
    """Return (ok, reason). Mirrors the agent's own start() preconditions."""
    try:
        _resolve_stdio_bundle()
    except AgentConfigError as exc:
        return False, str(exc)
    if shutil.which("node") is None:
        return False, "node not on PATH"
    if not os.getenv("AUTH_TOKEN") and not (Path.home() / ".aria" / "sdk-auth.json").is_file():
        return False, "no auth (AUTH_TOKEN unset and ~/.aria/sdk-auth.json absent)"
    return True, ""


_ok, _reason = _have_prereqs()
pytestmark = [_live, pytest.mark.skipif(not _ok, reason=f"Live Delegate SDK tests need: {_reason}")]


def _make_agent() -> DelegateSdkAgent:
    config = DelegateSdkAgentConfig(
        type="delegate-sdk",
        permission_mode="acceptEdits",
        model=os.getenv("DELEGATE_SDK_MODEL", _DEFAULT_MODEL),
    )
    return DelegateSdkAgent(config)


@_live
async def test_delegate_live_produces_text(tmp_path):
    """A trivial prompt returns assistant text and a completed TurnRecord."""
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate(
            "Reply with exactly the word PONG and nothing else.",
            timeout=180,
        )
    finally:
        await agent.stop()

    assert record.crashed is False
    assert record.agent_output.strip()
    assert "pong" in record.agent_output.lower()


@_live
async def test_delegate_live_captures_tool_call_telemetry(tmp_path):
    """A prompt that requires a tool produces CommandTelemetry on the turn.

    Asserts on the turn record's ``commands`` (the ``tool_call`` event →
    ``CommandTelemetry`` parsing path), not on local-disk effects: the agent's
    tools run in a remote sandbox (see module docstring).
    """
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate(
            "Run a shell command that prints the text coder-eval-live, then report its output.",
            timeout=180,
        )
    finally:
        await agent.stop()

    assert record.crashed is False
    assert record.commands, "expected at least one tool call captured as telemetry"
    first = record.commands[0]
    assert first.tool_name, "telemetry entry is missing a tool_name"
    assert isinstance(first.sequence_number, int)


@_live
async def test_delegate_live_token_usage_populated(tmp_path):
    """Token usage is forwarded from the host result message and attached."""
    agent = _make_agent()
    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate("Say hello.", timeout=180)
    finally:
        await agent.stop()

    assert record.token_usage is not None
    assert record.token_usage.input_tokens > 0
