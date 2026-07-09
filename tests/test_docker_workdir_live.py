"""Live docker integration for WORKDIR alignment (`sandbox.docker.working_dir`).

Gated: needs a real docker daemon. Verifies the `_resolve_workspace_dir("auto", …)`
inspect path against a real image — this is the one thing the mocked unit test
(`tests/test_docker_runner_mounts.py::TestWorkspaceDir`) cannot prove: that the
`docker image inspect --format '{{.Config.WorkingDir}}'` command shape actually
reads the image's declared WORKDIR. The orchestrator run-in-place + capture-out
logic is covered by unit tests in test_orchestrator.py / test_sandbox.py.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid

import pytest

from coder_eval.isolation.docker_runner import _resolve_workspace_dir


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]


def _docker_daemon_up() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


@pytest.fixture
def workdir_image():
    """Build a throwaway image whose declared WORKDIR is /app/workspace.

    Built FROM the local coder-eval-agent base (no network pull); one extra
    WORKDIR layer makes the inspected WorkingDir deterministic regardless of the
    base's own default.
    """
    if not _docker_daemon_up():
        pytest.skip("docker daemon not running")
    tag = f"coder-eval-workdir-test-{uuid.uuid4().hex[:8]}:latest"
    dockerfile = "FROM coder-eval-agent:latest\nWORKDIR /app/workspace\n"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "-"],
        input=dockerfile,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if build.returncode != 0:
        pytest.skip(f"fixture image build failed (base image likely absent): {build.stderr[-300:]}")
    try:
        yield tag
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=60)


def test_resolve_auto_reads_real_image_workdir(workdir_image):
    """`auto` detects the image's declared WORKDIR via a real `docker inspect`."""
    assert _resolve_workspace_dir("auto", workdir_image) == "/app/workspace"


def test_resolve_concrete_still_passthrough_live(workdir_image):
    """A concrete path is returned verbatim (no inspect) even with a real image."""
    assert _resolve_workspace_dir("/root", workdir_image) == "/root"
