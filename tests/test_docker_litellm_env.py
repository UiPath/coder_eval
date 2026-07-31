"""Tests for forwarding the LiteLLM backend into a docker-driver container.

The LiteLLM proxy runs on the HOST, so the container must (a) receive the
LITELLM_* credentials via the env allowlist and (b) reach the host via
host.docker.internal rather than an unreachable loopback address.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.docker_runner import (
    DockerRunner,
    _rewrite_loopback_for_container,
)
from coder_eval.models import DockerDriverConfig, FileExistsCriterion, SandboxConfig, TaskDefinition


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


class TestRewriteLoopbackForContainer:
    """localhost/127.0.0.1 -> host.docker.internal, preserving scheme/port/path."""

    def test_localhost_with_port(self):
        assert _rewrite_loopback_for_container("http://localhost:4000") == "http://host.docker.internal:4000"

    def test_127_0_0_1_with_port(self):
        assert _rewrite_loopback_for_container("http://127.0.0.1:4000") == "http://host.docker.internal:4000"

    def test_preserves_scheme_and_path(self):
        assert _rewrite_loopback_for_container("https://localhost:8443/v1") == "https://host.docker.internal:8443/v1"

    def test_no_port(self):
        assert _rewrite_loopback_for_container("http://localhost") == "http://host.docker.internal"

    def test_non_loopback_returns_none(self):
        assert _rewrite_loopback_for_container("http://litellm.internal:4000") is None


class TestLitellmEnvForwarding:
    """_build_argv forwards LITELLM_* and rewrites the base URL for the container."""

    def _make_runner(self) -> DockerRunner:
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig()),
            success_criteria=[FileExistsCriterion(description="c", path="x.txt")],
        )
        rt = MagicMock()
        rt.task = task
        rt.run_dir = Path(tempfile.gettempdir()) / "test_run_litellm"
        rt.task_file = None
        return DockerRunner(rt)

    def _build(self, runner: DockerRunner) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "in"
            output_dir = Path(tmp) / "out"
            input_dir.mkdir()
            output_dir.mkdir()
            return runner._build_argv(input_dir, output_dir, container_name="c")

    def test_loopback_url_rewritten_and_host_published(self, monkeypatch):
        monkeypatch.setenv("API_BACKEND", "litellm")
        monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000")
        monkeypatch.setenv("LITELLM_AUTH_TOKEN", "sk-master")
        monkeypatch.setenv("LITELLM_MODEL", "zai.glm-5")
        argv = self._build(self._make_runner())

        # Base URL forwarded as an explicit rewritten value (safe: URL, not a secret).
        assert "LITELLM_BASE_URL=http://host.docker.internal:4000" in argv
        # The bare name-only form must NOT also be forwarded (it would carry localhost).
        assert "LITELLM_BASE_URL" not in argv
        # host alias published for Linux parity.
        assert "--add-host" in argv
        assert "host.docker.internal:host-gateway" in argv
        # Credentials + model forwarded name-only (value copied by docker, stays out of argv).
        assert "LITELLM_AUTH_TOKEN" in argv
        assert "LITELLM_MODEL" in argv
        assert "sk-master" not in " ".join(argv)

    def test_cost_log_dir_bind_mounted_and_abs_path_forwarded(self, monkeypatch, tmp_path):
        # The proxy writes the per-call cost log on the HOST; the in-container join
        # reads it, so its dir is bind-mounted (ro) at the same host path and the
        # ABSOLUTE path is forwarded so it resolves to the mount inside the container.
        monkeypatch.setenv("API_BACKEND", "litellm")
        monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000")
        log_dir = tmp_path / "costs"
        log_dir.mkdir()
        cost_log = log_dir / "litellm-costs.jsonl"
        monkeypatch.setenv("LITELLM_COST_LOG", str(cost_log))
        argv = self._build(self._make_runner())

        assert f"{log_dir.resolve()}:{log_dir.resolve()}:ro" in argv  # dir mounted read-only, same path
        assert f"LITELLM_COST_LOG={cost_log.resolve()}" in argv  # absolute path → resolves to the mount
        assert "LITELLM_COST_LOG" not in argv  # not also forwarded name-only (could carry a relative value)

    def test_cost_log_skipped_when_dir_absent(self, monkeypatch, tmp_path):
        # No log dir on the host → no mount, no forward: the in-container join no-ops
        # and the run keeps static pricing (graceful, same as a local missing log).
        monkeypatch.setenv("API_BACKEND", "litellm")
        monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000")
        monkeypatch.setenv("LITELLM_COST_LOG", str(tmp_path / "nope" / "litellm-costs.jsonl"))
        argv = self._build(self._make_runner())
        assert not any(a.startswith("LITELLM_COST_LOG") for a in argv)

    def test_non_loopback_url_forwarded_name_only(self, monkeypatch):
        monkeypatch.setenv("API_BACKEND", "litellm")
        monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.setenv("LITELLM_AUTH_TOKEN", "sk-master")
        monkeypatch.setenv("LITELLM_MODEL", "zai.glm-5")
        argv = self._build(self._make_runner())

        # Forwarded name-only; value never rendered, no host alias needed.
        assert argv.count("LITELLM_BASE_URL") == 1
        assert "litellm.internal" not in " ".join(argv)
        assert "host.docker.internal:host-gateway" not in argv

    def test_no_network_skips_base_url(self, monkeypatch):
        monkeypatch.setenv("API_BACKEND", "litellm")
        monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000")
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(network="none")),
            success_criteria=[FileExistsCriterion(description="c", path="x.txt")],
        )
        rt = MagicMock()
        rt.task = task
        rt.run_dir = Path(tempfile.gettempdir()) / "test_run_litellm_nonet"
        rt.task_file = None
        argv = self._build(DockerRunner(rt))

        assert "LITELLM_BASE_URL" not in " ".join(argv)
        assert "host.docker.internal:host-gateway" not in argv
