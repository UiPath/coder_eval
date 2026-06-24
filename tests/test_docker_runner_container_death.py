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
from coder_eval.models import EvaluationResult, FileExistsCriterion, FinalStatus, SandboxConfig, TaskDefinition


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

        # Round-trips through the SAME parse every downstream consumer uses
        # (batch.py / reports.py / reports_stats.py) — not just a raw key check —
        # so a schema change that broke validation on the synthetic record fails here.
        parsed = EvaluationResult.model_validate_json(target.read_text(encoding="utf-8"))
        assert parsed.final_status == FinalStatus.ERROR
        assert parsed.task_id == "test-container-death"
        payload = json.loads(target.read_text(encoding="utf-8"))
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


class TestMalformedTaskJson:
    """A present-but-malformed task.json degrades to a synthetic ERROR record.

    Mirrors the missing-file branch: a stale ``:latest`` image producing a
    schema-skewed task.json (the version checks only warn), or a truncated/torn
    write, must not surface as an uncaught ``ValidationError``/``JSONDecodeError``.
    Instead the runner preserves the original aside (``task.json.malformed``),
    persists a parseable synthetic ERROR task.json, and returns a ``DockerRunError``
    for the caller to raise. Tests drive the container-free
    ``_handle_malformed_task_json`` seam directly.
    """

    @staticmethod
    def _malformed_exc(text: str) -> Exception:
        """Parse ``text`` as an EvaluationResult and return the raised ValueError."""
        from coder_eval.models import EvaluationResult

        try:
            EvaluationResult.model_validate_json(text)
        except ValueError as exc:
            return exc
        raise AssertionError("expected EvaluationResult parse to fail")

    def test_schema_skew_degrades(self, run_dir):
        """Valid JSON but wrong schema → synthetic ERROR + preserved sidecar + DockerRunError."""
        runner = _make_runner(run_dir)
        task_json = run_dir / "task.json"
        original = '{"task_id": 123, "final_status": "NOPE"}'
        task_json.write_text(original, encoding="utf-8")
        log_path = run_dir / "docker.log"
        exc = self._malformed_exc(original)

        returned = asyncio.run(runner._handle_malformed_task_json(task_json, log_path, exc))

        assert isinstance(returned, DockerRunError)
        # Synthetic ERROR task.json now present and round-trips through the real
        # EvaluationResult parse every downstream consumer relies on.
        parsed = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
        assert parsed.final_status == FinalStatus.ERROR
        # Original preserved byte-identically in the sidecar.
        sidecar = run_dir / "task.json.malformed"
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8") == original

    def test_truncated_degrades(self, run_dir):
        """Unterminated JSON → same degraded outcome."""
        runner = _make_runner(run_dir)
        task_json = run_dir / "task.json"
        original = '{"task_id": "x"'
        task_json.write_text(original, encoding="utf-8")
        log_path = run_dir / "docker.log"
        exc = self._malformed_exc(original)

        returned = asyncio.run(runner._handle_malformed_task_json(task_json, log_path, exc))

        assert isinstance(returned, DockerRunError)
        parsed = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
        assert parsed.final_status == FinalStatus.ERROR
        sidecar = run_dir / "task.json.malformed"
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8") == original

    def test_error_message_names_path(self, run_dir):
        """The returned DockerRunError references the malformed path and log."""
        runner = _make_runner(run_dir)
        task_json = run_dir / "task.json"
        task_json.write_text("not json", encoding="utf-8")
        log_path = run_dir / "docker.log"
        exc = self._malformed_exc("not json")

        returned = asyncio.run(runner._handle_malformed_task_json(task_json, log_path, exc))

        assert str(task_json) in str(returned)
        assert str(log_path) in str(returned)

    def test_move_aside_failure_is_swallowed(self, run_dir, monkeypatch):
        """A failed move-aside is logged and swallowed; the DockerRunError still returns (degrade, don't crash)."""
        runner = _make_runner(run_dir)
        task_json = run_dir / "task.json"
        original = "still malformed"
        task_json.write_text(original, encoding="utf-8")
        log_path = run_dir / "docker.log"
        exc = self._malformed_exc("not json")

        def _boom(*_args, **_kwargs):
            raise OSError("cannot move")

        # String target avoids importing docker_runner under both `import` and
        # `from ... import` (CodeQL py/import-and-import-from).
        monkeypatch.setattr("coder_eval.isolation.docker_runner.os.replace", _boom)

        returned = asyncio.run(runner._handle_malformed_task_json(task_json, log_path, exc))

        # Error still returned for the caller to raise (no crash).
        assert isinstance(returned, DockerRunError)
        # Move failed → original malformed file stays put; synthetic write no-ops
        # (never-overwrite guard) — strictly no worse than today, and now warned.
        assert task_json.read_text(encoding="utf-8") == original
        assert not (run_dir / "task.json.malformed").exists()

    def test_stale_sidecar_overwritten(self, run_dir):
        """A leftover .malformed from a prior run is atomically overwritten (no FileExistsError)."""
        runner = _make_runner(run_dir)
        task_json = run_dir / "task.json"
        original = "current malformed bytes"
        task_json.write_text(original, encoding="utf-8")
        sidecar = run_dir / "task.json.malformed"
        sidecar.write_text("stale prior-run bytes", encoding="utf-8")
        log_path = run_dir / "docker.log"
        exc = self._malformed_exc("not json")

        asyncio.run(runner._handle_malformed_task_json(task_json, log_path, exc))

        assert sidecar.read_text(encoding="utf-8") == original


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
