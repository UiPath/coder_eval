"""Tests for ClaudeCodeAgent's timeout + hard-kill behavior.

The SDK uses anyio task groups internally that swallow cooperative
asyncio cancellation, so wrapping communicate() in asyncio.wait_for()
is not sufficient. The agent enforces the turn timeout itself via a
watchdog task that hard-kills the CLI subprocess.
"""

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
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

    with patch("coder_eval.agents.claude_code_agent.kill_process_tree") as kill_tree:
        await agent.kill()

    # The whole tree, not just the CLI: Bash tool calls are its children.
    kill_tree.assert_called_once_with(12345)


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
            patch("coder_eval.agents.claude_code_agent.kill_process_tree") as kill_tree,
        ):
            swapper = asyncio.create_task(swap_active_transport_before_watchdog())
            with pytest.raises(TurnTimeoutError):
                await agent.communicate("A", timeout=0.1)
            await swapper

        # The watchdog must have killed transport A (its captured target),
        # not transport B (which happened to be in self._active_transport
        # when the watchdog fired). Killed as a TREE -- the CLI's Bash children
        # must not outlive it and keep writing into the sandbox.
        killed_pids = [c.args[0] for c in kill_tree.call_args_list]
        assert killed_pids == [111]


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

    The task-timeout kill path: the turn never returns a record and the frame that
    held one unwinds, so the pending slot is the only place its spend can survive.
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
                await asyncio.wait_for(turn, timeout=5)

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


