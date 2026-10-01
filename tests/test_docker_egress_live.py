"""Live docker check of the ``network: llm_only`` sidecar (real daemon, framework image, no LLM).

Proves on a real daemon what the mocked runner tests cannot: an allowlisted plain-HTTP host
is reachable through the sidecar, a denied host gets the proxy's 403, the per-task network
carries the ``inhibit_ipv4`` option (no host-reachable gateway), and teardown leaves nothing
labelled behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from coder_eval.isolation import docker_runner as dr
from coder_eval.isolation.egress import EGRESS_LABEL, EGRESS_LOG_HEADER, NO_PROXY_HOSTS, PROXY_URL, egress_scope
from coder_eval.utils import get_default_docker_image_tag


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", check=False, timeout=timeout
    )


@pytest.fixture
def framework_image() -> str:
    try:
        if _docker("info", timeout=15).returncode != 0:
            pytest.skip("docker daemon not running")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip("docker daemon not running")
    for image in (get_default_docker_image_tag(), "coder-eval-agent:latest"):
        if _docker("image", "inspect", image).returncode == 0:
            return image
    pytest.skip("framework image not built (make docker-image)")


def _curl(network: str, image: str, url: str) -> subprocess.CompletedProcess[str]:
    proxy_env: list[str] = []
    for name in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        proxy_env += ["-e", f"{name}={PROXY_URL}"]
    proxy_env += ["-e", f"no_proxy={NO_PROXY_HOSTS}"]
    return _docker(
        "run",
        "--rm",
        "--network",
        network,
        *proxy_env,
        "--entrypoint",
        "curl",
        image,
        "-sS",
        "-m",
        "20",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        url,
    )


async def test_allowlisted_http_works_denied_https_fails_and_nothing_leaks(framework_image: str, tmp_path: Path):
    name = f"coder-eval-egress-live-{uuid.uuid4().hex[:8]}"
    egress_dir = tmp_path / "egress"
    await asyncio.to_thread(dr._prepare_egress_dir, egress_dir)
    heartbeat = tmp_path / dr.HEARTBEAT_FILENAME
    heartbeat.touch()
    heartbeat_task = asyncio.create_task(dr._heartbeat_loop(heartbeat))
    log_path = tmp_path / "docker.log"
    try:
        async with egress_scope(
            container_name=name,
            image=framework_image,
            egress_dir=egress_dir,
            heartbeat=heartbeat,
            stale_seconds=dr.HEARTBEAT_STALE_SECONDS,
            targets=["example.com:80"],
            log_path=log_path,
        ) as handle:
            options = await asyncio.to_thread(_docker, "network", "inspect", "-f", "{{json .Options}}", handle.network)
            assert '"com.docker.network.bridge.inhibit_ipv4":"true"' in options.stdout
            allowed = await asyncio.to_thread(_curl, handle.network, framework_image, "http://example.com/")
            assert allowed.returncode == 0, allowed.stderr
            assert allowed.stdout == "200"
            denied = await asyncio.to_thread(_curl, handle.network, framework_image, "https://example.com/")
            assert denied.returncode == 56, denied.stderr
            assert "403" in denied.stderr
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    log = log_path.read_text(encoding="utf-8")
    assert EGRESS_LOG_HEADER in log
    assert "ALLOW example.com:80 GET" in log
    assert "DENY example.com:443 CONNECT" in log
    containers = _docker("ps", "-a", "-q", "--filter", f"label={EGRESS_LABEL}", "--filter", f"name={name}")
    networks = _docker("network", "ls", "-q", "--filter", f"label={EGRESS_LABEL}", "--filter", f"name={name}")
    assert containers.stdout.strip() == ""
    assert networks.stdout.strip() == ""
