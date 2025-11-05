"""Tests for concurrent snapshot operations and race conditions.

Tests ensure thread-safety and proper handling of concurrent modifications.
"""

import asyncio

import pytest

from coder_eval.models import FileChange, SandboxConfig, SnapshotMode
from coder_eval.sandbox import Sandbox


@pytest.mark.asyncio
async def test_concurrent_full_snapshots_no_corruption(tmp_path):
    """Test that concurrent full snapshots don't corrupt each other.

    Hypothesis: Multiple concurrent full snapshots should complete independently.
    Expected: All snapshots complete successfully, each with correct file count.

    Context: Lines 516-521 in sandbox.py use shutil.copytree via asyncio.to_thread.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create multiple files in sandbox
    for i in range(10):
        (sandbox.sandbox_dir / f"file{i}.txt").write_text(f"content{i}")

    # Create multiple snapshot directories
    snapshot_dirs = [tmp_path / f"snapshot_{i}" for i in range(5)]

    # Run concurrent full snapshots
    tasks = [
        sandbox.create_snapshot(snapshot_dir=sd, mode=SnapshotMode.FULL, ignore_patterns=None) for sd in snapshot_dirs
    ]

    manifests = await asyncio.gather(*tasks)

    # Verify all snapshots completed successfully
    assert len(manifests) == 5
    for i, manifest in enumerate(manifests):
        assert manifest.file_count >= 10  # At least our 10 files
        assert manifest.mode == SnapshotMode.FULL
        assert snapshot_dirs[i].exists()

        # Verify all files were copied
        for j in range(10):
            assert (snapshot_dirs[i] / f"file{j}.txt").exists()
            assert (snapshot_dirs[i] / f"file{j}.txt").read_text() == f"content{j}"


@pytest.mark.asyncio
async def test_concurrent_incremental_snapshots(tmp_path):
    """Test that concurrent incremental snapshots with different file sets work.

    Hypothesis: Concurrent incremental snapshots with non-overlapping files should succeed.
    Expected: Each snapshot contains only its designated files.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create multiple files
    for i in range(20):
        (sandbox.sandbox_dir / f"file{i}.txt").write_text(f"content{i}")

    # Prepare different file change sets for each snapshot
    file_change_sets = [
        [FileChange(path=f"file{i}.txt", operation="modified") for i in range(0, 5)],
        [FileChange(path=f"file{i}.txt", operation="modified") for i in range(5, 10)],
        [FileChange(path=f"file{i}.txt", operation="modified") for i in range(10, 15)],
        [FileChange(path=f"file{i}.txt", operation="modified") for i in range(15, 20)],
    ]

    snapshot_dirs = [tmp_path / f"incremental_{i}" for i in range(4)]

    # Run concurrent incremental snapshots
    tasks = [
        sandbox.create_snapshot(
            snapshot_dir=sd,
            mode=SnapshotMode.INCREMENTAL,
            changed_files=file_changes,
        )
        for sd, file_changes in zip(snapshot_dirs, file_change_sets, strict=True)
    ]

    manifests = await asyncio.gather(*tasks)

    # Verify each snapshot contains correct files
    for i, manifest in enumerate(manifests):
        assert manifest.file_count == 5
        assert manifest.mode == SnapshotMode.INCREMENTAL

        # Verify only designated files were copied
        start_idx = i * 5
        for j in range(start_idx, start_idx + 5):
            assert (snapshot_dirs[i] / f"file{j}.txt").exists()

        # Verify other files were NOT copied
        for j in range(20):
            if j < start_idx or j >= start_idx + 5:
                assert not (snapshot_dirs[i] / f"file{j}.txt").exists()


