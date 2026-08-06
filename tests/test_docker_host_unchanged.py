"""Detector A: a docker run must leave every host file byte-for-byte AND
metadata-identical (the COPY/PRUNE + GRADE-OUTSIDE exit criterion).

The redesign's whole purpose is that the harness never chmods/chowns a host
bind mount and the agent never gets rw access to a host original — so a run
cannot mutate the host checkout (the fatal bug of the superseded uid-barrier
approach, which chmod-0700'd raw host mounts and corrupted the host on Linux).

Two variants:

- **Daemon-less proxy (always runs, CI-cheap):** run the real staging +
  ``_build_argv`` (no container) and assert NO ``-v`` mount SOURCE is a host
  original mounted rw — only staging copies, ``/work/input`` (:ro), and
  ``/work/output``. This proves "there is no rw host mount to mutate" without a
  daemon and is the load-bearing CI sensor.

- **Daemon-gated real run (``-m live``):** snapshot content hash + ``os.lstat``
  metadata (mode/uid/gid) + symlink targets of the host skills / task dir /
  reference BEFORE a real ``--driver docker`` run, run it, and assert identical
  after. This is the exit-criterion sensor. NOTE: bind-mount host-mutation is
  only authoritative on native Linux overlayfs; on macOS/Windows Docker Desktop
  the uid-remap masks it, so the real-run variant is Linux-authoritative.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.docker_runner import DockerRunner
from coder_eval.models import (
    CONTAINER_INPUT_DIR,
    CONTAINER_OUTPUT_DIR,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
)


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


def _docker_daemon_up() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---- Daemon-less proxy: no rw host-original mount ---------------------------


def _make_runner(tmp_path: Path, plugin: Path, task_file: Path):
    from coder_eval.models import ReferenceSource

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code", "plugins": [{"type": "local", "path": str(plugin)}]},
        reference=ReferenceSource(directory=str(tmp_path / "refdir")),
        success_criteria=[FileExistsCriterion(description="c", path="o.txt")],
    )
    rt = MagicMock()
    rt.task = task
    rt.run_dir = tmp_path / "run"
    rt.task_file = task_file
    return DockerRunner(rt)


def test_no_host_original_rw_mount_daemonless(tmp_path):
    """The load-bearing CI sensor: every -v mount is a staging copy, /work/input
    (:ro), /work/output, or a :ro auto-mount — never a host original mounted rw.
    A host original mounted rw is the only way a run could mutate the host."""
    plugin = tmp_path / "skills_repo"
    (plugin / "skills").mkdir(parents=True)
    (plugin / "skills" / "SKILL.md").write_text("doc", encoding="utf-8")
    (plugin / "tests").mkdir()
    (plugin / "tests" / "check_x.py").write_text("x", encoding="utf-8")
    (tmp_path / "refdir").mkdir()
    task_file = tmp_path / "taskdir" / "task.yaml"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("x", encoding="utf-8")

    runner = _make_runner(tmp_path, plugin, task_file)
    staging = tmp_path / "staging"
    staging.mkdir()
    runner._prepare_host_mounts(staging)
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    argv = runner._build_argv(input_dir, output_dir, container_name="c", image="img")

    host_originals = {
        str(plugin.resolve()),
        str((tmp_path / "refdir").resolve()),
        str(task_file.parent.resolve()),
    }
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v" and i + 1 < len(argv)]
    for m in mounts:
        parts = m.split(":")
        src, dst = parts[0], (parts[1] if len(parts) >= 2 else "")
        mode = parts[2] if len(parts) == 3 else ""
        # A host original must NEVER be a mount source, in any mode.
        assert src not in host_originals, f"host original mounted (would be mutable-reachable): {m}"
        # Any rw (mode-less) mount must be /work/output or a staging copy.
        if mode != "ro":
            is_output = dst == CONTAINER_OUTPUT_DIR
            is_staging = str(staging) in src
            assert is_output or is_staging, f"rw mount of a non-staging source: {m}"
    # Sanity: input is present :ro.
    assert any(m == f"{input_dir.resolve()}:{CONTAINER_INPUT_DIR}:ro" for m in mounts)


# ---- Daemon-gated real run: host byte + metadata identical ------------------


def _snapshot(root: Path) -> dict[str, tuple]:
    """Content hash + lstat metadata + symlink target for every entry under root
    AND the root itself.

    Metadata = (mode, uid, gid, mtime). ``mtime`` catches a read-then-rewrite to
    identical bytes; the root itself is included because ``chmod 0700`` on the
    bind-mount ROOT (the superseded approach's exact failure mode) does not
    necessarily touch any child, so a child-only walk would miss it.
    """
    snap: dict[str, tuple] = {}
    # Include the mount root itself (rglob("*") excludes it).
    for p in [root, *sorted(root.rglob("*"))]:
        rel = "." if p == root else str(p.relative_to(root))
        st = p.lstat()
        meta = (st.st_mode, st.st_uid, st.st_gid, st.st_mtime_ns)
        if p.is_symlink():
            snap[rel] = ("symlink", os.readlink(p), meta)
        elif p.is_dir():
            snap[rel] = ("dir", None, meta)
        else:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            snap[rel] = ("file", digest, meta)
    return snap


def _base_image_present() -> bool:
    from coder_eval.utils import get_default_docker_image_tag

    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", get_default_docker_image_tag()],
                capture_output=True,
                timeout=15,
            ).returncode
            == 0
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


@pytest.mark.live
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_host_unchanged_after_real_docker_run(tmp_path):
    """EXIT-CRITERION sensor (Linux-authoritative): a real docker run leaves the
    host skills / task-dir / reference byte-for-byte AND metadata-identical.

    Directly catches the superseded approach's failure mode (chmod-0700 on a raw
    host bind mount corrupting the host checkout). On macOS/Windows Docker Desktop
    the uid-remap masks host mutation, so this is authoritative only on native
    Linux overlayfs — but the assertion still runs there and proves the mechanism.
    Skips cleanly when the daemon or the base image is absent.
    """
    import asyncio

    from coder_eval.isolation.docker_runner import DockerRunner
    from coder_eval.models import ReferenceSource
    from coder_eval.orchestration.config import resolve_preservation_mode

    if not _docker_daemon_up():
        pytest.skip("docker daemon not running")
    if not _base_image_present():
        pytest.skip("coder-eval-agent base image not built (run `make docker-image`)")

    # Host material the agent must never mutate: a skills-repo-shaped plugin, the
    # task dir, and a reference dir.
    skills_repo = tmp_path / "skills_repo"
    (skills_repo / "skills" / "demo").mkdir(parents=True)
    (skills_repo / "skills" / "demo" / "SKILL.md").write_text("# demo skill\n", encoding="utf-8")
    (skills_repo / "tests").mkdir()
    (skills_repo / "tests" / "check_demo.py").write_text("assert True\n", encoding="utf-8")
    ref_dir = tmp_path / "refdir"
    ref_dir.mkdir()
    (ref_dir / "golden.txt").write_text("golden\n", encoding="utf-8")
    task_dir = tmp_path / "taskdir"
    task_dir.mkdir()
    task_file = task_dir / "task.yaml"
    task_file.write_text("placeholder\n", encoding="utf-8")

    task = TaskDefinition(
        task_id="host-unchanged-probe",
        description="host-unchanged probe",
        initial_prompt="Create a file named app.py that prints hello.",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code", "plugins": [{"type": "local", "path": str(skills_repo)}]},
        reference=ReferenceSource(directory=str(ref_dir)),
        success_criteria=[FileExistsCriterion(description="c", path="app.py")],
    )
    rt = MagicMock()
    rt.task = task
    rt.run_dir = tmp_path / "run"
    rt.task_file = task_file
    rt.variant_id = None
    rt.replicate_index = 0
    rt.config_lineage = {}
    rt.source_yaml = task_file.read_text(encoding="utf-8")

    before = {
        "skills": _snapshot(skills_repo),
        "ref": _snapshot(ref_dir),
        "task": _snapshot(task_dir),
    }

    driver = "docker"
    runner = DockerRunner(rt, preservation_mode=resolve_preservation_mode(None, driver))
    try:
        asyncio.run(runner.run())
    except Exception as exc:  # a run failure must not mask the host-mutation check
        # We still assert host-unchanged below; a failed agent run is fine as long
        # as it didn't touch the host.
        print(f"docker run raised (host-unchanged still asserted): {exc}")

    after = {
        "skills": _snapshot(skills_repo),
        "ref": _snapshot(ref_dir),
        "task": _snapshot(task_dir),
    }
    for key in before:
        assert before[key] == after[key], f"host {key} was mutated by the docker run (byte/metadata diff)"
