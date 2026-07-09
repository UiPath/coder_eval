"""Unit tests for ``coder_eval.agents.watchdog.ThreadedWatchdog``."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from coder_eval.agents.watchdog import ThreadedWatchdog


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