@pytest.mark.asyncio
async def test_concurrent_mixed_snapshot_modes(tmp_path):
    """Test concurrent full and incremental snapshots don't interfere.

    Hypothesis: Mixed snapshot modes running concurrently should complete independently.
    Expected: Both full and incremental snapshots succeed with correct file counts.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create files
    for i in range(10):
        (sandbox.sandbox_dir / f"file{i}.txt").write_text(f"content{i}")

    # Prepare tasks: 2 full snapshots + 2 incremental snapshots
    full_dirs = [tmp_path / f"full_{i}" for i in range(2)]
    incr_dirs = [tmp_path / f"incr_{i}" for i in range(2)]

    file_changes = [FileChange(path=f"file{i}.txt", operation="modified") for i in range(5)]

    tasks = [
        sandbox.create_snapshot(snapshot_dir=full_dirs[0], mode=SnapshotMode.FULL, ignore_patterns=None),
        sandbox.create_snapshot(snapshot_dir=full_dirs[1], mode=SnapshotMode.FULL, ignore_patterns=None),
        sandbox.create_snapshot(
            snapshot_dir=incr_dirs[0],
            mode=SnapshotMode.INCREMENTAL,
            changed_files=file_changes,
        ),
        sandbox.create_snapshot(
            snapshot_dir=incr_dirs[1],
            mode=SnapshotMode.INCREMENTAL,
            changed_files=file_changes,
        ),
    ]

    manifests = await asyncio.gather(*tasks)

    # Verify full snapshots
    assert manifests[0].mode == SnapshotMode.FULL
    assert manifests[1].mode == SnapshotMode.FULL
    assert manifests[0].file_count >= 10
    assert manifests[1].file_count >= 10

    # Verify incremental snapshots
    assert manifests[2].mode == SnapshotMode.INCREMENTAL
    assert manifests[3].mode == SnapshotMode.INCREMENTAL
    assert manifests[2].file_count == 5
    assert manifests[3].file_count == 5


@pytest.mark.asyncio
async def test_snapshot_while_files_being_modified(tmp_path):
    """Test snapshot behavior when source files are modified during snapshot.

    Hypothesis: File modifications during snapshot should not cause corruption.
    Expected: Snapshot completes, may have stale or new content depending on timing.

    Note: This tests real-world race conditions.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create initial files
    for i in range(20):
        (sandbox.sandbox_dir / f"file{i}.txt").write_text(f"original{i}")

    snapshot_dir = tmp_path / "snapshot"

    async def modify_files_concurrently():
        """Modify files while snapshot is in progress."""
        await asyncio.sleep(0.01)  # Let snapshot start
        for i in range(20):
            (sandbox.sandbox_dir / f"file{i}.txt").write_text(f"modified{i}")
            await asyncio.sleep(0.001)  # Small delay between modifications

    # Run snapshot and file modifications concurrently
    snapshot_task = sandbox.create_snapshot(
        snapshot_dir=snapshot_dir,
        mode=SnapshotMode.FULL,
        ignore_patterns=None,
    )
    modify_task = modify_files_concurrently()

    manifest, _ = await asyncio.gather(snapshot_task, modify_task)

    # Snapshot should complete without errors
    assert manifest.file_count >= 20
    assert snapshot_dir.exists()

    # Files in snapshot may have original or modified content (timing-dependent)
    # Just verify no corruption (files exist and contain valid text)
    for i in range(20):
        assert (snapshot_dir / f"file{i}.txt").exists()
        content = (snapshot_dir / f"file{i}.txt").read_text()
        assert content in [f"original{i}", f"modified{i}"]


@pytest.mark.asyncio
async def test_concurrent_snapshot_with_deletions(tmp_path):
    """Test concurrent incremental snapshots with file deletions.

    Hypothesis: Deletion markers should be recorded correctly in concurrent snapshots.
    Expected: Each snapshot correctly tracks its deletions with DELETED: prefix.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create files
    for i in range(15):
        (sandbox.sandbox_dir / f"file{i}.txt").write_text(f"content{i}")

    # Prepare file changes with deletions
    file_change_sets = [
        [
            FileChange(path="file0.txt", operation="deleted"),
            FileChange(path="file1.txt", operation="modified"),
        ],
        [
            FileChange(path="file5.txt", operation="deleted"),
            FileChange(path="file6.txt", operation="modified"),
        ],
        [
            FileChange(path="file10.txt", operation="deleted"),
            FileChange(path="file11.txt", operation="modified"),
        ],
    ]

    snapshot_dirs = [tmp_path / f"snapshot_del_{i}" for i in range(3)]

    tasks = [
        sandbox.create_snapshot(
            snapshot_dir=sd,
            mode=SnapshotMode.INCREMENTAL,
            changed_files=changes,
        )
        for sd, changes in zip(snapshot_dirs, file_change_sets, strict=True)
    ]

    manifests = await asyncio.gather(*tasks)

    # Verify each snapshot tracks its deletions
    assert "DELETED:file0.txt" in manifests[0].changed_files
    assert "file1.txt" in manifests[0].changed_files
    assert manifests[0].file_count == 1  # Only modified file

    assert "DELETED:file5.txt" in manifests[1].changed_files
    assert "file6.txt" in manifests[1].changed_files
    assert manifests[1].file_count == 1

    assert "DELETED:file10.txt" in manifests[2].changed_files
    assert "file11.txt" in manifests[2].changed_files
    assert manifests[2].file_count == 1

    # Verify deleted files not copied, modified files copied
    assert not (snapshot_dirs[0] / "file0.txt").exists()
    assert (snapshot_dirs[0] / "file1.txt").exists()

    assert not (snapshot_dirs[1] / "file5.txt").exists()
    assert (snapshot_dirs[1] / "file6.txt").exists()

    assert not (snapshot_dirs[2] / "file10.txt").exists()
    assert (snapshot_dirs[2] / "file11.txt").exists()


@pytest.mark.asyncio
async def test_high_concurrency_stress_test(tmp_path):
    """Stress test with many concurrent snapshot operations.

    Hypothesis: System should handle high concurrency without deadlocks or corruption.
    Expected: All snapshots complete successfully.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create files
    for i in range(50):
        (sandbox.sandbox_dir / f"file{i}.txt").write_text(f"content{i}")

    # Create 20 concurrent snapshots (high concurrency)
    snapshot_dirs = [tmp_path / f"stress_{i}" for i in range(20)]

    tasks = [
        sandbox.create_snapshot(snapshot_dir=sd, mode=SnapshotMode.FULL, ignore_patterns=None) for sd in snapshot_dirs
    ]

    # All tasks should complete without deadlocks or errors
    manifests = await asyncio.gather(*tasks)

    # Verify all completed
    assert len(manifests) == 20
    for i, manifest in enumerate(manifests):
        assert manifest.file_count >= 50
        assert snapshot_dirs[i].exists()
        # Spot check: verify first and last file in each snapshot
        assert (snapshot_dirs[i] / "file0.txt").exists()
        assert (snapshot_dirs[i] / "file49.txt").exists()
