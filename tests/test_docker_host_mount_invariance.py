"""Live docker regression guard: a docker-driver run leaves host bind mounts untouched.

The docker driver bind-mounts raw host trees into the container - the plugin /
skills-repo dirs (``agent.plugins[].path``), the task YAML's parent dir, and
absolute ``reference.file`` / ``reference.directory`` paths. A bind mount is not
a copy: any in-container chmod/chown/write lands on the REAL host filesystem.
This test runs a full ``--driver docker`` task against disposable host fixture
trees on all three mount surfaces and asserts every path comes back byte-for-byte
and metadata-identical (content hash, uid/gid, mode, symlink target, xattrs,
POSIX ACLs).

On main this test PASSES BY DESIGN: the driver mounts these trees ``:ro`` and
nothing in-container chmods them, so the comparison is trivially clean. It is
NOT vacuous - it is a guard landing ahead of any change that mutates host bind
mounts from inside the container (e.g. an isolation barrier that mounts the
grading trees rw and locks them root-0700: on native Linux with a real-root
container a single run would leave the real host checkout root-owned and mode
0700 recursively). Do not delete it because it "always passes".

Authoritative ONLY on native Linux with a real-root docker engine. On macOS /
Windows (and Docker Desktop for Linux) the daemon runs in a VM whose uid remap /
file-sharing layer absorbs in-container chown/chmod of host bind mounts, so a
mutation would be invisible to this test there - exactly why this defect class
escapes notice on dev laptops. Rootless / userns-remapped engines mask it the
same way. All of those environments skip with an explicit reason.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from coder_eval.isolation.docker_runner import DockerRunner
from coder_eval.models import ResolvedTask
from coder_eval.orchestration.task_loader import load_task
from coder_eval.utils import get_default_docker_image_tag


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        sys.platform != "linux",
        reason=(
            "host-mount metadata invariance is only authoritative on native Linux: on macOS/Windows "
            "Docker Desktop runs the daemon in a VM whose uid remap / file-sharing layer masks "
            "in-container chown/chmod of host bind mounts, so a mutation would be invisible here"
        ),
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]


def _skip_unless_native_root_engine() -> None:
    """Skip on any engine where in-container chown/chmod cannot reach host metadata."""
    try:
        res = subprocess.run(
            ["docker", "info", "--format", "{{.OperatingSystem}}|{{json .SecurityOptions}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip("docker daemon not reachable (docker info failed)")
    if res.returncode != 0:
        pytest.skip("docker daemon not running")
    operating_system, _, security_options = res.stdout.strip().partition("|")
    if "docker desktop" in operating_system.lower():
        pytest.skip(
            "Docker Desktop engine: its VM uid remap masks in-container chown/chmod of host bind "
            + "mounts, so host metadata mutation is unobservable - only a native Linux root engine "
            + "is authoritative for this test"
        )
    if "rootless" in security_options or "userns" in security_options:
        pytest.skip(
            f"docker engine remaps uids (SecurityOptions={security_options}); in-container "
            + "chown/chmod of host bind mounts is masked - only a native root engine is authoritative"
        )


@pytest.fixture
def framework_image() -> str:
    """The locally built coder-eval-agent image, or skip (with the gating above)."""
    _skip_unless_native_root_engine()
    for tag in (get_default_docker_image_tag(), "coder-eval-agent:latest"):
        probe = subprocess.run(["docker", "image", "inspect", tag], capture_output=True, timeout=30)
        if probe.returncode == 0:
            return tag
    pytest.skip("coder-eval-agent image not built (run `make docker-image`)")


def _build_fixture_tree(root: Path) -> None:
    """A disposable host tree shaped like a skills checkout: files, subdirs, a symlink,
    varied modes, and (where the filesystem supports it) a user xattr probe."""
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "SKILL.md").write_text("agent-visible docs\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "check_x.py").write_text("EXPECTED = 42\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.txt").write_text("nested content\n", encoding="utf-8")
    (root / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    os.chmod(root / "run.sh", 0o755)
    os.chmod(root / "tests" / "check_x.py", 0o640)
    os.chmod(root / "sub", 0o750)
    os.symlink("skills/SKILL.md", root / "link_to_docs")
    if hasattr(os, "setxattr"):
        # Best-effort: makes the xattr leg non-vacuous on filesystems that support it.
        with contextlib.suppress(OSError):
            os.setxattr(root / "sub" / "nested.txt", "user.coder_eval_probe", b"probe")


def _xattrs(path: Path) -> dict[str, bytes] | None:
    """All xattrs of ``path`` (not following symlinks); None where unsupported."""
    if not hasattr(os, "listxattr"):
        return None
    try:
        names = sorted(os.listxattr(path, follow_symlinks=False))
        return {name: os.getxattr(path, name, follow_symlinks=False) for name in names}
    except OSError:
        return None


def _acl(path: Path) -> str | None:
    """POSIX ACL text of ``path`` via getfacl; None where the tool/filesystem lacks it."""
    if shutil.which("getfacl") is None:
        return None
    try:
        res = subprocess.run(
            ["getfacl", "--absolute-names", "-p", str(path)], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def _snapshot_tree(root: Path) -> dict[str, dict[str, Any]]:
    """Per-path content + metadata snapshot of ``root`` (recursively, symlinks not followed)."""
    snapshot: dict[str, dict[str, Any]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        st = path.lstat()
        is_link = stat.S_ISLNK(st.st_mode)
        snapshot[str(path.relative_to(root))] = {
            "uid": st.st_uid,
            "gid": st.st_gid,
            "mode": oct(st.st_mode),
            "symlink_target": os.readlink(path) if is_link else None,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(st.st_mode) else None,
            "xattrs": _xattrs(path),
            "acl": _acl(path),
        }
    return snapshot


def _assert_snapshots_equal(label: str, before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> None:
    added = after.keys() - before.keys()
    removed = before.keys() - after.keys()
    assert not added and not removed, (
        f"{label}: docker run changed the host file set (added={added}, removed={removed})"
    )
    diffs = {
        rel: {key: (before[rel][key], after[rel][key]) for key in before[rel] if before[rel][key] != after[rel][key]}
        for rel in before
        if before[rel] != after[rel]
    }
    assert not diffs, f"{label}: docker run mutated host content/metadata (before, after): {diffs}"


async def test_docker_run_leaves_host_mounts_byte_and_metadata_identical(
    tmp_path: Path, monkeypatch, framework_image: str
) -> None:
    """A full --driver docker run must not mutate any host tree it bind-mounts.

    Covers the three auto-mounted host surfaces at once: the plugin dir
    (``agent.plugins[].path``), an absolute ``reference.directory``, and the task
    YAML's parent dir. The task uses ``agent: {type: none}`` so the run is
    credential-free and deterministic; ``pre_run`` + the file_exists criterion
    prove the container really executed the staging + orchestrator path (so a
    silently dead container can't make this pass vacuously).
    """
    # Keep the run hermetic: don't copy/mount the host ~/.claude.
    monkeypatch.setenv("CODER_EVAL_NO_CLAUDE_MOUNT", "1")

    plugin_root = tmp_path / "plugin_repo"
    reference_root = tmp_path / "reference_repo"
    task_dir = tmp_path / "task_dir"
    for root in (plugin_root, reference_root, task_dir):
        _build_fixture_tree(root)

    task_file = task_dir / "task.yaml"
    task_file.write_text(
        yaml.safe_dump(
            {
                "task_id": "host-mount-metadata-invariance",
                "description": "Regression guard: a docker run must leave host bind mounts untouched.",
                "agent": {
                    "type": "none",
                    "plugins": [{"type": "local", "path": str(plugin_root)}],
                },
                "sandbox": {
                    "driver": "docker",
                    "docker": {"image": framework_image, "network": "none"},
                },
                "reference": {"directory": str(reference_root)},
                "run_limits": {"task_timeout": 300},
                "pre_run": [{"command": "printf ran > proof.txt"}],
                "success_criteria": [
                    {
                        "type": "file_exists",
                        "path": "proof.txt",
                        "description": "pre_run wrote its file (proves the container executed the run)",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    trees = {"plugin mount": plugin_root, "reference mount": reference_root, "task-dir mount": task_dir}
    before = {label: _snapshot_tree(root) for label, root in trees.items()}

    task, source_yaml = load_task(task_file)
    rt = ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=tmp_path / "run",
        variant_id="default",
        source_yaml=source_yaml,
    )
    result = await DockerRunner(rt).run()

    # Non-vacuity: the container must have actually run the orchestrator path.
    assert result.all_criteria_passed, f"container run did not complete cleanly: {result.final_status}"

    for label, root in trees.items():
        _assert_snapshots_equal(label, before[label], _snapshot_tree(root))
