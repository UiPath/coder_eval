"""Preserved workspaces are pruned of capture-ignored entries on EVERY preservation path.

``capture_to`` (docker WORKDIR alignment) filters ``_WORKSPACE_CAPTURE_IGNORE`` at
copy time, but ``preserve_to`` (MOVE_ON_WRITE) moved the raw workspace and
DIRECT_WRITE never copies at all — so agent-created ``.venv``/``node_modules``
(whose ``bin/`` symlinks point at sandbox-only interpreter paths, dangling on the
host) and credential-store names persisted verbatim in ``run_dir/artifacts``.
One such dangling ``.venv/bin/python`` symlink is exactly what a symlink-refusing
artifact uploader chokes on. These tests pin the prune on all three paths, plus
the teardown-interrupt fix: a task-timeout CancelledError delivered during
post-run must not skip ``_cleanup()`` / ``_finalize_result()``.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.models import (
    AgentKind,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator, PreservationMode
from coder_eval.sandbox import Sandbox, _prune_capture_ignored


def _seed_workspace(root: Path) -> None:
    """Lay out a workspace with keepers and capture-ignored entries at several depths."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "artifact.json").write_text("{}\n", encoding="utf-8")
    # Agent-created venv with a DANGLING interpreter symlink (the nightly-killer shape).
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to("/nonexistent/uv/cpython-3.13/bin/python3.13")
    # Nested bulk + a node_modules bin symlink.
    nested = root / "src" / "pkg" / ".venv"
    nested.mkdir(parents=True)
    (nested / "pyvenv.cfg").write_text("home = /x\n", encoding="utf-8")
    nm_bin = root / "node_modules" / ".bin"
    nm_bin.mkdir(parents=True)
    (nm_bin / "tool").symlink_to("../tool/cli.js")
    # Credential-store name.
    (root / ".claude").mkdir()
    (root / ".claude" / ".credentials.json").write_text("{}\n", encoding="utf-8")


def test_prune_removes_ignored_entries_at_all_depths(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    _seed_workspace(root)

    pruned = _prune_capture_ignored(root)

    assert not (root / ".venv").exists()
    assert not (root / "src" / "pkg" / ".venv").exists()
    assert not (root / "node_modules").exists()
    assert not (root / ".claude").exists()
    # Keepers survive.
    assert (root / "src" / "app.py").exists()
    assert (root / "artifact.json").exists()
    assert set(pruned) >= {".claude", ".venv", "node_modules", str(Path("src") / "pkg" / ".venv")}


def test_prune_unlinks_symlinked_dir_without_following(tmp_path):
    """A symlinked ignored dir is unlinked, never rmtree'd through into its target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".venv").symlink_to(outside, target_is_directory=True)

    pruned = _prune_capture_ignored(root)

    assert pruned == [".venv"]
    assert not (root / ".venv").exists(follow_symlinks=False)
    assert (outside / "keep.txt").exists()


def test_prune_missing_root_is_noop(tmp_path):
    assert _prune_capture_ignored(tmp_path / "nope") == []


def test_preserve_to_prunes_moved_workspace(tmp_path):
    """MOVE_ON_WRITE: preserve_to's move is followed by the same filter capture_to applies."""
    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="prune_move_test")
    try:
        sandbox_dir = sandbox.setup()
        _seed_workspace(sandbox_dir)

        preserved = sandbox.preserve_to(tmp_path / "artifacts")

        assert (preserved / "src" / "app.py").exists()
        assert not (preserved / ".venv").exists(follow_symlinks=False)
        assert not (preserved / "node_modules").exists()
        assert not (preserved / ".claude").exists()
    finally:
        sandbox.cleanup()


def test_sandbox_prune_preserved_in_place(tmp_path):
    """DIRECT_WRITE's seam: prune_preserved() filters the sandbox tree where it stands."""
    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="prune_inplace_test")
    try:
        sandbox_dir = sandbox.setup()
        _seed_workspace(sandbox_dir)

        pruned = sandbox.prune_preserved()

        assert ".venv" in pruned
        assert not (sandbox_dir / ".venv").exists(follow_symlinks=False)
        assert (sandbox_dir / "artifact.json").exists()
    finally:
        sandbox.cleanup()


def test_sandbox_prune_preserved_without_setup_is_noop():
    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="prune_unset_test")
    assert sandbox.prune_preserved() == []


# --- Orchestrator seams -------------------------------------------------------


def _build_orchestrator(tmp_path: Path) -> Orchestrator:
    task = TaskDefinition(
        task_id="prune_teardown_task",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="x", path="x.py")],
    )
    run_dir = tmp_path / "run" / "prune_teardown_task"
    return Orchestrator(task=task, run_dir=run_dir, variant_id="v1")


def _patch_finalize_persistence():
    """Skip the on-disk persistence side-effects of _finalize_result."""
    return patch("coder_eval.reports_html.write_task_html", return_value=None)


@pytest.mark.asyncio
async def test_direct_write_cleanup_prunes_before_grant(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.preservation_mode = PreservationMode.DIRECT_WRITE
    orch.result = MagicMock()
    calls: list[str] = []
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sb"
    mock_sandbox.prune_preserved = MagicMock(side_effect=lambda: calls.append("prune"))
    mock_sandbox.grant_read_access = MagicMock(side_effect=lambda: calls.append("grant"))
    orch.sandbox = mock_sandbox

    await orch._cleanup()

    assert calls == ["prune", "grant"]
    mock_sandbox.cleanup.assert_called_once_with(preserve=False)


@pytest.mark.asyncio
async def test_post_run_cancellation_still_runs_cleanup_and_finalize(tmp_path):
    """The task-timeout watchdog's CancelledError lands during post-run: teardown
    must still complete (cleanup + finalize) and the cancellation still propagate."""
    orch = _build_orchestrator(tmp_path)
    cleanup = AsyncMock()

    async def boom_setup() -> None:
        raise RuntimeError("synthetic setup failure")

    with (
        _patch_finalize_persistence(),
        patch.object(orch, "_setup", side_effect=boom_setup),
        patch.object(orch, "_run_post_run_commands", AsyncMock(side_effect=asyncio.CancelledError())),
        patch.object(orch, "_cleanup", cleanup),
        pytest.raises(asyncio.CancelledError),
    ):
        await orch.run()

    cleanup.assert_awaited_once()
    # _finalize_result ran: the result was completed and scored despite the interrupt.
    assert orch.result is not None
    assert orch.result.completed_at is not None


@pytest.mark.asyncio
async def test_post_run_plain_exception_still_runs_cleanup_and_reraises(tmp_path):
    orch = _build_orchestrator(tmp_path)
    cleanup = AsyncMock()

    with (
        _patch_finalize_persistence(),
        patch.object(orch, "_setup", AsyncMock()),
        patch.object(orch, "_evaluation_loop", AsyncMock(return_value=True)),
        patch.object(orch, "_run_post_run_commands", AsyncMock(side_effect=OSError("post-run blew up"))),
        patch.object(orch, "_cleanup", cleanup),
        pytest.raises(OSError, match="post-run blew up"),
    ):
        await orch.run()

    cleanup.assert_awaited_once()
    assert orch.result is not None
    assert orch.result.completed_at is not None
