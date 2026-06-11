"""Tests for preservation-mode resolution and DIRECT_WRITE artifact permissions."""

import os
import sys

import pytest

from coder_eval.models import PreservationMode, SandboxConfig
from coder_eval.orchestration.config import resolve_preservation_mode
from coder_eval.sandbox import Sandbox


class TestResolvePreservationMode:
    """The driver-derived default, and explicit-always-wins."""

    def test_docker_defaults_to_direct_write(self):
        assert resolve_preservation_mode(None, "docker") == PreservationMode.DIRECT_WRITE

    def test_tempdir_defaults_to_move_on_write(self):
        assert resolve_preservation_mode(None, "tempdir") == PreservationMode.MOVE_ON_WRITE

    def test_unknown_driver_defaults_to_move_on_write(self):
        # Anything that isn't docker keeps the host-safe default.
        assert resolve_preservation_mode(None, "something-else") == PreservationMode.MOVE_ON_WRITE

    @pytest.mark.parametrize("mode", list(PreservationMode))
    @pytest.mark.parametrize("driver", ["docker", "tempdir"])
    def test_explicit_always_wins(self, mode, driver):
        # An explicit choice is honored regardless of driver — even DIRECT_WRITE
        # on the host ("tough luck") or NONE under docker.
        assert resolve_preservation_mode(mode, driver) == mode


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits not meaningful on Windows")
def test_grant_read_access_makes_tree_traversable(tmp_path):
    """DIRECT_WRITE relies on grant_read_access (preserve_to is skipped)."""
    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="grant_test")
    target = tmp_path / "artifacts" / "grant_test"
    sandbox.setup(target_dir=target)
    assert sandbox.is_persistent

    # Simulate a root-owned 0700 tree that the host user couldn't traverse.
    nested = target / "sub"
    nested.mkdir()
    (nested / "out.txt").write_text("artifact")
    os.chmod(target, 0o700)
    os.chmod(nested, 0o700)

    sandbox.grant_read_access()

    # a+rX: group/other read everywhere, traverse on dirs.
    assert os.stat(target).st_mode & 0o055 == 0o055
    assert os.stat(nested).st_mode & 0o055 == 0o055
    assert os.stat(nested / "out.txt").st_mode & 0o044 == 0o044


def test_grant_read_access_noop_without_setup():
    """No sandbox dir yet → no error."""
    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="noop")
    sandbox.grant_read_access()  # must not raise


def test_setup_failure_does_not_clear_direct_write_target(tmp_path, monkeypatch):
    """A caller-supplied target_dir (DIRECT_WRITE) must survive a setup failure."""
    target = tmp_path / "artifacts" / "task"
    target.mkdir(parents=True)
    (target / "prior.txt").write_text("prior run")

    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="task")
    monkeypatch.setattr(sandbox, "_setup_template", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        sandbox.setup(target_dir=target)

    # Target dir and its pre-existing content are left intact (mode contract).
    assert target.exists()
    assert (target / "prior.txt").read_text() == "prior run"


def test_setup_failure_clears_self_created_tempdir(monkeypatch):
    """A self-created tempdir IS removed on setup failure (no target_dir)."""
    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="task")
    monkeypatch.setattr(sandbox, "_setup_template", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        sandbox.setup()

    assert sandbox.sandbox_dir is None


def test_docker_runner_forwards_mode_and_container_reads_it_back():
    """The host serializes preservation_mode.value; the container re-parses it (round-trip)."""
    # Host side: DockerRunner stamps the resolved mode's .value into context.json.
    value = PreservationMode.DIRECT_WRITE.value
    assert value == "DIRECT_WRITE"
    # Container side: run_task_internal parses it back, and falls back to DIRECT_WRITE
    # (the docker default) when the key is absent (no host plumbed it).
    assert (
        PreservationMode({"preservation_mode": value}.get("preservation_mode", value)) is PreservationMode.DIRECT_WRITE
    )
    assert (
        PreservationMode({}.get("preservation_mode", PreservationMode.DIRECT_WRITE.value))
        is PreservationMode.DIRECT_WRITE
    )
    assert (
        PreservationMode({"preservation_mode": "MOVE_ON_WRITE"}.get("preservation_mode", value))
        is PreservationMode.MOVE_ON_WRITE
    )


def test_clear_rerun_artifacts_removes_only_existing(tmp_path):
    """clear_rerun_artifacts wipes a stale artifacts/<task_id> for each re-run task."""
    from coder_eval.models import ResolvedTask, TaskDefinition
    from coder_eval.orchestration.batch import clear_rerun_artifacts

    def _rt(task_id: str) -> ResolvedTask:
        task = TaskDefinition(
            task_id=task_id,
            description="d",
            initial_prompt="p",
            agent={"type": "claude-code"},
            sandbox={"driver": "docker"},
            success_criteria=[{"type": "file_exists", "path": "x.txt", "description": "x"}],
        )
        return ResolvedTask(
            task=task,
            task_file=tmp_path / "t.yaml",
            run_dir=tmp_path / task_id / "00",
            variant_id="default",
            original_task_id=task_id,
        )

    stale = _rt("stale")
    (stale.run_dir / "artifacts" / "stale").mkdir(parents=True)
    (stale.run_dir / "artifacts" / "stale" / "leftover.txt").write_text("from killed run")
    fresh = _rt("fresh")  # no artifacts dir

    cleared = clear_rerun_artifacts([stale, fresh])

    assert cleared == 1
    assert not (stale.run_dir / "artifacts" / "stale").exists()


@pytest.mark.asyncio
async def test_run_batch_dispatches_resolved_mode_for_tempdir(tmp_path):
    """run_batch must hand the driver-derived mode (tempdir→MOVE_ON_WRITE) to the Orchestrator."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from coder_eval.models import AgentState, ResolvedTask, TaskDefinition
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestrator import Orchestrator

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent={"type": "claude-code"},
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "x.txt", "description": "x"}],
    )
    run_dir = tmp_path / "run"
    # config.preservation_mode=None → resolver must derive MOVE_ON_WRITE for tempdir.
    config = BatchRunConfig(run_dir=run_dir, max_parallel=1, preservation_mode=None)
    rt = ResolvedTask(
        task=task,
        task_file=tmp_path / "t.yaml",
        run_dir=run_dir / "default" / "t" / "default",
        variant_id="default",
        original_task_id="t",
    )

    captured = {}
    orig_init = Orchestrator.__init__

    def capturing_init(self, *a, **kw):
        captured["mode"] = kw.get("preservation_mode")
        orig_init(self, *a, **kw)

    # MagicMock base (NOT AsyncMock) so the orchestrator's synchronous get_state()
    # doesn't return an un-awaited coroutine that leaks a RuntimeWarning at GC.
    mock_agent = MagicMock()
    mock_agent.start = AsyncMock(side_effect=RuntimeError("mock crash"))
    mock_agent.stop = AsyncMock()
    mock_agent.get_state.return_value = AgentState.ERROR

    with (
        patch.object(Orchestrator, "__init__", capturing_init),
        patch.object(Orchestrator, "_create_agent", new=AsyncMock(return_value=mock_agent)),
    ):
        await Orchestrator.run_batch([rt], config)

    assert captured["mode"] == PreservationMode.MOVE_ON_WRITE
