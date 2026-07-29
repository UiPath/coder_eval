"""Tests for ClaudeCodeAgent's timeout + hard-kill behavior.

The SDK uses anyio task groups internally that swallow cooperative
asyncio cancellation, so wrapping communicate() in asyncio.wait_for()
is not sufficient. The agent enforces the turn timeout itself via a
watchdog task that hard-kills the CLI subprocess.
"""

import asyncio
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import ProcessError

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.errors.timeout import TurnTimeoutError
from coder_eval.models import AgentKind, parse_agent_config


def _make_agent() -> ClaudeCodeAgent:
    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    return ClaudeCodeAgent(config)


@pytest.mark.asyncio
async def test_kill_is_noop_when_no_active_transport():
    """kill() must be safe to call when no subprocess is running."""
    agent = _make_agent()
    await agent.kill()  # should not raise
    assert agent._active_transport is None


@pytest.mark.asyncio
async def test_kill_terminates_active_subprocess():
    """kill() sends SIGKILL to the transport's subprocess when one is live."""
    agent = _make_agent()

    fake_proc = MagicMock()
    fake_proc.returncode = None
    fake_proc.pid = 12345
    fake_transport = MagicMock()
    fake_transport._process = fake_proc
    agent._active_transport = fake_transport

    await agent.kill()

    fake_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_kill_skips_already_exited_process():
    """kill() must not signal a process that has already exited."""
    agent = _make_agent()

    fake_proc = MagicMock()
    fake_proc.returncode = 0  # already exited
    fake_transport = MagicMock()
    fake_transport._process = fake_proc
    agent._active_transport = fake_transport

    await agent.kill()

    fake_proc.kill.assert_not_called()


@pytest.mark.asyncio
async def test_communicate_watchdog_raises_turn_timeout():
    """When the watchdog fires, the agent must kill the subprocess and raise TurnTimeoutError.

    The mock query() sleeps past the watchdog deadline then returns cleanly.
    The watchdog flips `timeout_hit` and calls `_kill_transport(target)`
    (closure-captured, so we patch the static method to observe it).
    """
    agent = _make_agent()

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def mock_query(prompt, options, transport=None):
            # Sleep past the 100ms watchdog deadline, then return cleanly.
            # The post-finally timeout check is what raises TurnTimeoutError.
            await asyncio.sleep(0.2)
            if False:  # pragma: no cover - keep this an async generator
                yield None

        with (
            patch("coder_eval.agents.claude_code_agent.ClaudeCodeAgent._kill_transport") as mock_kill_transport,
            patch("coder_eval.agents.claude_code_agent.SubprocessCLITransport") as mock_transport_cls,
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
        ):
            mock_transport_cls.return_value = MagicMock()

            with pytest.raises(TurnTimeoutError) as exc:
                # 100ms watchdog — bypasses AgentConfig's ge=10 validator
                # because we pass it directly as a call arg, not via config.
                await agent.communicate("prompt", timeout=0.1)

        assert exc.value.timeout_seconds == 0.1
        assert exc.value.layer == "turn"
        mock_kill_transport.assert_called()


@pytest.mark.asyncio
async def test_process_error_after_watchdog_kill_raises_turn_timeout():
    """Regression for C1: when SIGKILL causes the SDK to raise ProcessError,
    we must surface TurnTimeoutError (non-retryable), not the generic
    `CLI process failed` RuntimeError (which is AGENT_CRASH and retryable).
    """
    agent = _make_agent()

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def mock_query(prompt, options, transport=None):
            # Sleep past the watchdog deadline (0.1s), then raise ProcessError
            # — what the SDK does when the child exits with a non-zero code.
            # By the time we raise, timeout_hit has already been flipped.
            await asyncio.sleep(0.2)
            raise ProcessError("Command failed", exit_code=-9, stderr="")
            if False:  # pragma: no cover - keep generator shape
                yield None

        with (
            patch("coder_eval.agents.claude_code_agent.SubprocessCLITransport") as mock_transport_cls,
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
        ):
            mock_transport_cls.return_value = MagicMock()

            with pytest.raises(TurnTimeoutError):
                await agent.communicate("prompt", timeout=0.1)


