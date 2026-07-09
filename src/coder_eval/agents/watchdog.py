"""OS-thread-based deadline enforcer for async code.

Replaces ``asyncio.sleep``/``asyncio.wait_for`` as the primitive for enforcing
wall-clock timeouts on code that may block the event loop or be wrapped in
anyio cancel scopes that swallow asyncio.CancelledError. The timer runs on a
daemon OS thread, so it fires even when the event loop is starved or stuck
on subprocess I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Self


logger = logging.getLogger(__name__)


class ThreadedWatchdog:
    """OS-thread-based deadline enforcer.

    A ``threading.Timer`` fires at ``timeout_seconds`` and invokes ``on_timeout``
    from the timer thread (NOT the asyncio event loop). After it fires,
    ``fired`` is True (readable from any thread).

    ``on_timeout`` MUST be synchronous and thread-safe. Exceptions it raises
    are logged and swallowed so one bad callback never kills the timer thread
    without a trace.

    Typical use in async code:

        def _on_timeout() -> None:
            kill_subprocess_by_pid(pid)  # sync, thread-safe

        with ThreadedWatchdog(
            timeout_seconds=1200,
            on_timeout=_on_timeout,
            asyncio_task_to_cancel=asyncio.current_task(),
            label="turn timeout",
        ) as wd:
            async for message in query(...):
                ...
        if wd.fired:
            raise TurnTimeoutError(...)

    Notes:
    - ``timeout_seconds`` of None or <= 0 → no-op watchdog (no timer started).
      This lets callers write a uniform ``with`` block regardless of whether
      a timeout is configured.
    - When ``asyncio_task_to_cancel`` is provided, the timer thread also
      calls ``loop.call_soon_threadsafe(task.cancel)`` after ``on_timeout``.
      This delivers cancellation across the thread boundary so mock-based
      tests (no real subprocess) still unwind at the deadline.
    - Each instance is single-use. Re-entering the ``with`` block after exit
      is not supported.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float | None,
        on_timeout: Callable[[], None],
        asyncio_task_to_cancel: asyncio.Task[object] | None = None,
        label: str = "watchdog",
    ) -> None:
        self._timeout = timeout_seconds
        self._on_timeout = on_timeout
        self._task_to_cancel = asyncio_task_to_cancel
        self._label = label
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._fired = False

    @property
    def fired(self) -> bool:
        """True if the timer has fired (thread-safe)."""
        with self._lock:
            return self._fired

    def _fire(self) -> None:
        """Timer-thread callback. Short, exception-safe."""
        with self._lock:
            if self._fired:
                return
            self._fired = True
        logger.warning("%s fired after %.1fs — hard-killing subprocess", self._label, self._timeout or 0)
        try:
            self._on_timeout()
        except Exception:
            # Log-and-swallow: a bad on_timeout callback must not crash the
            # timer thread silently. `logger.exception` captures the traceback.
            logger.exception("%s on_timeout callback raised", self._label)
        if self._task_to_cancel is not None:
            # Loop may already be closed (e.g. interpreter shutdown), in
            # which case call_soon_threadsafe raises RuntimeError — we've
            # already done the kill, the cancel is a best-effort secondary.
            with contextlib.suppress(RuntimeError):
                loop = self._task_to_cancel.get_loop()
                loop.call_soon_threadsafe(self._task_to_cancel.cancel)

    def __enter__(self) -> Self:
        if self._timeout is None or self._timeout <= 0:
            return self
        self._timer = threading.Timer(self._timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
