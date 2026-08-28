"""Tests for the anti-cheat permission window around agent turns.

The agent under evaluation shares a filesystem with the harness, so without an
active control it can read the reference solution instead of solving the task.
``fs_permissions.set_permissions`` chmods the staged REFERENCE directory to 000
for the duration of every ``agent.communicate`` call. The task directory is
deliberately NOT shielded — under docker it is a ``:ro`` mount (chmod -> EROFS)
and the same YAML is readable at ``/work/input`` regardless, so shielding it was
ineffective twice over. ``test_task_dir_is_not_shielded`` pins that.
"""

import asyncio
import contextlib
import os
import signal
import stat
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from coder_eval.fs_permissions import (
    READ_ONLY_MODE,
    RESTRICTED_MODE,
    _PermissionStack,
    set_permissions,
)
from coder_eval.models import CONTAINER_REFERENCE_DIR
from coder_eval.path_utils import digest_tree


# These tests drive `chmod` against the HOST filesystem. Windows `chmod` honours
# only the read-only bit, so mode 000 never takes and every assertion reads back
# 0o555/0o777.
#
# This is NOT a coverage gap for Windows users. The window is enforced only when
# CODER_EVAL_IN_CONTAINER=1, which only DockerRunner sets — and Docker Desktop on
# Windows runs LINUX containers (WSL2), so the in-container orchestrator that
# performs the chmod is on Linux and behaves exactly as these tests assert. A
# Windows host only ever sees the window under `driver: tempdir`, where it is a
# deliberate no-op regardless of platform. So there is nothing here for a Windows
# runner to exercise — the real behaviour is covered by the Linux CI jobs and by
# `tasks/anti_cheat_reference`, which runs in the container.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="host-side POSIX mode semantics; the window runs in a Linux container on every host OS",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _skip_if_root() -> None:
    """Root bypasses DAC entirely, so a denial assertion cannot hold for it.

    `os.geteuid` is POSIX-only; guarded so this stays importable everywhere even
    though the module as a whole is skipped off POSIX.
    """
    if getattr(os, "geteuid", lambda: 1)() == 0:
        pytest.skip("root bypasses DAC; the docker path drops DAC_* caps instead")


@pytest.fixture
def guarded_dir(tmp_path):
    """A directory with a file in it, guaranteed restored even if a test fails."""
    d = tmp_path / "secret"
    d.mkdir()
    (d / "answer.txt").write_text("SOLUTION=42", encoding="utf-8")
    original = _mode(d)
    yield d
    os.chmod(d, original)


