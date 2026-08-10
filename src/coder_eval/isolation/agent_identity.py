"""Linux identity helpers for the in-container evaluated agent."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

from coder_eval.models import AGENT_GID, AGENT_UID


AGENT_ISOLATION_ENV = "CODER_EVAL_AGENT_ISOLATION"
AGENT_KILL_TIMEOUT_SECONDS = 2.0


def agent_isolation_enabled() -> bool:
    """Whether the Docker host requested the UID/GID agent boundary."""

    return os.environ.get(AGENT_ISOLATION_ENV) == "1"


def require_isolation_runtime() -> None:
    """Fail closed unless Linux root can perform the requested UID drop."""

    if not agent_isolation_enabled():
        return
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("agent UID/GID isolation requires a native Linux container running the harness as root")


def grant_agent_workspace(path: Path) -> None:
    """Give the unprivileged identity ownership of a generated workspace.

    The caller may pass only disposable sandbox content, never a raw host source
    checkout. Symlinks are chowned without following their targets.
    """

    if not agent_isolation_enabled():
        return
    require_isolation_runtime()
    if not path.is_absolute() or not path.exists():
        raise RuntimeError(f"agent workspace must be an existing absolute path: {path}")

    failures: list[str] = []
    chown = getattr(os, "chown", None)
    if chown is None:
        raise RuntimeError("agent UID/GID isolation requires os.chown")

    def grant(candidate: Path) -> None:
        try:
            chown(candidate, AGENT_UID, AGENT_GID, follow_symlinks=False)
        except OSError as exc:
            failures.append(f"{candidate}: {exc}")

    grant(path)
    if path.is_dir() and not path.is_symlink():
        for root_name, dirnames, filenames in os.walk(path, followlinks=False):
            root = Path(root_name)
            for name in (*dirnames, *filenames):
                grant(root / name)

    if failures:
        detail = "; ".join(failures[:5])
        raise RuntimeError(f"failed to grant generated workspace to agent uid {AGENT_UID}: {detail}")


def _agent_pids() -> list[int]:
    """Return processes whose real/effective/saved/fs UID includes the agent."""

    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status_lines = (entry / "status").read_text(encoding="utf-8").splitlines()
            uid_line = next(line for line in status_lines if line.startswith("Uid:"))
            uids = [int(value) for value in uid_line.split()[1:]]
        except (OSError, StopIteration, ValueError):
            continue
        if AGENT_UID in uids:
            pids.append(int(entry.name))
    return pids


def _signal_agent_pids(pids: list[int], sig: signal.Signals) -> None:
    for pid in pids:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, sig)


def terminate_agent_processes() -> None:
    """Terminate and verify removal of every process owned by the agent UID."""

    if not agent_isolation_enabled():
        return
    require_isolation_runtime()

    _signal_agent_pids(_agent_pids(), signal.SIGTERM)
    time.sleep(0.1)

    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    deadline = time.monotonic() + AGENT_KILL_TIMEOUT_SECONDS
    while pids := _agent_pids():
        _signal_agent_pids(pids, sigkill)
        if time.monotonic() >= deadline:
            residual = _agent_pids()
            if residual:
                raise RuntimeError(f"agent UID {AGENT_UID} still owns processes after SIGKILL: {residual[:10]}")
            return
        time.sleep(0.02)
