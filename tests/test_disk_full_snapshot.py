"""Tests for snapshot handling of disk full errors.

Tests ensure graceful error handling when disk space is exhausted.
"""

import errno
from unittest.mock import patch

import pytest

from coder_eval.models import FileChange, SandboxConfig, SnapshotMode
from coder_eval.sandbox import Sandbox


@pytest.mark.asyncio
async def test_full_snapshot_disk_full_raises_error(tmp_path):
    """Test that disk full during full snapshot raises OSError.

    Hypothesis: Disk full should propagate as OSError with ENOSPC.
    Expected: OSError raised when shutil.copytree fails with disk full.

    Context: Line 516-521 in sandbox.py use shutil.copytree via asyncio.to_thread.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()
    (sandbox.sandbox_dir / "test.txt").write_text("content")

    snapshot_dir = tmp_path / "snapshot"

    # Mock shutil.copytree to raise disk full error
    disk_full_error = OSError(errno.ENOSPC, "No space left on device")

    with patch("shutil.copytree", side_effect=disk_full_error), pytest.raises(OSError, match="No space left on device"):
        await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.FULL,
            ignore_patterns=None,
        )


@pytest.mark.asyncio
async def test_incremental_snapshot_disk_full_during_copy(tmp_path):
    """Test that disk full during incremental snapshot file copy raises OSError.

    Hypothesis: Disk full during file copy should raise OSError.
    Expected: OSError propagated when shutil.copy2 fails.

    Context: Lines 566-567 in sandbox.py use shutil.copy2 for incremental snapshots.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()
    (sandbox.sandbox_dir / "modified.txt").write_text("new content")

    file_changes = [
        FileChange(path="modified.txt", operation="modified"),
    ]

    snapshot_dir = tmp_path / "snapshot"

    # Mock shutil.copy2 to raise disk full error
    disk_full_error = OSError(errno.ENOSPC, "No space left on device")

    with patch("shutil.copy2", side_effect=disk_full_error), pytest.raises(OSError, match="No space left on device"):
        await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.INCREMENTAL,
            changed_files=file_changes,
        )


@pytest.mark.asyncio
async def test_snapshot_manifest_write_disk_full(tmp_path):
    """Test that disk full during manifest write raises OSError.

    Hypothesis: Disk full when writing manifest.json should raise OSError.
    Expected: OSError propagated from Path.write_text.

    Context: Line 480 in sandbox.py calls _write_manifest after snapshot.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()
    (sandbox.sandbox_dir / "file.txt").write_text("content")

    snapshot_dir = tmp_path / "snapshot"

    # Mock Path.write_text to raise disk full error when writing manifest
    disk_full_error = OSError(errno.ENOSPC, "No space left on device")

    # We need to mock the write_text call that happens during manifest writing
    # The snapshot will succeed, but manifest write will fail
    original_write_text = type(snapshot_dir).write_text

    def mock_write_text(self, *args, **kwargs):
        # Only fail on manifest.json write, allow other writes
        if self.name == "manifest.json":
            raise disk_full_error
        return original_write_text(self, *args, **kwargs)

    with (
        patch.object(type(snapshot_dir), "write_text", mock_write_text),
        pytest.raises(OSError, match="No space left on device"),
    ):
        await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.FULL,
            ignore_patterns=None,
        )


@pytest.mark.asyncio
async def test_snapshot_mkdir_disk_full(tmp_path):
    """Test that disk full when creating snapshot directory raises OSError.

    Hypothesis: Disk full when creating snapshot dir should raise OSError.
    Expected: OSError raised during snapshot_dir.mkdir().

    Context: Line 468 in sandbox.py creates snapshot_dir via asyncio.to_thread.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    snapshot_dir = tmp_path / "snapshot"

    # Mock Path.mkdir to raise disk full error
    disk_full_error = OSError(errno.ENOSPC, "No space left on device")

    with (
        patch.object(type(snapshot_dir), "mkdir", side_effect=disk_full_error),
        pytest.raises(OSError, match="No space left on device"),
    ):
        await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.FULL,
            ignore_patterns=None,
        )


@pytest.mark.asyncio
async def test_snapshot_partial_failure_cleanup(tmp_path):
    """Test that partial snapshot (some files copied, then disk full) leaves state.

    Hypothesis: Disk full mid-copy leaves partial snapshot directory.
    Expected: Snapshot directory exists with some files, OSError raised.

    Note: This tests actual behavior - no cleanup on failure.
    """
    import shutil

    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()
    (sandbox.sandbox_dir / "file1.txt").write_text("content1")
    (sandbox.sandbox_dir / "file2.txt").write_text("content2")

    file_changes = [
        FileChange(path="file1.txt", operation="modified"),
        FileChange(path="file2.txt", operation="modified"),
    ]

    snapshot_dir = tmp_path / "snapshot"

    # Save original copy2 function
    original_copy2 = shutil.copy2

    # Mock shutil.copy2 to succeed first time, fail second time
    call_count = 0

    def mock_copy2(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call succeeds (copy file1.txt) - use original function
            return original_copy2(*args, **kwargs)
        else:
            # Second call fails (disk full during file2.txt)
            raise OSError(errno.ENOSPC, "No space left on device")

    with patch("shutil.copy2", side_effect=mock_copy2), pytest.raises(OSError, match="No space left on device"):
        await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.INCREMENTAL,
            changed_files=file_changes,
        )

    # Verify partial snapshot exists (file1.txt was copied)
    assert snapshot_dir.exists()
    assert (snapshot_dir / "file1.txt").exists()
    # file2.txt should not exist (failed during copy)
    assert not (snapshot_dir / "file2.txt").exists()


@pytest.mark.asyncio
async def test_snapshot_success_after_disk_space_freed(tmp_path):
    """Test that snapshot succeeds after disk space is freed.

    Hypothesis: Retry after freeing space should succeed.
    Expected: First snapshot fails, second succeeds.

    This tests resilience and retry capability.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()
    (sandbox.sandbox_dir / "file.txt").write_text("content")

    snapshot_dir_1 = tmp_path / "snapshot_1"
    snapshot_dir_2 = tmp_path / "snapshot_2"

    # Mock first snapshot to fail (disk full)
    disk_full_error = OSError(errno.ENOSPC, "No space left on device")

    with patch("shutil.copytree", side_effect=disk_full_error), pytest.raises(OSError, match="No space left on device"):
        await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir_1,
            mode=SnapshotMode.FULL,
            ignore_patterns=None,
        )

    # Second snapshot succeeds (disk space freed - no mock)
    manifest = await sandbox.create_snapshot(
        snapshot_dir=snapshot_dir_2,
        mode=SnapshotMode.FULL,
        ignore_patterns=None,
    )

    # Verify success
    assert manifest.file_count > 0
    assert snapshot_dir_2.exists()
    assert (snapshot_dir_2 / "file.txt").exists()
