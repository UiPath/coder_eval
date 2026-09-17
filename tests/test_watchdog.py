"""Unit tests for ``coder_eval.agents.watchdog.ThreadedWatchdog``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time

import pytest

from coder_eval.agents.watchdog import ThreadedWatchdog, WatchdogFired, run_with_watchdog


def test_watchdog_fires_and_invokes_callback() -> None:
    """Timer fires and invokes the callback; ``fired`` flag flips True."""
    called = threading.Event()
    wd = ThreadedWatchdog(timeout_seconds=0.05, on_timeout=called.set, label="test")
    with wd:
        assert called.wait(timeout=0.5), "callback did not run within 500ms"
    assert wd.fired is True


@pytest.mark.asyncio
async def test_watchdog_cancels_asyncio_task() -> None:
    """Timer cancels the provided asyncio task via call_soon_threadsafe."""
    wd = ThreadedWatchdog(
        timeout_seconds=0.05,
        on_timeout=lambda: None,
        asyncio_task_to_cancel=asyncio.current_task(),
        label="async-cancel",
    )
    with pytest.raises(asyncio.CancelledError), wd:
        await asyncio.sleep(5)
    assert wd.fired is True


def test_watchdog_does_not_fire_when_block_exits_early() -> None:
    """If the ``with`` block exits before the deadline, the callback never runs."""
    called = threading.Event()

    wd = ThreadedWatchdog(timeout_seconds=2.0, on_timeout=lambda: called.set(), label="early-exit")
    with wd:
        time.sleep(0.05)  # exit well before deadline

    # Wait past the original deadline to confirm the timer was cancelled.
    assert not called.wait(timeout=2.5)
    assert wd.fired is False


def test_watchdog_none_timeout_is_noop() -> None:
    """``timeout_seconds=None`` starts no timer; callback never runs."""
    called = threading.Event()
    wd = ThreadedWatchdog(timeout_seconds=None, on_timeout=lambda: called.set(), label="none-to")
    with wd:
        time.sleep(0.1)
    assert wd.fired is False
    assert not called.is_set()


def test_watchdog_zero_timeout_is_noop() -> None:
    """``timeout_seconds=0`` starts no timer; callback never runs."""
    called = threading.Event()
    wd = ThreadedWatchdog(timeout_seconds=0, on_timeout=lambda: called.set(), label="zero-to")
    with wd:
        time.sleep(0.1)
    assert wd.fired is False
    assert not called.is_set()


def test_watchdog_callback_exception_is_swallowed() -> None:
    """If the callback raises, the timer thread swallows it and ``fired`` stays True."""

    def _bad_cb() -> None:
        raise RuntimeError("boom")

    wd = ThreadedWatchdog(timeout_seconds=0.05, on_timeout=_bad_cb, label="bad-cb")
    with wd:
        time.sleep(0.15)
    assert wd.fired is True


def test_watchdog_double_fire_protected_by_lock() -> None:
    """Concurrent firing paths invoke the callback at most once.

    Stresses the internal lock by invoking the fire path from multiple
    threads. Uses ``timeout_seconds=None`` so the real timer never starts
    — we exercise only the concurrent-fire code path.
    """
    count = 0
    count_lock = threading.Lock()

    def _cb() -> None:
        nonlocal count
        with count_lock:
            count += 1

    wd = ThreadedWatchdog(timeout_seconds=None, on_timeout=_cb, label="double-fire")

    threads = [threading.Thread(target=wd._fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wd.fired is True
    assert count == 1


def test_watchdog_logger_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Firing emits a WARNING log containing the label."""
    with caplog.at_level(logging.WARNING, logger="coder_eval.agents.watchdog"):
        wd = ThreadedWatchdog(timeout_seconds=0.05, on_timeout=lambda: None, label="my-label")
        with wd:
            time.sleep(0.15)

    assert wd.fired is True
    assert any("my-label" in rec.message and rec.levelname == "WARNING" for rec in caplog.records)


async def _sleep(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "done"


class TestRunWithWatchdog:
    """The turn body runs as a child task, so a watchdog timeout never lands a cancel on the caller."""

    async def test_a_fired_watchdog_raises_and_leaves_the_caller_uncancelled(self) -> None:
        async def caller() -> str:
            fired: list[bool] = []
            try:
                async with asyncio.timeout(0.6):
                    with pytest.raises(WatchdogFired):
                        await run_with_watchdog(
                            _sleep(5), timeout_seconds=0.1, on_timeout=lambda: fired.append(True), label="t"
                        )
                    task = asyncio.current_task()
                    assert task is not None and task.cancelling() == 0
                    assert fired == [True]
                    await asyncio.sleep(5)
            except TimeoutError:
                return "enclosing timeout raised TimeoutError"
            return "no timeout"

        assert await asyncio.ensure_future(caller()) == "enclosing timeout raised TimeoutError"

    async def test_cancelling_the_caller_propagates_and_cancels_the_body(self) -> None:
        started = asyncio.Event()
        bodies: list[asyncio.Task[object]] = []

        async def body() -> str:
            task = asyncio.current_task()
            assert task is not None
            bodies.append(task)
            started.set()
            await asyncio.sleep(5)
            return "done"

        outer = asyncio.ensure_future(run_with_watchdog(body(), timeout_seconds=10, on_timeout=lambda: None, label="t"))
        await started.wait()
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0)
        assert bodies[0].cancelled()

    async def test_a_body_finishing_at_the_deadline_leaks_no_late_cancel(self) -> None:
        async def caller() -> str:
            value = await run_with_watchdog(_sleep(0.19), timeout_seconds=0.2, on_timeout=lambda: None, label="t")
            await asyncio.sleep(0.3)
            return value

        for _ in range(20):
            with contextlib.suppress(WatchdogFired):
                assert await asyncio.ensure_future(caller()) == "done"

    async def test_a_body_exception_propagates_unchanged(self) -> None:
        async def body() -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await run_with_watchdog(body(), timeout_seconds=5, on_timeout=lambda: None, label="t")

    async def test_no_timeout_still_runs_the_body(self) -> None:
        assert await run_with_watchdog(_sleep(0), timeout_seconds=None, on_timeout=lambda: None, label="t") == "done"
