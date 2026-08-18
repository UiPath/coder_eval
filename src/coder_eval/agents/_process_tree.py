"""Kill a subprocess *and its descendants*.

The agent SDKs spawn their CLI with a plain ``open_process``/``Popen`` — no
``start_new_session`` — so the CLI inherits **our** process group. That makes
``os.killpg`` unusable as a blanket strategy: it would take out the eval
harness itself. And killing only the CLI pid leaves whatever it spawned (a
``pytest``, an ``npm run build`` a Bash tool call started) alive and still
writing into the sandbox that success criteria are about to read.

So this walks the descendant tree explicitly:

* ``os.killpg`` is used ONLY when the target genuinely *leads* its own process
  group (``getpgid(pid) == pid``, and not our own group) — correct and atomic
  when available, e.g. if a future SDK version opts into ``start_new_session``.
* Otherwise, descendants are discovered from ``/proc/<pid>/stat``'s ppid field
  and signalled leaf-first.
* ``/proc`` is preferred over shelling out to ``ps`` because it is pure file
  reads: no fork (so this stays cheap enough to call from an event loop), and
  it is present in the slim images evals actually run in. Neither
  ``docker/Dockerfile`` nor ``docker/Dockerfile.runtime`` installs ``procps``,
  and in inject mode the kit is ``COPY --from``'d onto an arbitrary task base
  image — so a ``ps``-only implementation silently degraded to the pre-fix
  single-process kill in exactly the environment that matters.
* ``ps`` remains the fallback for a POSIX host with no ``/proc`` (macOS dev
  machines). It forks, so callers on the event loop reach this via
  ``asyncio.to_thread`` (see ``ClaudeCodeAgent.kill``).
* If neither works it degrades to killing just the process, i.e. exactly the
  previous behaviour — never worse — and says so in the log.

Deliberately dependency-free: ``psutil`` would do this in one call but is a
dev-only dependency here, and promoting it to a runtime dep for one teardown
path is not worth the supply-chain surface.
"""

import logging
import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path


logger = logging.getLogger(__name__)

_PROC = Path("/proc")

# Cap on the one `ps` call used when /proc is unavailable.
_PS_TIMEOUT_SECONDS = 5.0


def _ppid_of(pid: int) -> int | None:
    """Parent pid from /proc/<pid>/stat, or None if unreadable.

    Field 4 is ppid, but field 2 (comm) is parenthesised and may itself contain
    spaces or parens, so the split is anchored on the LAST ')' rather than
    naively on whitespace.
    """
    try:
        stat = (_PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = stat.rfind(")")
    if close == -1:
        return None
    fields = stat[close + 2 :].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _children_from_proc() -> dict[int, list[int]] | None:
    """ppid -> children, read from /proc. None when /proc is unavailable."""
    try:
        pids = [int(p.name) for p in _PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return None
    if not pids:
        return None
    children: dict[int, list[int]] = {}
    for pid in pids:
        ppid = _ppid_of(pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    return children


def _children_from_ps() -> dict[int, list[int]] | None:
    """ppid -> children via one ``ps`` call. None on any failure.

    Fallback for a POSIX host without /proc (macOS). Forks, unlike the /proc
    path, which is why async callers hop to a thread first.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)
    return children or None


def _descendants(root_pid: int) -> list[int]:
    """Every live descendant of ``root_pid``, deepest first.

    Deepest-first so a signalled parent cannot be the one left holding a live
    child. The root itself is handled separately by ``kill_process_tree``, which
    SIGSTOPs it before this snapshot is taken so it cannot spawn a child that
    the snapshot would miss.

    Returns [] when neither /proc nor ``ps`` can enumerate, which makes the
    caller degrade to a single-process kill.
    """
    children = _children_from_proc()
    if children is None:
        children = _children_from_ps()
    if children is None:
        return []
    ordered: list[int] = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        nxt: list[int] = []
        for pid in frontier:
            for kid in children.get(pid, ()):
                # Guard against a malformed/racing snapshot claiming a cycle.
                if kid not in seen:
                    seen.add(kid)
                    nxt.append(kid)
        ordered = nxt + ordered  # deepest generation ends up first
        frontier = nxt
    return ordered


def kill_process_tree(pid: int) -> int:
    """SIGKILL ``pid`` and everything it spawned. Returns how many were signalled.

    Never raises: a teardown path must not fail because a pid already exited.
    """
    if pid <= 1:
        # 0 means "every process in our group" and -1 means "everything we may
        # signal"; 1 is init. None of them is ever the intended target, and
        # os.kill(0, SIGKILL) would take out the evaluation itself.
        logger.debug("Refusing to signal sentinel pid %s", pid)
        return 0

    if os.name == "nt":  # pragma: no cover - exercised only on Windows
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=_PS_TIMEOUT_SECONDS,
                check=False,
            )
            return 1
        return 0

    with suppress(OSError):
        # Only when the target genuinely LEADS its own group. `!= getpgid(0)`
        # alone would also match a pid that merely sits in some *third party's*
        # group, and killpg would then take out processes we never spawned.
        if os.getpgid(pid) == pid and pid != os.getpgid(0):
            os.killpg(pid, signal.SIGKILL)
            return 1

    # SIGSTOP first: the root is the spawner (the agent CLI), so leaving it
    # runnable while we enumerate lets it start a Bash tool call that appears in
    # no snapshot and outlives teardown -- still writing into the tree the
    # criteria are about to read, which is the race this helper exists to close.
    # A stopped process cannot fork, so the snapshot below is complete.
    stopped = False
    with suppress(OSError):
        os.kill(pid, signal.SIGSTOP)
        stopped = True

    killed = 0
    descendants = _descendants(pid)
    if not descendants:
        logger.debug(
            "No descendants enumerated for pid %s; killing it alone (expected for a childless CLI,"
            + " but also what a host with neither /proc nor ps degrades to)",
            pid,
        )
    for child in descendants:
        if child <= 1 or child == os.getpid():
            continue
        with suppress(OSError):
            os.kill(child, signal.SIGKILL)
            killed += 1

    with suppress(OSError):
        os.kill(pid, signal.SIGKILL)
        killed += 1
    if stopped:
        # SIGKILL is delivered to a stopped process, but SIGCONT costs nothing
        # and guarantees it is reaped rather than lingering in T state if the
        # kill above raced a state change.
        with suppress(OSError):
            os.kill(pid, signal.SIGCONT)
    return killed
