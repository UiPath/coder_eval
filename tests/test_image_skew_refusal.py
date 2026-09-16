"""The image version preflight: refuse a skewed image before a billed container starts."""

from __future__ import annotations

import logging
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.config import settings
from coder_eval.isolation import docker_runner as dr
from coder_eval.isolation.docker_runner import DockerRunError, DockerRunner
from coder_eval.models import DockerDriverConfig, FileExistsCriterion, SandboxConfig, TaskDefinition


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")

HOST_VERSION = "9.9.9"


@pytest.fixture(autouse=True)
def _host_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda _name: HOST_VERSION)
    monkeypatch.setattr(settings, "allow_image_skew", False)


def _inspect_prints(monkeypatch: pytest.MonkeyPatch, label: str | None) -> None:
    """Fake `docker image inspect`: print ``label``, or fail when it is None."""

    def _run(argv, *args, **kwargs):
        if label is None:
            raise subprocess.CalledProcessError(1, argv, stderr="No such image")
        return subprocess.CompletedProcess(argv, 0, f"{label}\n", "")

    monkeypatch.setattr(dr.subprocess, "run", _run)


def _runner(tmp_path: Path, *, dockerfile_path: str | None = None) -> DockerRunner:
    task = TaskDefinition(
        task_id="skew",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(dockerfile_path=dockerfile_path)),
        success_criteria=[FileExistsCriterion(description="c", path="t.txt")],
    )
    rt = MagicMock()
    rt.task = task
    rt.run_dir = tmp_path / "run"
    rt.replicate_index = 0
    rt.variant_id = "default"
    rt.config_lineage = {}
    rt.source_yaml = "# task"
    rt.task_file = None
    return DockerRunner(rt)


_LAUNCH_SENTINEL = "docker run is not under test"


async def _run_until_launch(
    runner: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[tuple[object, ...]], BaseException]:
    """Drive ``run()`` with the daemon checks faked; return the launches attempted and the error raised."""
    monkeypatch.setenv("CODER_EVAL_NO_CLAUDE_MOUNT", "1")
    monkeypatch.setattr(dr, "_preflight", lambda: None)
    launches: list[tuple[object, ...]] = []

    async def _fake_exec(*argv, **kwargs):
        launches.append(argv)
        raise FileNotFoundError(_LAUNCH_SENTINEL)

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    with pytest.raises((DockerRunError, FileNotFoundError)) as exc:
        await runner.run()
    return launches, exc.value


async def test_a_version_mismatch_refuses_before_the_container_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inspect_prints(monkeypatch, "0.0.1")

    with pytest.raises(DockerRunError) as exc:
        dr._preflight_image_contract("img", None)
    message = str(exc.value)
    assert "0.0.1" in message and HOST_VERSION in message
    assert "make docker-image" in message
    assert "ALLOW_IMAGE_SKEW=1" in message, "the operator must be able to recover from the error text alone"

    launches, error = await _run_until_launch(_runner(tmp_path), monkeypatch)
    assert launches == [], "no container may start for a skewed image"
    assert isinstance(error, DockerRunError) and "but the host runs" in str(error)


def test_the_escape_hatch_downgrades_the_mismatch_to_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _inspect_prints(monkeypatch, "0.0.1")
    monkeypatch.setattr(settings, "allow_image_skew", True)

    with caplog.at_level(logging.WARNING, logger=dr.logger.name):
        dr._preflight_image_contract("img", None)

    assert any("0.0.1" in r.getMessage() and "ALLOW_IMAGE_SKEW" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("label", ["", "<no value>"])
def test_a_missing_label_is_refused_for_a_configured_image(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    _inspect_prints(monkeypatch, label)
    with pytest.raises(DockerRunError, match=r"Image img is not a coder-eval runtime image"):
        dr._preflight_image_contract("img", None)


def test_a_missing_label_is_refused_for_a_dockerfile_image_with_the_from_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The BYOD author's only guidance: name the base to build FROM and the doc."""
    _inspect_prints(monkeypatch, "")
    dockerfile = tmp_path / "Dockerfile"
    with pytest.raises(DockerRunError) as exc:
        dr._preflight_image_contract("coder-eval-task-x:built", dockerfile)
    message = str(exc.value)
    assert f"Image built from {dockerfile}" in message
    assert "FROM coder-eval-agent" in message
    assert "docs/DOCKER_ISOLATION.md" in message


def test_the_escape_hatch_does_not_excuse_a_missing_label(monkeypatch: pytest.MonkeyPatch) -> None:
    _inspect_prints(monkeypatch, "")
    monkeypatch.setattr(settings, "allow_image_skew", True)
    with pytest.raises(DockerRunError, match=r"org\.coder-eval\.version"):
        dr._preflight_image_contract("img", None)


async def test_a_dockerfile_image_is_now_version_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM coder-eval-agent:0.0.1\n", encoding="utf-8")
    runner = _runner(tmp_path, dockerfile_path=str(dockerfile))
    monkeypatch.setattr(runner, "_build_image", lambda: "coder-eval-task-skew:built")
    _inspect_prints(monkeypatch, "0.0.1")

    launches, error = await _run_until_launch(runner, monkeypatch)
    assert launches == []
    assert isinstance(error, DockerRunError) and "but the host runs" in str(error)


def test_a_matching_version_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _inspect_prints(monkeypatch, HOST_VERSION)
    dr._preflight_image_contract("img", None)


def test_an_unknown_host_version_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A source checkout: skew is not computable, which is not a refusal."""

    def _no_package(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("importlib.metadata.version", _no_package)
    _inspect_prints(monkeypatch, "0.0.1")

    with caplog.at_level(logging.WARNING, logger=dr.logger.name):
        dr._preflight_image_contract("img", None)

    assert any("cannot be checked against the host" in r.getMessage() for r in caplog.records)


async def test_an_inspect_failure_still_falls_through_to_docker_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing local image is `docker run`'s error to report, not the preflight's."""
    _inspect_prints(monkeypatch, None)
    launches, error = await _run_until_launch(_runner(tmp_path), monkeypatch)
    assert len(launches) == 1
    assert str(error) == _LAUNCH_SENTINEL