class TestRestrictPermissions:
    async def test_restricts_then_restores(self, guarded_dir):
        original = _mode(guarded_dir)

        async with set_permissions([guarded_dir]):
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_contents_unreadable_inside_the_window(self, guarded_dir):
        """The point of the exercise: the solution is not readable during a turn."""
        _skip_if_root()

        async with set_permissions([guarded_dir]):
            with pytest.raises(PermissionError):
                (guarded_dir / "answer.txt").read_text(encoding="utf-8")

        assert (guarded_dir / "answer.txt").read_text(encoding="utf-8") == "SOLUTION=42"

    async def test_restores_on_exception(self, guarded_dir):
        """An agent crash or turn timeout must not leave the tree unreadable."""
        original = _mode(guarded_dir)

        # Explicit try/except rather than `pytest.raises`: CodeQL cannot model
        # pytest.raises as catching, so it reports everything after such a block
        # as unreachable (alerts 77/80 on the first revision of this file).
        raised = False
        try:
            async with set_permissions([guarded_dir]):
                raise RuntimeError("agent crashed")
        except RuntimeError:
            raised = True
        assert raised, "the exception must propagate, not be swallowed by the CM"
        assert _mode(guarded_dir) == original

    async def test_restores_on_cancellation(self, guarded_dir):
        """The task_timeout watchdog cancels the orchestrator task mid-turn."""
        original = _mode(guarded_dir)
        entered = asyncio.Event()

        async def _body():
            async with set_permissions([guarded_dir]):
                entered.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(_body())
        await entered.wait()
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled, "the task must actually be cancelled"
        assert _mode(guarded_dir) == original

    async def test_preserves_a_non_default_original_mode(self, guarded_dir):
        """Restore the mode we observed, not a hardcoded 0o755.

        0o700 rather than a group-readable mode: it makes the same point (the
        pre-window mode is captured, not assumed) without tripping the
        overly-permissive-chmod scanner.
        """
        os.chmod(guarded_dir, 0o700)

        async with set_permissions([guarded_dir]):
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == 0o700

    async def test_none_entries_and_duplicates_are_tolerated(self, guarded_dir):
        """Callers pass [task_dir, reference_dir] without pre-filtering."""
        original = _mode(guarded_dir)

        async with set_permissions([None, guarded_dir, guarded_dir, None]):
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_missing_path_is_a_no_op(self, tmp_path):
        async with set_permissions([tmp_path / "does_not_exist"]):
            pass  # must not raise

    async def test_chmod_refusal_warns_and_skips_the_matching_pop(self, guarded_dir, monkeypatch, caplog):
        """The branch whose entire job is to signal "this run is NOT protected".

        A read-only mount or a foreign owner makes chmod fail; the window then
        does not apply and the run continues unprotected, so the warning is the
        only signal an operator gets. Exiting must not chmod either — a pop with
        no matching push would apply a mode nobody asked for.
        """
        original = _mode(guarded_dir)
        calls: list[tuple[str, int]] = []

        def _refuse(path, mode):
            calls.append((str(path), mode))
            raise PermissionError(30, "Read-only file system")

        monkeypatch.setattr("coder_eval.fs_permissions.os.chmod", _refuse)
        with caplog.at_level("WARNING"):
            async with set_permissions([guarded_dir]):
                pass

        assert "could not chmod" in caplog.text
        assert len(calls) == 1, "a refused push must not be followed by a pop"
        monkeypatch.undo()
        assert _mode(guarded_dir) == original

    async def test_unresolvable_path_is_warned_not_silently_dropped(self, tmp_path, monkeypatch, caplog):
        def _boom(self, *a, **k):
            raise OSError("nope")

        monkeypatch.setattr("coder_eval.fs_permissions.Path.resolve", _boom)
        with caplog.at_level("WARNING"):
            async with set_permissions([tmp_path]):
                pass
        assert "could not resolve" in caplog.text

    async def test_overlapping_windows_of_the_same_mode(self, guarded_dir):
        """Two windows applying the same mode: the inner exit must NOT restore.

        This is what a refcount used to buy; the stack gives it for free, because
        the inner pop re-applies the outer's (identical) mode.
        """
        original = _mode(guarded_dir)

        async with set_permissions([guarded_dir]):
            async with set_permissions([guarded_dir]):
                assert _mode(guarded_dir) == RESTRICTED_MODE
            # Inner exited; outer still holds it.
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_nested_regrant_falls_back_to_the_enclosing_mode(self, guarded_dir):
        """The whole reason this is a stack and not a refcount.

        Code that runs INSIDE the turn but is not the agent (a future live
        criterion, say) opens a read-only window over a shielded path. Exiting it
        must return to the enclosing 000 — not to the pre-window mode, which would
        silently un-shield the reference for the rest of the agent's turn.
        """
        original = _mode(guarded_dir)

        async with set_permissions([guarded_dir], mode=RESTRICTED_MODE):
            assert _mode(guarded_dir) == RESTRICTED_MODE

            async with set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                assert _mode(guarded_dir) == READ_ONLY_MODE

            # Back to the agent-facing window, NOT to `original`.
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_nested_regrant_actually_permits_reads(self, guarded_dir):
        """The mode is not just bookkeeping — the re-grant really opens the file."""
        _skip_if_root()

        async with set_permissions([guarded_dir], mode=RESTRICTED_MODE):
            with pytest.raises(PermissionError):
                (guarded_dir / "answer.txt").read_text(encoding="utf-8")

            async with set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                assert (guarded_dir / "answer.txt").read_text(encoding="utf-8") == "SOLUTION=42"

            # Re-shielded for the remainder of the turn.
            with pytest.raises(PermissionError):
                (guarded_dir / "answer.txt").read_text(encoding="utf-8")

    async def test_regrant_unwinds_on_exception(self, guarded_dir):
        """A raise inside the re-grant must not leave the path readable."""
        async with set_permissions([guarded_dir], mode=RESTRICTED_MODE):
            raised = False
            try:
                async with set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                    raise RuntimeError("live check blew up")
            except RuntimeError:
                raised = True
            assert raised
            assert _mode(guarded_dir) == RESTRICTED_MODE

    async def test_three_deep_stack_unwinds_in_order(self, guarded_dir):
        original = _mode(guarded_dir)

        async with set_permissions([guarded_dir], mode=0o700):
            async with set_permissions([guarded_dir], mode=RESTRICTED_MODE):
                async with set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                    assert _mode(guarded_dir) == READ_ONLY_MODE
                assert _mode(guarded_dir) == RESTRICTED_MODE
            assert _mode(guarded_dir) == 0o700

        assert _mode(guarded_dir) == original

    async def test_concurrent_holders_restore_exactly_once(self, guarded_dir):
        """Same invariant, driven concurrently rather than lexically nested."""
        original = _mode(guarded_dir)
        release = asyncio.Event()

        async def _hold():
            async with set_permissions([guarded_dir]):
                await release.wait()

        holders = [asyncio.create_task(_hold()) for _ in range(5)]
        await asyncio.sleep(0.05)
        assert _mode(guarded_dir) == RESTRICTED_MODE

        release.set()
        await asyncio.gather(*holders)
        assert _mode(guarded_dir) == original

    async def test_out_of_order_release_of_differing_modes(self, guarded_dir):
        """Pins the documented single-holder assumption.

        `pop()` discards `applied[-1]` regardless of which window is exiting, so
        two OVERLAPPING (not nested) windows at different modes released in push
        order do not restore per-holder. The orchestrator never does this — the
        window is container-only and one task per container — but the primitive
        is public, so the behaviour is pinned rather than left to chance.
        """
        original = _mode(guarded_dir)

        outer = set_permissions([guarded_dir], mode=RESTRICTED_MODE)
        inner = set_permissions([guarded_dir], mode=READ_ONLY_MODE)
        await outer.__aenter__()
        await inner.__aenter__()
        assert _mode(guarded_dir) == READ_ONLY_MODE

        # Release in PUSH order (not LIFO): the stack pops the top entry.
        await outer.__aexit__(None, None, None)
        assert _mode(guarded_dir) == RESTRICTED_MODE
        await inner.__aexit__(None, None, None)

        assert _mode(guarded_dir) == original

    async def test_crash_handlers_install_from_the_event_loop_thread(self, guarded_dir, monkeypatch):
        """The advertised crash-safety property: a killed run must not leave the
        tree at mode 000.

        Asserted through the PUBLIC ``set_permissions`` entry point, not by
        calling ``push`` directly. That distinction is the whole bug this test
        exists for: installation used to happen inside ``push``, which only ever
        runs on an ``asyncio.to_thread`` worker, where ``signal.signal`` raises
        ``ValueError`` into a swallowing ``except`` — so SIGTERM had no restore
        at all in production while a push-level test reported it installed.
        """
        registered: list[object] = []
        installed_signals: list[int] = []
        monkeypatch.setattr("coder_eval.fs_permissions.atexit.register", registered.append)

        def _fake_signal(signum, _handler):
            # Reproduce the property that made the original bug invisible: the
            # real signal.signal raises off the main thread. A permissive stub
            # records an install that CPython would have refused, which is
            # exactly how a push()-time install passed its own test while doing
            # nothing in production.
            if threading.current_thread() is not threading.main_thread():
                raise ValueError("signal only works in main thread of the main interpreter")
            installed_signals.append(signum)

        monkeypatch.setattr("coder_eval.fs_permissions.signal.signal", _fake_signal)
        monkeypatch.setattr("coder_eval.fs_permissions._registry", _PermissionStack())

        async with set_permissions([guarded_dir]):
            pass

        assert registered, "atexit restore was not registered when the window opened"
        assert {signal.SIGINT, signal.SIGTERM} <= set(installed_signals)

        # Second window must not re-install.
        before = len(registered)
        async with set_permissions([guarded_dir]):
            pass
        assert len(registered) == before

    async def test_install_failure_is_not_latched(self, guarded_dir, monkeypatch):
        """A failed install must be retried, not recorded as done.

        ``_install_crash_handlers`` used to swallow the failure internally and
        latch ``_handlers_installed = True`` regardless, so the one retry that
        could have succeeded (from the main thread) never happened.
        """
        attempts: list[int] = []

        def _refuse(signum, _handler):
            attempts.append(signum)
            raise ValueError("not the main thread")

        registry = _PermissionStack()

        # SCOPE the patches, do not lift them by hand. `signal.signal` is patched
        # process-wide, and the async teardown restores SIGINT by calling
        # `signal.signal(SIGINT, default_int_handler)` — which hits `_refuse` and raises
        # `ValueError` out of teardown, failing the test for something it does not test.
        # An explicit `undo()` after the calls fixed the happy path only: if
        # `ensure_crash_handlers` itself raised — the very regression this test exists to
        # catch — the undo would be skipped and the teardown ValueError would MASK the
        # real failure. A context manager restores on every exit path.
        with monkeypatch.context() as mp:
            mp.setattr("coder_eval.fs_permissions.atexit.register", lambda _fn: None)
            mp.setattr("coder_eval.fs_permissions.signal.signal", _refuse)

            registry.ensure_crash_handlers()
            first = set(attempts)
            attempts.clear()
            registry.ensure_crash_handlers()
            second = set(attempts)

        # Compare the two calls' signal SETS, not a running total of 4. Anything else in
        # the process that reaches fs_permissions inside the window lands in `attempts`
        # too, and under `-n auto` that depends on which tests share this worker — a count
        # assertion failed on a schedule change with `[INT, TERM, INT, TERM, INT]`,
        # reporting a latch bug that did not exist. The set form tolerates a stray
        # duplicate while still proving the retry: a latched install records nothing at
        # all on the second call.
        expected = {signal.SIGINT, signal.SIGTERM}
        assert first == expected, f"first install did not attempt both signals: {sorted(first)}"
        assert second == expected, (
            "a failed install must be retried on the next call, not latched — the second "
            f"call attempted {sorted(second)}"
        )

    @pytest.mark.parametrize("previous_is_callable", [True, False])
    async def test_signal_handler_restores_then_chains(self, guarded_dir, monkeypatch, previous_is_callable):
        """Invoke the handler that was actually installed, rather than discarding it.

        The chaining contract is the reason this handler is allowed to exist at
        all: an operator's Ctrl-C must not be swallowed. Capturing the handler is
        what makes that assertable.
        """
        monkeypatch.setattr("coder_eval.fs_permissions.atexit.register", lambda _fn: None)
        captured: dict[int, Any] = {}
        chained: list[int] = []
        killed: list[int] = []

        previous = (lambda sig, _frame: chained.append(sig)) if previous_is_callable else signal.SIG_DFL
        monkeypatch.setattr("coder_eval.fs_permissions.signal.getsignal", lambda _s: previous)
        monkeypatch.setattr("coder_eval.fs_permissions.signal.signal", lambda s, h: captured.__setitem__(s, h))
        monkeypatch.setattr("coder_eval.fs_permissions.os.kill", lambda _pid, sig: killed.append(sig))

        registry = _PermissionStack()
        registry.ensure_crash_handlers()
        original = _mode(guarded_dir)
        assert registry.push(guarded_dir, RESTRICTED_MODE) is True
        assert _mode(guarded_dir) == RESTRICTED_MODE

        captured[signal.SIGTERM](signal.SIGTERM, None)

        assert _mode(guarded_dir) == original, "the crash handler must restore before chaining"
        if previous_is_callable:
            assert chained == [signal.SIGTERM]
            assert killed == []
        else:
            # SIG_DFL: reset the disposition and re-raise, or SIGTERM stops killing.
            assert killed == [signal.SIGTERM]

    async def test_sig_ign_previous_is_not_re_raised(self, guarded_dir, monkeypatch):
        """SIG_IGN is neither callable nor SIG_DFL — the one disposition the
        original chain fell through entirely. Restoring and returning is correct;
        killing the process would override a deliberate `signal.signal(SIGINT,
        SIG_IGN)` by the embedding application."""
        monkeypatch.setattr("coder_eval.fs_permissions.atexit.register", lambda _fn: None)
        captured: dict[int, Any] = {}
        killed: list[int] = []
        monkeypatch.setattr("coder_eval.fs_permissions.signal.getsignal", lambda _s: signal.SIG_IGN)
        monkeypatch.setattr("coder_eval.fs_permissions.signal.signal", lambda s, h: captured.__setitem__(s, h))
        monkeypatch.setattr("coder_eval.fs_permissions.os.kill", lambda _pid, sig: killed.append(sig))

        registry = _PermissionStack()
        registry.ensure_crash_handlers()
        original = _mode(guarded_dir)
        registry.push(guarded_dir, RESTRICTED_MODE)

        captured[signal.SIGINT](signal.SIGINT, None)

        assert _mode(guarded_dir) == original
        assert killed == []

    async def test_failed_restore_keeps_the_entry_for_the_crash_path(self, guarded_dir, monkeypatch):
        """``pop`` used to ``del`` the entry BEFORE the restoring chmod, so a
        failed chmod stripped ``restore_all`` of the only record of the original
        mode — turning a recoverable failure into a permanently-000 tree."""
        registry = _PermissionStack()
        original = _mode(guarded_dir)
        assert registry.push(guarded_dir, RESTRICTED_MODE) is True

        real_chmod = os.chmod
        monkeypatch.setattr(
            "coder_eval.fs_permissions.os.chmod",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError(1, "refused")),
        )
        registry.pop(guarded_dir)
        monkeypatch.setattr("coder_eval.fs_permissions.os.chmod", real_chmod)

        assert _mode(guarded_dir) == RESTRICTED_MODE, "the failed chmod should have left it restricted"
        registry.restore_all()
        assert _mode(guarded_dir) == original, "restore_all could not recover: pop discarded the entry"

    async def test_same_path_via_different_routes_shares_one_entry(self, guarded_dir):
        """Refcount keys are resolved paths, so `d` and `d/../d` are one entry."""
        original = _mode(guarded_dir)
        indirect = guarded_dir.parent / ".." / guarded_dir.parent.name / guarded_dir.name

        async with set_permissions([guarded_dir]):
            async with set_permissions([indirect]):
                assert _mode(guarded_dir) == RESTRICTED_MODE
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original


