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


def _fake_result_message():
    """Duck-typed SDK ResultMessage: session_id + usage (and no `content`,
    so it can't false-match the assistant/user branches of dispatch)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        session_id="sess-terminal",
        usage={"input_tokens": 10, "output_tokens": 5},
        model_usage=None,
        total_cost_usd=0.01,
        num_turns=3,
        subtype="success",
        stop_reason="end_turn",
        result="all done",
        is_error=False,
    )


@pytest.mark.asyncio
async def test_communicate_ends_turn_on_result_message_when_cli_lingers():
    """A CLI that emits its terminal ResultMessage but never exits must yield
    a clean TurnRecord — NOT run to the watchdog and raise TurnTimeoutError.

    Regression for the sweep-killing class: the agent finished (end_turn,
    subtype=success), the CLI process lingered, the stream never hit EOF, and
    the watchdog converted a successful run into a 0-score timeout ERROR.
    """
    agent = _make_agent()

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def mock_query(prompt, options, transport=None):
            yield _fake_result_message()
            # Simulate the lingering CLI: the stream never ends. Without the
            # break-on-result fix this sleeps until the watchdog fires.
            await asyncio.sleep(30)

        fake_transport = MagicMock()
        fake_transport._process.returncode = 0  # CLI already exited cleanly

        with (
            patch("coder_eval.agents.claude_code_agent.SubprocessCLITransport") as mock_transport_cls,
            patch("coder_eval.agents.claude_code_agent.query", mock_query),
        ):
            mock_transport_cls.return_value = fake_transport

            # Watchdog at 5s: with the fix communicate() returns immediately
            # after the ResultMessage; without it, TurnTimeoutError.
            record = await agent.communicate("prompt", timeout=5.0)

        assert record is not None
        assert record.result_summary is not None
        assert record.result_summary.subtype == "success"


@pytest.mark.asyncio
async def test_reap_after_result_kills_lingering_process():
    """_reap_transport_after_result must SIGKILL a process that outlives its
    grace period, and leave an already-exited process alone."""
    lingering = MagicMock()
    lingering._process.returncode = None
    lingering._process.pid = 4242
    await ClaudeCodeAgent._reap_transport_after_result(lingering, grace_seconds=0.3)
    lingering._process.kill.assert_called_once()

    exited = MagicMock()
    exited._process.returncode = 0
    await ClaudeCodeAgent._reap_transport_after_result(exited, grace_seconds=0.3)
    exited._process.kill.assert_not_called()

    # None transport / None process are no-ops
    await ClaudeCodeAgent._reap_transport_after_result(None)
    no_proc = MagicMock()
    no_proc._process = None
    await ClaudeCodeAgent._reap_transport_after_result(no_proc)
