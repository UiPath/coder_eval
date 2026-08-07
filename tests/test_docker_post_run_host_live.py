"""Live docker e2e sketch for HARNESS-OUTSIDE ``post_run`` (host-side teardown).

Gated ``-m live`` (real docker daemon + the coder-eval-agent image), so it is
EXCLUDED from ``make test``. Proves end-to-end that under ``--driver docker``:
  - the container runs the agent + ``pre_run`` (the seed lands in the workspace),
  - ``post_run`` teardown runs HOST-side after the container exits, with
    ``cwd`` = the copied-out workspace (so it sees the seed the agent produced),
  - the seed round-trips: it is present in the copied-out workspace the host
    ``post_run`` reads (not only inside the container).

This is a SKETCH intended to be fleshed out when running on a Linux host with a
daemon; it is never run in CI's non-live suite.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest


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


@pytest.mark.asyncio
async def test_post_run_teardown_runs_host_side_over_copied_out_workspace(tmp_path):
    """A docker task whose agent writes ``seed.json`` and whose ``post_run`` reads
    it (host-side) proves the teardown ran on the host over the copied-out
    workspace, and that the seed round-tripped out of the container."""
    if not _docker_daemon_up():
        pytest.skip("docker daemon not running")

    from datetime import datetime
    from pathlib import Path

    from coder_eval.isolation.docker_runner import run_post_run_on_host
    from coder_eval.models import (
        EvaluationResult,
        FinalStatus,
        PostRunCommand,
        ResolvedTask,
        SandboxConfig,
        TaskDefinition,
    )

    # Stand-in for the copied-out workspace the DockerRunner produces at
    # rt.run_dir/artifacts/<id>. In a full e2e this is populated by an actual
    # `coder-eval run --driver docker` of a task whose prompt writes seed.json;
    # here we assert the host teardown step over that directory.
    artifacts = tmp_path / "run" / "artifacts" / "t"
    artifacts.mkdir(parents=True)
    (artifacts / "seed.json").write_text('{"resource_id": "live-123"}', encoding="utf-8")

    task_dir = tmp_path / "taskdir"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text("x", encoding="utf-8")
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="write seed.json",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "seed.json"}],
        # Teardown reads the seed the agent produced (the real cleanup.py pattern:
        # read seed.json from CWD to know which cloud resource to delete).
        post_run=[
            PostRunCommand(command="python3 -c \"import json; print(json.load(open('seed.json'))['resource_id'])\"")
        ],
    )
    rt = ResolvedTask(
        task=task,
        task_file=task_dir / "task.yaml",
        run_dir=tmp_path / "run",
        variant_id="v",
        source_yaml="raw",
    )
    result = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type="claude-code",
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        # Container-absolute path is re-rooted onto rt.run_dir by _resolve_artifacts_dir.
        sandbox_path="/work/output/artifacts/t",
        success_criteria_results=[],
    )

    await run_post_run_on_host(result, rt)

    # Teardown ran host-side over the copied-out workspace and saw the seed.
    assert result.post_run_results, "post_run teardown did not run host-side"
    assert result.post_run_results[0].stdout.strip() == "live-123"
    # And the record persisted to task.json.
    assert (Path(rt.run_dir) / "task.json").is_file()


@pytest.mark.asyncio
async def test_host_pre_run_seed_reaches_the_agent(tmp_path):
    """SKETCH (fill in on a Linux host with a daemon + image): a full
    ``coder-eval run --driver docker`` of a task whose HOST-side ``pre_run``
    writes ``seed.json`` into the staging dir must land that seed in the agent's
    initial workspace inside the container.

    Wiring: write a task YAML with ``sandbox.driver: docker``, a
    ``pre_run: [{command: "printf '{\"k\":1}' > seed.json"}]`` (runs host-side
    into the staging dir), and a criterion / prompt that asserts the agent could
    read ``seed.json``. Run it via the CLI (or ``run_batch``) against the real
    image, then assert the copied-out workspace + the agent trajectory both show
    the seed. Deliberately not executed in the non-live suite.
    """
    if not _docker_daemon_up():
        pytest.skip("docker daemon not running")
    pytest.skip("live e2e sketch — flesh out on a Linux host with the coder-eval-agent image")
