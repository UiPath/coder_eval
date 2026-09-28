"""Temporary filesystem-permission windows for anti-cheat.

The agent shares the harness's filesystem view, so it can read anything the
harness can — the staged reference solution included.
:func:`set_permissions` chmods them (0o000 by default) for the body of an
``async with`` block and falls back on exit; the orchestrator wraps every
``agent.communicate`` call in it.

Windows **stack**; an inner one may be MORE permissive::

    async with set_permissions([reference], mode=RESTRICTED_MODE):
        ...                                       # agent turn: 000
        async with set_permissions([reference], mode=READ_ONLY_MODE):
            ...                                   # can read: 555
        ...                                       # back to 000

The inner form is for live success criteria; not wired up yet.

Pre-window modes are captured, not hardcoded; unwinds also run from
:mod:`atexit` and on ``SIGINT``/``SIGTERM``.

.. warning::
   **Defense-in-depth, not a boundary.** ``chmod`` is a DAC control; container
   root can re-open the path or wait the window out.
   Rationale: .claude/notes/permissions.md § Reference solutions and the anti-cheat window

Rationale: .claude/notes/permissions.md § The stacked chmod window
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import signal
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from types import FrameType
from typing import Any


logger = logging.getLogger(__name__)

# What ``signal.getsignal`` can hand back: a Python callable, one of the
# SIG_DFL/SIG_IGN sentinels, or None for a handler installed from C. Spelled out
# so the chained call into it is argument-checked instead of hidden behind
# ``object`` + a blanket ``# type: ignore``.
_SignalDisposition = Callable[[int, FrameType | None], Any] | int | signal.Handlers | None


# Mode applied during an agent turn: no read, write, or traverse for anyone.
RESTRICTED_MODE = 0o000

# Read + traverse, no write. The mode to re-grant with when something that runs
# INSIDE the turn window legitimately needs to read a shielded path.
READ_ONLY_MODE = 0o555


class PermissionWindowError(RuntimeError):
    """A permission window that MUST hold could not be applied.

    Raised only under ``strict=True`` — i.e. from ``Sandbox.set_permissions``
    inside a container, where the window is the anti-cheat control rather than a
    best-effort nicety.
    """


class _PermissionStack:
    """Process-wide stack of applied modes, per resolved path.

    A plain stack, not a refcount: windows nest with *different* modes, so what
    an exit restores is the enclosing window's mode.

    Keyed by the *resolved* path, so a directory reached by two routes is one
    entry. Guarded by a ``threading`` lock, not an ``asyncio`` one, because the
    crash-safety handlers run outside the event loop and must be able to take it.

    Rationale: .claude/notes/permissions.md § Locking and crash safety
    """

    def __init__(self) -> None:
        self._handlers_installed = False
        # RLock, not Lock: a signal handler's restore_all() can land while
        # atexit's is mid-flight, and a non-reentrant lock deadlocks at exit.
        self._lock = threading.RLock()
        # resolved path -> (mode before the outermost window, applied-mode stack)
        self._entries: dict[Path, tuple[int, list[int]]] = {}

    def push(self, path: Path, mode: int, *, strict: bool = False) -> bool:
        """Apply ``mode`` to ``path`` and record it for the matching :meth:`pop`.

        Returns True when the caller must later pop. Returns False when the mode
        could not be applied at all (missing path, or chmod refused) -- the
        caller then skips the matching pop.

        Does NOT install the crash handlers: ``push`` runs on a worker thread
        (``asyncio.to_thread``), where ``signal.signal`` raises ``ValueError``.
        :func:`set_permissions` installs them from the event-loop thread before
        the offload.
        """
        with self._lock:
            existing = self._entries.get(path)
            original = existing[0] if existing is not None else None
            try:
                if original is None:
                    original = path.stat().st_mode & 0o7777
                os.chmod(path, mode)
            except OSError as e:
                # A missing path is the common, benign case. A genuine refusal
                # means this run is not protected, so the operator hears about it.
                if isinstance(e, FileNotFoundError):
                    logger.debug("set_permissions: %s does not exist; nothing to do", path)
                    return False
                message = (
                    f"set_permissions: could not chmod {path} to {mode:#o} ({e}) -- "
                    + "the agent would be able to read it during this turn"
                )
                if strict:
                    # Fail closed: an unprotected run that reports a normal
                    # pass/fail is indistinguishable from a protected one.
                    raise PermissionWindowError(message) from e
                logger.warning("%s", message)
                return False
            if existing is None:
                self._entries[path] = (original, [mode])
            else:
                existing[1].append(mode)
            logger.debug("set_permissions: %s -> %#o (depth %d)", path, mode, len(self._entries[path][1]))
            return True

    def pop(self, path: Path) -> None:
        """Undo the innermost applied mode: fall back to the enclosing one.

        Restores the pre-window mode only when the outermost window closes.
        """
        with self._lock:
            entry = self._entries.get(path)
            if entry is None:
                return
            original, applied = entry
            applied.pop()
            target = applied[-1] if applied else original
            # chmod INSIDE the lock: releasing first lets a concurrent push()
            # record the restricted mode as its `original` and strand the path
            # at 000 permanently.
            try:
                os.chmod(path, target)
                logger.debug("set_permissions: %s <- %#o", path, target)
            except OSError as e:
                logger.error(
                    "set_permissions: FAILED to chmod %s back to %#o (%s) -- the path may need a manual chmod",
                    path,
                    target,
                    e,
                )
                # Keep the entry: restore_all() is the last chance to put this
                # path back, and it needs the pre-window mode this entry holds.
                return
            if not applied:
                del self._entries[path]

    def ensure_crash_handlers(self) -> None:
        """Install atexit + signal restores once, before the first window opens.

        MUST be called from the main thread: ``signal.signal`` raises
        ``ValueError`` anywhere else. Deliberately NOT done at import time —
        ``sandbox.py`` imports this module, and an import-time install would
        rewrite SIGINT/SIGTERM disposition for every process that merely imports
        ``coder_eval``.

        The flag latches only when the signal handlers really went in, so a call
        from a worker thread is retried from the main thread later.

        Rationale: .claude/notes/permissions.md § Locking and crash safety
        """
        with self._lock:
            if self._handlers_installed:
                return
            self._handlers_installed = _install_crash_handlers(self)

    def restore_all(self) -> None:
        """Unwind every outstanding path to its pre-window mode (crash path)."""
        with self._lock:
            outstanding = [(path, entry[0]) for path, entry in self._entries.items()]
            self._entries.clear()
        for path, original in outstanding:
            try:
                os.chmod(path, original)
                logger.warning("set_permissions: emergency-restored %s to %#o", path, original)
            except OSError as e:
                logger.error("set_permissions: emergency restore of %s failed: %s", path, e)


_registry = _PermissionStack()


def _make_signal_handler(
    registry: _PermissionStack,
    previous: _SignalDisposition,
) -> Callable[[int, FrameType | None], None]:
    """Build a handler that restores ``registry``, then chains to ``previous``.

    Chaining matters twice over: an operator's Ctrl-C must not be swallowed, and
    SIGTERM must still terminate.

    Rationale: .claude/notes/permissions.md § Locking and crash safety
    """

    def _handler(sig: int, frame: FrameType | None) -> None:
        registry.restore_all()
        if callable(previous):
            previous(sig, frame)
        elif previous == signal.SIG_IGN:
            return
        else:
            # SIG_DFL, or None == a handler installed from C. Treating both as
            # SIG_DFL keeps SIGTERM terminating.
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)

    return _handler


def _install_crash_handlers(registry: _PermissionStack) -> bool:
    """Register the atexit + signal restores for ``registry``.

    Returns True only when every signal handler was installed, so the caller can
    decline to latch a partial (or entirely failed) install. Called once, under
    the registry's lock, from the main thread.
    """
    atexit.register(registry.restore_all)

    installed_all = True
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(signum)
            signal.signal(signum, _make_signal_handler(registry, previous))
        except (ValueError, OSError) as e:
            # atexit covers ordinary exit, but SIGTERM does NOT run atexit —
            # a killed run can strand the tree at mode 000. Warn, don't debug.
            installed_all = False
            logger.warning(
                "set_permissions: could not install a restore handler for signal %s (%s); "
                + "a kill -TERM during a turn may leave the reference at mode 000",
                signum,
                e,
            )
    return installed_all


@contextlib.asynccontextmanager
async def set_permissions(
    paths: Iterable[Path | None],
    *,
    mode: int = RESTRICTED_MODE,
    strict: bool = False,
) -> AsyncIterator[None]:
    """Chmod ``paths`` to ``mode`` for the body, then fall back on exit.

    Windows NEST, and an inner window may be *more* permissive than the one
    around it. Exiting restores the enclosing window's mode; only the outermost
    exit restores the pre-window mode.

    ``None`` entries and duplicates are dropped, so callers can pass optional
    paths without pre-filtering. Paths are resolved before use. The unwind runs
    in a ``finally``, so an agent crash or turn timeout cannot leave the tree
    unreadable.

    Args:
        paths: Directories (or files) to chmod. ``None`` entries are skipped.
        mode: Permission bits to apply. Defaults to :data:`RESTRICTED_MODE`.
        strict: Raise :class:`PermissionWindowError` when an existing path
            cannot be chmod'd, instead of warning and continuing unprotected.

    Raises:
        PermissionWindowError: under ``strict``, when a path exists but the
            chmod was refused.

    Rationale: .claude/notes/permissions.md § strict=True and the hard-fail path
    """
    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        if raw is None:
            continue
        try:
            candidate = Path(raw).resolve()
        except OSError as e:
            # Same fail-open outcome as a chmod refusal, same visibility.
            logger.warning("set_permissions: could not resolve %s (%s); it will not be shielded", raw, e)
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved.append(candidate)

    # HERE, on the event-loop (main) thread, not inside _push_all:
    # signal.signal() raises ValueError off the main thread.
    if resolved:
        _registry.ensure_crash_handlers()

    # chmod is a syscall per path; offload so a slow filesystem doesn't stall
    # the event loop about to drive the agent's streaming turn.
    held: list[Path] = []
    push_task: asyncio.Future[list[Path]] | None = None
    try:
        if resolved:
            # INSIDE the try, so the finally ALWAYS runs. shield protects the
            # inner task, not this await: a cancellation still raises here while
            # the worker completes every chmod — which is why the pop below
            # joins the task rather than assuming nothing landed.
            # Rationale: .claude/notes/permissions.md § Locking and crash safety
            push_task = asyncio.ensure_future(asyncio.to_thread(_push_all, resolved, mode, strict))
            held = await asyncio.shield(push_task)
        yield
    finally:
        if push_task is not None and not held:
            # Cancelled mid-push: join the shielded worker so `held` names
            # exactly what landed and the unwind cannot race it.
            held = await asyncio.shield(push_task)
        if held:
            # Shielded: the unwind MUST run under cancellation too, or the
            # tree stays at 000.
            await asyncio.shield(asyncio.to_thread(_pop_all, held))


def _push_all(paths: list[Path], mode: int, strict: bool) -> list[Path]:
    """Push every path, returning only those that must later be popped.

    Under ``strict`` a refused chmod raises, and the paths pushed before it are
    left applied — the context manager's ``finally`` cannot see a return value
    that never came. ``_registry.restore_all`` still holds their pre-window
    modes.

    Rationale: .claude/notes/permissions.md § strict=True and the hard-fail path
    """
    return [path for path in paths if _registry.push(path, mode, strict=strict)]


def _pop_all(paths: list[Path]) -> None:
    for path in paths:
        _registry.pop(path)
