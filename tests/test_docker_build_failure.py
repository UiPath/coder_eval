"""Build-failure observability: a failed `docker build` must not vanish.

A task image is built before run_dir / docker.log / task.json exist, so a build
failure used to leave an empty result directory with no status and no log. These
tests assert the fix: the build log is captured to docker.log and a synthetic
``BUILD_FAILED`` task.json is written, and the batch layer records BUILD_FAILED
(not generic ERROR) at the run level. Hermetic — docker is never invoked.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation import docker_runner as dr
from coder_eval.isolation.docker_runner import DockerBuildError, DockerRunner, build_error_result
from coder_eval.models.criteria import FileExistsCriterion
from coder_eval.models.enums import FinalStatus
from coder_eval.models.sandbox import DockerDriverConfig, SandboxConfig
from coder_eval.models.tasks import TaskDefinition


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="DockerRunner targets POSIX hosts")


def _make_runner(run_dir: Path) -> DockerRunner:
    task = TaskDefinition(
        task_id="suri",
        description="t",
        initial_prompt="do",
        # A concrete agent type keeps docker.agent_isolation (default true)
        # active instead of downgrading with a warning before the build runs;
        # this test covers build-failure observability, not that resolution.
        agent={"type": "claude-code"},
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(image="x:1", dockerfile_path="/df")),
        success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
    )
    rt = MagicMock()
    rt.task = task
    rt.task_file = None
    rt.run_dir = run_dir
    rt.variant_id = "default"
    rt.replicate_index = 0
    return DockerRunner(rt)


# --- build_error_result honors BUILD_FAILED + carries the log ----------------- #
def test_build_error_result_build_failed_status_and_log():
    rt = MagicMock()
    rt.task = MagicMock(task_id="t")
    rt.variant_id = "default"
    exc = DockerBuildError("nope", build_log="step 3\nERROR: dnf: not found\nexit 127\n")
    res = build_error_result(rt, exc, status=FinalStatus.BUILD_FAILED)
    assert res.final_status == FinalStatus.BUILD_FAILED
    assert res.final_status.category == "error"
    assert "dnf: not found" in (res.error_log_tail or "")
    assert res.task_description == "Docker image build failed"


# --- run() records the failure instead of leaving an empty dir ---------------- #
def test_run_records_build_failure_to_docker_log_and_task_json(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    runner = _make_runner(run_dir)

    monkeypatch.setattr(dr, "_preflight", lambda: None)

    def boom():
        raise DockerBuildError(
            "Failed to build Docker image from /df: dnf: not found",
            build_log="Step 3/5 : RUN dnf -y install foo\n/bin/sh: dnf: not found\nERROR: exit code: 127\n",
        )

    monkeypatch.setattr(runner, "_build_image", boom)

    with pytest.raises(DockerBuildError):
        asyncio.run(runner.run())

    # docker.log captured the build output
    log = run_dir / "docker.log"
    assert log.is_file() and "dnf: not found" in log.read_text()

    # synthetic task.json written with BUILD_FAILED status
    tj = run_dir / "task.json"
    assert tj.is_file()
    data = json.loads(tj.read_text())
    assert data["final_status"] == "BUILD_FAILED"


# --- batch maps DockerBuildError -> BUILD_FAILED at the run level -------------- #
def test_batch_error_result_maps_build_failure():
    from coder_eval.orchestration.batch import _create_error_task_result

    tr = _create_error_task_result(
        Path("x.yaml"),
        DockerBuildError("boom", build_log="..."),
        task_id="t",
        variant_id="default",
    )
    assert tr.result.final_status == FinalStatus.BUILD_FAILED

    tr2 = _create_error_task_result(Path("x.yaml"), RuntimeError("other"), task_id="t", variant_id="default")
    assert tr2.result.final_status == FinalStatus.ERROR


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