class TestSandboxDriverGate:
    """`Sandbox.set_permissions` decides whether a chmod window applies at all."""

    def _sandbox(self, tmp_path, *, driver="tempdir"):
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox

        return Sandbox(SandboxConfig(driver=driver, python=None), task_id="t", task_dir=tmp_path)

    def test_host_run_does_not_enforce(self, tmp_path, monkeypatch):
        """On the host the agent is just another process with our uid, and
        `tasks/` is shared across a parallel batch — chmod buys nothing and has
        cross-task side effects on the user's working copy."""
        monkeypatch.delenv("CODER_EVAL_IN_CONTAINER", raising=False)
        assert self._sandbox(tmp_path).enforces_permission_windows is False

    def test_in_container_enforces_even_though_driver_reads_tempdir(self, tmp_path, monkeypatch):
        """REGRESSION GUARD for a silent-disable trap.

        `run_task_internal_command` rewrites `driver: docker` -> `tempdir` before
        constructing the Orchestrator, because nested docker is impossible in the
        image. So inside the container the driver reads "tempdir". Gating on the
        driver would therefore disable the anti-cheat window on exactly the path
        that needs it — the gate must key on CODER_EVAL_IN_CONTAINER instead.
        """
        monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
        sandbox = self._sandbox(tmp_path, driver="tempdir")
        assert sandbox.config.driver == "tempdir"
        assert sandbox.enforces_permission_windows is True

    async def test_no_op_on_host_leaves_mode_untouched(self, guarded_dir, tmp_path, monkeypatch):
        monkeypatch.delenv("CODER_EVAL_IN_CONTAINER", raising=False)
        original = _mode(guarded_dir)

        async with self._sandbox(tmp_path).set_permissions([guarded_dir]):
            assert _mode(guarded_dir) == original  # untouched

        assert _mode(guarded_dir) == original

    async def test_applies_in_container(self, guarded_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
        original = _mode(guarded_dir)

        async with self._sandbox(tmp_path).set_permissions([guarded_dir]):
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_nesting_still_works_through_the_sandbox(self, guarded_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
        sandbox = self._sandbox(tmp_path)

        async with sandbox.set_permissions([guarded_dir], mode=RESTRICTED_MODE):
            async with sandbox.set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                assert _mode(guarded_dir) == READ_ONLY_MODE
            assert _mode(guarded_dir) == RESTRICTED_MODE


class TestOrchestratorWiring:
    """End-to-end: the agent cannot read the reference during its own turn."""

    async def test_agent_cannot_read_reference_mid_turn(self, tmp_path, monkeypatch):
        """The regression this whole feature exists for.

        Drives the real ``_communicate_with_retry`` seam with an agent that tries
        to read the reference solution from inside ``communicate`` — exactly what a
        cheating agent does — and asserts it is denied, while the same read succeeds
        once the turn is over.
        """
        _skip_if_root()

        from unittest.mock import MagicMock

        from coder_eval.models import EvaluationResult, FinalStatus, TurnRecord
        from coder_eval.orchestrator import Orchestrator

        task_dir = tmp_path / "task"
        reference = task_dir / "reference"
        reference.mkdir(parents=True)
        (reference / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        task = _reference_task(reference_dir="reference")
        orchestrator = Orchestrator(
            task=task,
            run_dir=tmp_path / "run",
            variant_id="v",
            task_file=task_file,
        )
        await orchestrator._stage_reference()
        staged = orchestrator._reference_dir
        assert staged is not None

        orchestrator.sandbox = _container_sandbox(task_dir, tmp_path / "sbx", monkeypatch)
        orchestrator.result = EvaluationResult(
            task_id=task.task_id,
            task_description=task.description,
            variant_id="v",
            agent_type=task.agent.type,
            started_at=0.0,
            final_status=FinalStatus.SUCCESS,
            iteration_count=0,
            environment_info={},
        )

        observed: dict[str, object] = {}

        async def _cheating_communicate(prompt, **kwargs):
            observed["reference"] = _try_read(staged / "solution.py")
            observed["task_dir"] = _try_read(task_file)
            return TurnRecord(iteration=1, prompt=prompt, user_input=prompt, agent_output="done")

        agent = MagicMock()
        agent.communicate = _cheating_communicate
        agent.pending_turn = None
        orchestrator.agent = agent

        await orchestrator._communicate_with_retry(prompt="go", iteration=1, operation_label="test")

        # The reference is denied during the turn...
        assert observed["reference"] == "DENIED"
        # ...and so is the task dir. This reverses an earlier deliberate
        # exclusion, which held while the task dir was a `:ro` bind mount whose
        # chmod returned EROFS. It is now a read-write throwaway copy
        # (docker_runner._prepare_task_dir_mount), so the window applies -- and it
        # must, because the task dir carries run_command fixtures, expected
        # outputs, and (for a flat layout, where the parent is the whole `tasks/`
        # tree) every sibling task's reference solution.
        assert observed["task_dir"] == "DENIED"
        # ...and both readable again afterwards, so criteria and judges still work.
        assert (staged / "solution.py").read_text(encoding="utf-8") == "SOLUTION=42"
        assert task_file.read_text(encoding="utf-8") == "# task"

    async def test_reference_is_unshielded_by_the_time_criteria_run(self, tmp_path, monkeypatch):
        """Criteria and judges must see a READABLE reference.

        The window closes when ``_communicate_with_retry`` returns, so every
        ``check_all_async`` call site sits outside it. That ordering is easy to
        break by widening the ``async with`` — this test asserts the observable
        mode at the moment criteria actually run, not just the call structure.
        """
        from unittest.mock import AsyncMock, MagicMock

        from coder_eval.models import CriterionResult, EvaluationResult, FinalStatus, TurnRecord
        from coder_eval.orchestrator import Orchestrator

        task_dir = tmp_path / "task"
        reference = task_dir / "reference"
        reference.mkdir(parents=True)
        (reference / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        task = _reference_task(reference_dir="reference")
        orchestrator = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="v", task_file=task_file)
        await orchestrator._stage_reference()
        staged = orchestrator._reference_dir
        assert staged is not None

        orchestrator.sandbox = _container_sandbox(task_dir, tmp_path / "sbx", monkeypatch)
        orchestrator.result = EvaluationResult(
            task_id=task.task_id,
            task_description=task.description,
            variant_id="v",
            agent_type=task.agent.type,
            started_at=0.0,
            final_status=FinalStatus.SUCCESS,
            iteration_count=0,
            environment_info={},
        )

        seen: dict[str, object] = {}

        async def _communicate(prompt, **kwargs):
            seen["during_turn"] = _mode(staged)
            return TurnRecord(iteration=1, prompt=prompt, user_input=prompt, agent_output="done")

        agent = MagicMock()
        agent.communicate = _communicate
        agent.pending_turn = None
        orchestrator.agent = agent

        async def _check_all_async(*args, **kwargs):
            seen["during_criteria"] = _mode(staged)
            # The judge reads reference files off disk — prove that works here.
            seen["content"] = (staged / "solution.py").read_text(encoding="utf-8")
            return [CriterionResult(criterion_type="file_exists", description="x", score=1.0)]

        orchestrator.success_checker = MagicMock()
        orchestrator.success_checker.check_all_async = AsyncMock(side_effect=_check_all_async)

        await orchestrator._evaluation_loop()

        assert seen["during_turn"] == RESTRICTED_MODE
        assert seen["during_criteria"] != RESTRICTED_MODE
        assert seen["content"] == "SOLUTION=42"


def _container_sandbox(task_dir: Path, sandbox_dir: Path, monkeypatch) -> "object":
    """A real Sandbox that reports itself as running inside a docker container.

    `enforces_permission_windows` keys on CODER_EVAL_IN_CONTAINER rather than
    `config.driver`, precisely because the in-container entry point rewrites
    the driver to "tempdir" — so this fixture mirrors production by leaving the
    driver at "tempdir" and setting only the env var.
    """
    from coder_eval.models import SandboxConfig
    from coder_eval.sandbox import Sandbox

    monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="anti_cheat", task_dir=task_dir)
    sandbox.sandbox_dir = sandbox_dir
    return sandbox


def _try_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (PermissionError, FileNotFoundError):
        return "DENIED"


def _reference_task(*, reference_dir: str):
    from coder_eval.models import (
        FileExistsCriterion,
        ReferenceSource,
        SandboxConfig,
        TaskDefinition,
        parse_agent_config,
    )

    return TaskDefinition(
        task_id="anti_cheat",
        description="d",
        initial_prompt="do it",
        agent=parse_agent_config(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="x.py", description="x")],
        reference=ReferenceSource(directory=reference_dir),
    )


def _orchestrator_for(tmp_path: Path):
    """A bare Orchestrator suitable for driving _cleanup directly.

    No sandbox, no agent, no result -- _cleanup tolerates all three being unset,
    and the reference-removal branch is what these tests exercise.
    """
    from coder_eval.orchestrator import Orchestrator

    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    task_file = task_dir / "task.yaml"
    task_file.write_text("# task", encoding="utf-8")
    return Orchestrator(
        task=_reference_task(reference_dir="reference"),
        run_dir=tmp_path / "run",
        variant_id="v",
        task_file=task_file,
    )


class TestSetupWiring:
    """Guards that the feature is actually armed by _setup, not just callable."""

    async def test_setup_stages_the_reference_and_binds_it_to_the_sandbox(self, tmp_path, monkeypatch):
        """Without this, deleting the `_stage_reference()` call from `_setup` keeps
        the whole suite green while every real run has `_reference_dir is None` —
        no REFERENCE_DIR, no shielded path, judges grading with no reference.
        Every other test in this file calls `_stage_reference()` by hand.
        """
        from unittest.mock import AsyncMock, patch

        from coder_eval.models import EvaluationResult, FinalStatus
        from coder_eval.orchestrator import Orchestrator
        from coder_eval.orchestrator import settings as orchestrator_settings

        task_dir = tmp_path / "task"
        (task_dir / "reference").mkdir(parents=True)
        (task_dir / "reference" / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        task = _reference_task(reference_dir="reference")
        orchestrator = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="v", task_file=task_file)
        orchestrator.result = EvaluationResult(
            task_id=task.task_id,
            task_description=task.description,
            variant_id="v",
            agent_type=task.agent.type,
            started_at=0.0,
            final_status=FinalStatus.SUCCESS,
            iteration_count=0,
            environment_info={},
        )

        # Stop _setup after the sandbox is built: creating a real agent needs creds.
        # Agent construction may still fail; the assertions below only depend on
        # the staging step, which runs first.
        with (
            patch.object(Orchestrator, "_create_and_start_agent", new=AsyncMock(return_value=None), create=True),
            patch.object(type(orchestrator_settings), "validate_api_keys", return_value=None),
            contextlib.suppress(Exception),
        ):
            await orchestrator._setup()

        assert orchestrator._reference_dir is not None, "_setup did not stage the reference"
        assert (orchestrator._reference_dir / "solution.py").read_text(encoding="utf-8") == "SOLUTION=42"
        assert orchestrator.sandbox is not None
        assert orchestrator.sandbox.reference_dir == orchestrator._reference_dir
        await orchestrator._cleanup()

    async def test_cleanup_removes_the_staging_root_even_when_left_at_mode_000(self, tmp_path):
        """A killed run can leave the staged copy unreadable; plain
        rmtree(ignore_errors=True) then silently declines, orphaning a tempdir
        that holds the solution.

        Drives ``_cleanup`` itself. The earlier version of this test named
        ``_cleanup`` but called the helper directly, so ``_cleanup`` read as
        covered while it was in fact still using the swallowing rmtree the
        helper's docstring rejects.
        """
        orchestrator = _orchestrator_for(tmp_path)
        root = tmp_path / "staging"
        (root / "reference").mkdir(parents=True)
        (root / "reference" / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        orchestrator._reference_staging_root = root
        orchestrator._reference_dir = root / "reference"
        os.chmod(root / "reference", RESTRICTED_MODE)

        await orchestrator._cleanup()

        assert not root.exists()
        assert orchestrator._reference_staging_root is None

    async def test_cleanup_removes_the_staging_root_when_the_copy_failed(self, tmp_path):
        """CLAUDE.md promises cleanup is 'keyed on the mkdtemp root so a failed
        copy still cleans up'. Keying on ``_reference_dir.parent`` broke that:
        ``_reference_dir`` is assigned only on the success path, so a copytree
        that raised left a partial copy of the solution behind with no warning."""
        orchestrator = _orchestrator_for(tmp_path)
        root = tmp_path / "staging"
        (root / "reference").mkdir(parents=True)
        (root / "reference" / "partial.py").write_text("SOLUTION=42", encoding="utf-8")
        # Exactly the state after a mid-copytree failure: root recorded, no
        # _reference_dir.
        orchestrator._reference_staging_root = root
        orchestrator._reference_dir = None

        await orchestrator._cleanup()

        assert not root.exists()

    async def test_cleanup_never_touches_the_container_mount(self, tmp_path):
        """/work/references is host-owned; rmtree'ing its parent would take /work."""
        orchestrator = _orchestrator_for(tmp_path)
        orchestrator._reference_dir = Path(CONTAINER_REFERENCE_DIR)
        orchestrator._reference_staging_root = None

        removed: list[Path] = []
        with patch("coder_eval.orchestrator.rmtree_restrictive", side_effect=removed.append):
            await orchestrator._cleanup()

        assert removed == []


@pytest.fixture
def container_mode(tmp_path, monkeypatch):
    """Make the in-container code paths reachable from a unit test.

    Docker is the ONLY driver where the reference feature does anything
    (``Sandbox.enforces_permission_windows`` is False everywhere else), so
    without this fixture ``make test`` proves none of the shipped behaviour —
    the happy path was covered solely by a live-API CI job and the fail-closed
    raise by nothing at all.

    Patches the module-level ``CONTAINER_REFERENCE_DIR`` constants (there is no
    ``/work/references`` on a dev box) and sets the env gate.
    """
    mount = tmp_path / "work_references"
    monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
    monkeypatch.setattr("coder_eval.orchestration.evaluation.CONTAINER_REFERENCE_DIR", str(mount))
    monkeypatch.setattr("coder_eval.orchestrator.CONTAINER_REFERENCE_DIR", str(mount))
    return mount


class TestInContainerReferenceResolution:
    """The `/work/references` branch: docker is the only driver this feature runs on."""

    def test_mount_wins_over_the_task_relative_path(self, tmp_path, container_mode):
        """In-container the task-dir copy is masked by an empty tmpfs, so
        resolving relative to the task file would find the MASK, not the
        solution — and the mode-000 window would then shield a decoy."""
        from coder_eval.orchestration.evaluation import resolve_reference_dir

        container_mode.mkdir()
        (container_mode / "solution.py").write_text("REAL", encoding="utf-8")
        task_dir = tmp_path / "task"
        (task_dir / "reference").mkdir(parents=True)
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        resolved = resolve_reference_dir(_reference_task(reference_dir="reference"), task_file)

        assert resolved == container_mode
        assert (resolved / "solution.py").read_text(encoding="utf-8") == "REAL"

    def test_missing_mount_fails_closed(self, tmp_path, container_mode):
        """A missing mount must NOT fall back to the task-relative path.

        That fallback resolves to the un-masked reference under the `:ro`
        task-dir bind, which the window then cannot chmod (EROFS) — so the run
        would complete with the solution readable for the whole turn while
        reporting an ordinary pass/fail.
        """
        from coder_eval.orchestration.evaluation import resolve_reference_dir

        task_dir = tmp_path / "task"
        task_dir.mkdir()
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        with pytest.raises(FileNotFoundError) as excinfo:
            resolve_reference_dir(_reference_task(reference_dir="reference"), task_file)

        message = str(excinfo.value)
        # Names the author's literal value and the likely cause first — a typo'd
        # reference.directory is far more common than a stale image.
        assert "reference" in message
        assert "does not resolve to a directory" in message
        assert "refusing to run unprotected" in message

    def test_env_gate_is_required_not_just_the_path(self, tmp_path, monkeypatch):
        """A bare `/work/references` probe would hijack every task's reference on
        any host that happens to have that directory — silently, with wrong
        reference content and wrong scores."""
        from coder_eval.orchestration.evaluation import resolve_reference_dir

        mount = tmp_path / "work_references"
        mount.mkdir()
        (mount / "solution.py").write_text("HIJACKED", encoding="utf-8")
        monkeypatch.delenv("CODER_EVAL_IN_CONTAINER", raising=False)
        monkeypatch.setattr("coder_eval.orchestration.evaluation.CONTAINER_REFERENCE_DIR", str(mount))

        task_dir = tmp_path / "task"
        (task_dir / "reference").mkdir(parents=True)
        (task_dir / "reference" / "solution.py").write_text("REAL", encoding="utf-8")
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        resolved = resolve_reference_dir(_reference_task(reference_dir="reference"), task_file)

        assert resolved == (task_dir / "reference").resolve()

    async def test_stage_reference_does_not_copy_the_container_mount(self, tmp_path, container_mode):
        """Re-copying would move the shielded path OFF the one the agent attacks
        — the exact leak `tasks/anti_cheat_reference` caught on its first run."""
        container_mode.mkdir()
        (container_mode / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        orchestrator = _orchestrator_for(tmp_path)

        await orchestrator._stage_reference()

        assert orchestrator._reference_dir == Path(str(container_mode))
        assert orchestrator._reference_staging_root is None, "the host owns this dir; we must not schedule a delete"

    async def test_cleanup_does_not_rmtree_the_mounts_parent(self, tmp_path, container_mode):
        """rmtree'ing `/work/references`'s parent would take `/work` with it."""
        container_mode.mkdir()
        (container_mode / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        orchestrator = _orchestrator_for(tmp_path)
        await orchestrator._stage_reference()

        await orchestrator._cleanup()

        assert container_mode.exists(), "the host-owned reference mount was deleted"
        assert (container_mode / "solution.py").exists()


class TestFailClosed:
    """A window that cannot be applied must not produce a normal-looking score."""

    async def test_strict_raises_when_the_chmod_is_refused(self, guarded_dir, monkeypatch):
        """Left as a warning, an unprotected run is indistinguishable downstream
        from a protected one — same task.json, same run.json, same report."""
        from coder_eval.fs_permissions import PermissionWindowError

        monkeypatch.setattr(
            "coder_eval.fs_permissions.os.chmod",
            lambda *_a, **_k: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted")),
        )

        with pytest.raises(PermissionWindowError, match="would be able to read it"):
            async with set_permissions([guarded_dir], strict=True):
                pass

    async def test_non_strict_still_warns_and_continues(self, guarded_dir, monkeypatch, caplog):
        """Off the enforced path (host runs) a refused chmod must stay advisory."""
        monkeypatch.setattr(
            "coder_eval.fs_permissions.os.chmod",
            lambda *_a, **_k: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted")),
        )

        with caplog.at_level("WARNING"):
            async with set_permissions([guarded_dir]):
                pass

        assert any("could not chmod" in r.message for r in caplog.records)

    async def test_missing_path_is_not_an_error_even_under_strict(self, tmp_path):
        """A task with no reference passes None/absent paths; that is the normal case."""
        async with set_permissions([tmp_path / "absent"], strict=True):
            pass

    async def test_sandbox_uses_strict_only_when_it_enforces(self, tmp_path, monkeypatch):

        seen: dict[str, Any] = {}

        @contextlib.asynccontextmanager
        async def _spy(paths, *, mode=RESTRICTED_MODE, strict=False):
            seen["strict"] = strict
            yield

        monkeypatch.setattr("coder_eval.sandbox.set_permissions", _spy)
        sandbox = _container_sandbox(tmp_path / "task", tmp_path / "sbx", monkeypatch)

        async with sandbox.set_permissions([tmp_path]):
            pass

        assert seen["strict"] is True


class TestCancellationOnEnter:
    """A cancel landing on __aenter__ must not strand the tree at mode 000."""

    async def test_cancel_during_push_still_unwinds(self, guarded_dir, monkeypatch):
        """`asyncio.shield` protects the INNER task, not the awaiting coroutine.

        With the push above the `try`, a task_timeout cancel raised out of
        __aenter__ while the worker thread went on completing every chmod: the
        `finally` never ran, the path stayed at 000 for the rest of the run
        (failing every downstream $REFERENCE_DIR criterion), and the leaked
        registry entry poisoned the next window on the same path.
        """
        original = _mode(guarded_dir)
        real_chmod = os.chmod
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _slow_chmod(path, mode):
            loop.call_soon_threadsafe(started.set)
            real_chmod(path, mode)

        monkeypatch.setattr("coder_eval.fs_permissions.os.chmod", _slow_chmod)

        async def _body():
            async with set_permissions([guarded_dir]):
                pass

        task = asyncio.create_task(_body())
        await started.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert _mode(guarded_dir) == original, "cancel on __aenter__ left the path shielded"
        # And the registry must be clean, or the next window records 000 as its
        # own "original" and never restores.
        from coder_eval.fs_permissions import _registry

        assert guarded_dir.resolve() not in _registry._entries


class TestReferenceIntegrity:
    """The window is per-turn and the docker mount must be writable, so an
    agent-backgrounded process can overwrite the reference between turns and
    drive reference_comparison to 1.0."""

    async def test_unmodified_reference_passes(self, tmp_path):
        orchestrator = _orchestrator_for(tmp_path)
        ref = tmp_path / "ref"
        ref.mkdir()
        (ref / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        orchestrator._reference_dir = ref
        orchestrator._reference_digest = digest_tree(ref)

        await orchestrator._verify_reference_integrity()

    async def test_overwritten_reference_is_an_error_not_a_score(self, tmp_path):
        from coder_eval.errors import ReferenceTamperedError

        orchestrator = _orchestrator_for(tmp_path)
        ref = tmp_path / "ref"
        ref.mkdir()
        (ref / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        orchestrator._reference_dir = ref
        orchestrator._reference_digest = digest_tree(ref)

        # Exactly what a backgrounded `cp my_answer.py /work/references/` does.
        (ref / "solution.py").write_text("def whatever_the_agent_wrote(): ...", encoding="utf-8")

        with pytest.raises(ReferenceTamperedError, match="changed during the run"):
            await orchestrator._verify_reference_integrity()

    async def test_added_file_is_detected(self, tmp_path):
        from coder_eval.errors import ReferenceTamperedError

        orchestrator = _orchestrator_for(tmp_path)
        ref = tmp_path / "ref"
        ref.mkdir()
        (ref / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        orchestrator._reference_dir = ref
        orchestrator._reference_digest = digest_tree(ref)

        (ref / "extra.py").write_text("", encoding="utf-8")

        with pytest.raises(ReferenceTamperedError):
            await orchestrator._verify_reference_integrity()

    def test_digest_is_order_independent_and_path_sensitive(self, tmp_path):
        """os.walk order must not leak into the digest, but a RENAME must."""
        a, b = tmp_path / "a", tmp_path / "b"
        for root in (a, b):
            (root / "pkg").mkdir(parents=True)
        (a / "pkg" / "one.py").write_text("1", encoding="utf-8")
        (a / "two.py").write_text("2", encoding="utf-8")
        (b / "two.py").write_text("2", encoding="utf-8")
        (b / "pkg" / "one.py").write_text("1", encoding="utf-8")

        assert digest_tree(a) == digest_tree(b)

        (b / "two.py").rename(b / "renamed.py")
        assert digest_tree(a) != digest_tree(b)


class TestConfigErrorsFailBeforeTheAgentRuns:
    async def test_typoed_reference_file_raises_during_setup(self, tmp_path):
        """A gating score=0.0 books an eval-config error against the agent's pass
        rate; on a dataset-fanned suite it zeroes every row silently."""
        from coder_eval.models import ReferenceComparisonCriterion

        orchestrator = _orchestrator_for(tmp_path)
        orchestrator.task.success_criteria = [
            ReferenceComparisonCriterion(
                description="cmp",
                agent_file="solution.py",
                reference_file="typo.py",
            )
        ]
        task_dir = orchestrator.task_file.parent
        (task_dir / "reference").mkdir(exist_ok=True)
        (task_dir / "reference" / "solution.py").write_text("SOLUTION=42", encoding="utf-8")

        with pytest.raises(ValueError, match="task-definition error, not an agent failure"):
            await orchestrator._stage_reference()

        await orchestrator._cleanup()