@pytest.mark.asyncio
async def test_watchdog_kills_own_transport_not_current_active():
    """Regression for C2: the watchdog must target the transport it was
    started for, even if self._active_transport has been reassigned to a
    different turn's transport by the time the watchdog fires.

    Setup: start a communicate() call with transport A and a short timeout.
    Before the watchdog fires, swap self._active_transport to transport B
    (simulating a later turn). When the watchdog fires it must kill A's
    process, NOT B's.
    """
    agent = _make_agent()

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        transport_a = MagicMock()
        proc_a = MagicMock()
        proc_a.returncode = None
        proc_a.pid = 111
        transport_a._process = proc_a

        transport_b = MagicMock()
        proc_b = MagicMock()
        proc_b.returncode = None
        proc_b.pid = 222
        transport_b._process = proc_b

        async def mock_query(prompt, options, transport=None):
            # Block long enough for the watchdog to fire and then for the
            # ProcessError / clean exit to happen.
            await asyncio.sleep(1.0)
            if False:  # pragma: no cover - async generator shape
                yield None

        async def swap_active_transport_before_watchdog() -> None:
            # Let communicate() install transport_a first, then overwrite
            # self._active_transport to simulate a later turn taking over.
            await asyncio.sleep(0.02)
            assert agent._active_transport is transport_a
            agent._active_transport = transport_b

        with (
            patch(
                "coder_eval.agents.claude_code_agent.SubprocessCLITransport",
                side_effect=[transport_a, transport_b],
            ),
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
        ):
            swapper = asyncio.create_task(swap_active_transport_before_watchdog())
            with pytest.raises(TurnTimeoutError):
                await agent.communicate("A", timeout=0.1)
            await swapper

        # The watchdog must have killed transport A (its captured target),
        # not transport B (which happened to be in self._active_transport
        # when the watchdog fired).
        proc_a.kill.assert_called_once()
        proc_b.kill.assert_not_called()


@pytest.mark.asyncio
async def test_successful_turn_past_deadline_is_not_misclassified():
    """Regression for Gemini H1 (review #2): the post-finally timeout check
    must NOT raise TurnTimeoutError when the loop exited cleanly but the
    wall clock crossed the deadline during post-loop processing (e.g.,
    finalize commands, file tree capture).

    Only the watchdog firing or the in-loop wall-clock guard signals a real
    timeout — the post-finally wall-clock check was a false positive.
    """
    agent = _make_agent()

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def mock_query(prompt, options, transport=None):
            # Yield one assistant-ish message so commands/turn record can build
            if False:  # pragma: no cover - async generator shape
                yield None

        # Patch time.monotonic so the first call (turn_start_time) returns a
        # small value and all subsequent calls return a huge value — past the
        # deadline. This reproduces the case where the loop exited cleanly
        # but post-loop work took us past the deadline.
        monotonic_values = [1000.0]

        def fake_monotonic() -> float:
            if len(monotonic_values) < 2:
                monotonic_values.append(9999.0)
                return 1000.0
            return 9999.0

        with (
            patch("coder_eval.agents.claude_code_agent.SubprocessCLITransport") as mock_transport_cls,
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
            patch("coder_eval.agents.claude_code_agent.time.monotonic", fake_monotonic),
        ):
            mock_transport_cls.return_value = MagicMock()
            # Must return a TurnRecord, not raise TurnTimeoutError, even
            # though wall-clock is way past deadline.
            result = await agent.communicate("prompt", timeout=1.0)
            assert result is not None


@pytest.mark.asyncio
async def test_communicate_without_timeout_does_not_construct_transport():
    """When timeout is None, the agent must not construct SubprocessCLITransport.

    This preserves test fixtures that mock only query() and do not have a
    claude CLI installed on PATH.
    """
    agent = _make_agent()

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def mock_query(prompt, options):
            if False:  # pragma: no cover
                yield None

        with (
            patch("coder_eval.agents.claude_code_agent.SubprocessCLITransport") as mock_transport_cls,
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
        ):
            await agent.communicate("prompt")  # no timeout
            mock_transport_cls.assert_not_called()


class _MockTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _MockAssistantMessage:
    """Duck-typed SDK AssistantMessage carrying one text block and a usage dict."""

    def __init__(self, text: str, usage: dict[str, int]) -> None:
        self.content = [_MockTextBlock(text)]
        self.model = "claude-sonnet-5"
        self.usage = usage
        self.stop_reason = "end_turn"
        self.message_id = "msg_partial_1"


@pytest.mark.asyncio
async def test_external_cancel_parks_the_turn_it_interrupted():
    """A turn cancelled from outside must leave its telemetry on ``pending_turn``.

    This is the task-level timeout's kill path: the watchdog kills the agent and
    cancels the task awaiting ``communicate()``, so the turn never returns a record
    and the frame that held one unwinds. Whatever it spent before the kill is real
    and billed, so the pending slot is the only place it can survive.
    """
    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits", model="claude-sonnet-5")
    agent = ClaudeCodeAgent(config)

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        streaming = asyncio.Event()

        async def mock_query(prompt, options):
            yield _MockAssistantMessage("working on it", {"input_tokens": 40_000, "output_tokens": 2_000})
            streaming.set()
            # Still mid-turn when the cancel lands, as on a real hard kill: no
            # terminal ResultMessage is ever delivered.
            await asyncio.sleep(30)

        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            turn = asyncio.create_task(agent.communicate("prompt"))
            await streaming.wait()
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn

    partial = agent.pending_turn
    assert partial is not None, "the interrupted turn's record was discarded"
    assert partial.crashed is True
    usage = partial.token_usage
    assert usage is not None
    assert usage.output_tokens == 2_000
    # No ResultMessage means the backend never priced this turn, so the cost is
    # backfilled from the buckets at the model's rate.
    assert usage.total_cost_usd is not None
    assert usage.total_cost_usd > 0
