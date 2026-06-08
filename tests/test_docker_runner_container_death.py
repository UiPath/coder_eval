"""Tests for the container-death diagnostics: synthetic task.json persistence.

When a container dies before its in-container orchestrator writes task.json
(e.g. torn down by the host's cancellation cleanup after a stream failure, or
killed externally), the batch layer records only an in-memory ERROR skeleton —
the task silently vanishes from every per-task consumer (dashboard, timelines).
``DockerRunner._write_synthetic_task_json`` persists a minimal ERROR task.json
in that case. These tests pin its semantics, and pin that the container keeps
daemon-side auto-removal (``--rm``) — reviewer-required so stopped containers
never accumulate on long-lived nightly VMs.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.docker_runner import DockerRunError, DockerRunner
from coder_eval.models import FileExistsCriterion, SandboxConfig, TaskDefinition


# DockerRunner targets Linux containers from POSIX hosts; see
# test_docker_runner_mounts.py for the full rationale.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


def _make_runner(run_dir: Path) -> DockerRunner:
    task = TaskDefinition(
        task_id="test-container-death",
        description="test task",
        initial_prompt="test",
        sandbox=SandboxConfig(),
        success_criteria=[FileExistsCriterion(description="test criterion", path="test.txt")],
    )
    rt = MagicMock()
    rt.task = task
    rt.run_dir = run_dir
    rt.task_file = None
    rt.variant_id = "default"
    return DockerRunner(rt)


@pytest.fixture
def run_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class TestSyntheticTaskJson:
    def test_written_when_task_json_missing(self, run_dir):
        """A dead container gets a parseable ERROR task.json with the cause."""
        runner = _make_runner(run_dir)
        target = run_dir / "task.json"
        error = DockerRunError("Container exited with code 137 without producing task.json.")

        asyncio.run(runner._write_synthetic_task_json(target, error))

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["final_status"] == "ERROR"
        assert payload["task_id"] == "test-container-death"
        assert "code 137" in payload["error_message"]

    def test_never_overwrites_existing_task_json(self, run_dir):
        """If the container won the race after all, the real result wins."""
        runner = _make_runner(run_dir)
        target = run_dir / "task.json"
        target.write_text('{"real": "result"}', encoding="utf-8")

        asyncio.run(runner._write_synthetic_task_json(target, DockerRunError("boom")))

        assert target.read_text(encoding="utf-8") == '{"real": "result"}'

    def test_atomic_write_leaves_no_tmp_file(self, run_dir):
        """tmp + os.replace: a concurrent reader never sees a torn file."""
        runner = _make_runner(run_dir)
        target = run_dir / "task.json"

        asyncio.run(runner._write_synthetic_task_json(target, DockerRunError("boom")))

        leftovers = [p.name for p in run_dir.iterdir() if p.name != "task.json"]
        assert leftovers == []

    def test_write_failure_never_raises(self, run_dir):
        """Diagnostics must never mask the DockerRunError the caller raises."""
        runner = _make_runner(run_dir)
        target = run_dir / "missing-subdir" / "task.json"  # parent doesn't exist

        # Must swallow the OSError (logged), not propagate it.
        asyncio.run(runner._write_synthetic_task_json(target, DockerRunError("boom")))

        assert not target.exists()


class TestContainerAutoRemoval:
    def test_rm_flag_retained(self, run_dir):
        """Containers stay daemon-side auto-removed (``--rm``).

        Reviewer-required on #375: replacing ``--rm`` with explicit removal
        leaves stopped containers (and their layers) accumulating on the
        long-lived nightly VM whenever a removal path is skipped (e.g. host
        SIGKILL). Post-mortem ``docker inspect`` is not worth that risk.
        """
        runner = _make_runner(run_dir)
        argv = runner._build_argv(run_dir, run_dir, container_name="cdr-test")
        assert "--rm" in argv