class TestKillProcessTree:
    """The hard kill must reap the agent's CHILDREN, not just the CLI process.

    The Claude CLI runs Bash tool calls as child processes, so killing only the
    CLI leaves a `pytest` / `npm run build` running -- and still writing into
    the sandbox that the forced-kill grading path is about to read to decide the
    task's verdict. A surviving writer makes that verdict a race (code review,
    2026-08-17).
    """

    @staticmethod
    def _alive(pid: int) -> bool:
        """Live AND not a zombie.

        `os.kill(pid, 0)` succeeds for a reaped-but-unwaited process in Z state.
        The killed grandchildren are reparented when their parent dies -- to init
        on a normal host (which waits them), but to the pytest process itself in a
        container where it is pid 1 or a subreaper, where nothing ever waits them.
        Checking the state field keeps this test honest in both.
        """
        import os

        try:
            os.kill(pid, 0)
        except OSError:
            return False
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            return True  # no /proc: fall back to the signal probe above
        close = stat.rfind(")")
        if close == -1:
            return True
        fields = stat[close + 2 :].split()
        return not fields or fields[0] != "Z"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-tree semantics")
    def test_kills_grandchildren_not_just_the_named_process(self):
        """A real three-deep tree: killing the top pid must take the leaf too."""
        import subprocess
        import time

        from coder_eval.agents._process_tree import _descendants, kill_process_tree

        # parent shell -> child shell -> sleep. Killing `parent` alone would
        # orphan the sleep, which is precisely the bug under test.
        parent = subprocess.Popen(["sh", "-c", "sh -c 'sleep 30' & wait"])
        try:
            deadline = time.monotonic() + 5.0
            descendants: list[int] = []
            while time.monotonic() < deadline and not descendants:
                descendants = _descendants(parent.pid)
                if not descendants:
                    time.sleep(0.05)
            assert descendants, "expected the helper to enumerate the spawned children"

            kill_process_tree(parent.pid)

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and any(self._alive(p) for p in descendants):
                time.sleep(0.05)
            for pid in descendants:
                assert not self._alive(pid), f"descendant {pid} survived the tree kill"
        finally:
            with contextlib.suppress(Exception):
                parent.kill()
            with contextlib.suppress(Exception):
                parent.wait(timeout=5)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
    def test_never_group_kills_the_evaluations_own_process_group(self, monkeypatch):
        """Guard rail against the textbook fix.

        The SDK spawns the CLI via anyio.open_process WITHOUT start_new_session,
        so the child shares this process's group -- a naive
        `os.killpg(os.getpgid(pid), SIGKILL)` would SIGKILL the whole evaluation.
        """
        import os as _os

        from coder_eval.agents import _process_tree as mod

        own_pgid = _os.getpgid(0)  # capture BEFORE patching, or the lambda recurses
        killpg_calls: list[int] = []
        monkeypatch.setattr(mod.os, "killpg", lambda pgid, sig: killpg_calls.append(pgid))
        monkeypatch.setattr(mod.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(mod, "_descendants", lambda pid: [])
        # Report the pid as sharing OUR group -- the real situation today.
        monkeypatch.setattr(mod.os, "getpgid", lambda pid: own_pgid)

        mod.kill_process_tree(424242)

        assert killpg_calls == [], "must never group-kill the evaluation's own group"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
    def test_never_group_kills_a_group_the_target_merely_belongs_to(self, monkeypatch):
        """`getpgid(pid) != getpgid(0)` is NOT sufficient -- it must LEAD the group.

        A pid sitting in some third party's group would otherwise have that whole
        group SIGKILLed, taking out processes this evaluation never spawned.
        """
        from coder_eval.agents import _process_tree as mod

        killpg_calls: list[int] = []
        individual_kills: list[int] = []
        monkeypatch.setattr(mod.os, "killpg", lambda pgid, sig: killpg_calls.append(pgid))
        monkeypatch.setattr(mod.os, "kill", lambda pid, sig: individual_kills.append(pid))
        monkeypatch.setattr(mod, "_descendants", lambda pid: [])
        # Someone else's group: not ours, but not led by the target either.
        monkeypatch.setattr(mod.os, "getpgid", lambda pid: 999999 if pid == 424242 else 555)

        mod.kill_process_tree(424242)

        assert killpg_calls == []
        assert 424242 in individual_kills

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
    def test_uses_one_group_kill_when_the_child_leads_its_own_group(self, monkeypatch):
        """If a spawn path ever does set start_new_session, take the cheap path."""
        from coder_eval.agents import _process_tree as mod

        killpg_calls: list[int] = []
        individual_kills: list[int] = []
        monkeypatch.setattr(mod.os, "killpg", lambda pgid, sig: killpg_calls.append(pgid))
        monkeypatch.setattr(mod.os, "kill", lambda pid, sig: individual_kills.append(pid))
        monkeypatch.setattr(mod.os, "getpgid", lambda pid: pid if pid == 424242 else 555)

        mod.kill_process_tree(424242)

        assert killpg_calls == [424242]
        assert individual_kills == []

    def test_ignores_sentinel_pids(self, monkeypatch):
        """pid 0 means 'everyone in my group' and 1 is init -- never signal them.

        Signals are patched out rather than really sent: on Linux both pid 1 and
        pid 2 have ppid 0, so if the guard ever regresses, a real
        `kill_process_tree(0)` would enumerate the entire process table and
        SIGKILL the CI runner / developer session. Asserting on the patched
        signals makes a regression fail the test instead of the host.
        """
        from coder_eval.agents import _process_tree as mod

        signalled: list[int] = []
        monkeypatch.setattr(mod.os, "kill", lambda pid, sig: signalled.append(pid))
        if hasattr(mod.os, "killpg"):
            monkeypatch.setattr(mod.os, "killpg", lambda pgid, sig: signalled.append(pgid))

        for sentinel in (0, 1, -1):
            assert mod.kill_process_tree(sentinel) == 0

        assert signalled == [], f"a sentinel pid was signalled: {signalled}"

    @pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux /proc enumeration")
    def test_enumerates_descendants_without_forking_ps(self, monkeypatch):
        """The /proc walk is the primary path -- `ps` is absent in the slim images.

        Neither docker/Dockerfile nor docker/Dockerfile.runtime installs procps,
        and in inject mode the kit lands on an arbitrary task base image. A
        ps-only implementation silently degraded to the pre-fix single-process
        kill in exactly the environment evals run in.
        """
        import subprocess
        import time

        from coder_eval.agents import _process_tree as mod

        def _no_ps(*a, **k):
            raise AssertionError("fell back to `ps` on a host that has /proc")

        monkeypatch.setattr(mod.subprocess, "run", _no_ps)

        parent = subprocess.Popen(["sh", "-c", "sh -c 'sleep 30' & wait"])
        try:
            deadline = time.monotonic() + 5.0
            descendants: list[int] = []
            while time.monotonic() < deadline and not descendants:
                descendants = mod._descendants(parent.pid)
                if not descendants:
                    time.sleep(0.05)
            assert descendants, "the /proc walk found no children"
        finally:
            with contextlib.suppress(Exception):
                parent.kill()
            with contextlib.suppress(Exception):
                parent.wait(timeout=5)

    async def test_async_kill_does_not_block_the_event_loop(self):
        """`kill()` must offload, or the quiesce's asyncio.wait_for cannot bound it.

        With no suspension point in kill()'s body a slow tree reap froze the loop
        -- so `wait_for(agent.kill(), N)` could not time out, and in a parallel
        run_batch every other in-flight task stalled with it.
        """
        import time

        agent = _make_agent()
        fake_proc = MagicMock()
        fake_proc.returncode = None
        fake_proc.pid = 4242
        fake_transport = MagicMock()
        fake_transport._process = fake_proc
        agent._active_transport = fake_transport

        ticks = 0

        async def tick():
            nonlocal ticks
            for _ in range(50):
                ticks += 1
                await asyncio.sleep(0.005)

        def slow_kill(pid):
            time.sleep(0.2)  # blocking, like a real /proc walk or `ps` fork

        ticker = asyncio.create_task(tick())
        with patch("coder_eval.agents.claude_code_agent.kill_process_tree", slow_kill):
            await agent.kill()
        loop_ran_during_kill = ticks
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker

        assert loop_ran_during_kill > 1, "the event loop was blocked for the whole kill"
