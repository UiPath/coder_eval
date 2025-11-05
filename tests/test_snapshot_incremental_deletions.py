"""Tests for incremental snapshot handling of file deletions.

Tests ensure deleted files are recorded in snapshot manifests.
"""

import pytest

from coder_eval.models import FileChange, SandboxConfig
from coder_eval.sandbox import Sandbox


@pytest.mark.asyncio
async def test_incremental_snapshot_records_deletions(tmp_path):
    """Test that incremental snapshot records deleted files with DELETED: marker.

    Hypothesis: File deletions should be tracked in manifest.
    Expected: changed_files contains "DELETED:filename" entries.

    Context: Lines 559-562 in sandbox.py store deletion markers.
    """
    # Create sandbox
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    # Setup sandbox manually
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create some initial files
    (sandbox.sandbox_dir / "file1.txt").write_text("content1")
    (sandbox.sandbox_dir / "file2.txt").write_text("content2")
    (sandbox.sandbox_dir / "keep.txt").write_text("keep this")

    # Define file changes including deletions
    file_changes = [
        FileChange(path="file1.txt", operation="deleted"),
        FileChange(path="file2.txt", operation="deleted"),
        FileChange(path="keep.txt", operation="modified"),
    ]

    # Create snapshot directory
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    # Create incremental snapshot
    manifest = await sandbox._snapshot_incremental(snapshot_dir, file_changes)

    # Verify deletions are recorded with DELETED: prefix
    assert "DELETED:file1.txt" in manifest.changed_files
    assert "DELETED:file2.txt" in manifest.changed_files

    # Verify modified file is copied (without DELETED: prefix)
    assert "keep.txt" in manifest.changed_files
    assert (snapshot_dir / "keep.txt").exists()
    assert (snapshot_dir / "keep.txt").read_text() == "keep this"

    # Verify deleted files are NOT copied to snapshot
    assert not (snapshot_dir / "file1.txt").exists()
    assert not (snapshot_dir / "file2.txt").exists()

    # Verify file count excludes deletions
    assert manifest.file_count == 1  # Only keep.txt


@pytest.mark.asyncio
async def test_incremental_snapshot_mixed_operations(tmp_path):
    """Test incremental snapshot with create, modify, and delete operations.

    Hypothesis: All operation types should be handled correctly.
    Expected: Created and modified files copied, deleted files marked.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create files for different operations
    (sandbox.sandbox_dir / "created.txt").write_text("new file")
    (sandbox.sandbox_dir / "modified.txt").write_text("modified content")
    # deleted.txt doesn't exist in sandbox (already deleted)

    file_changes = [
        FileChange(path="created.txt", operation="created"),
        FileChange(path="modified.txt", operation="modified"),
        FileChange(path="deleted.txt", operation="deleted"),
    ]

    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    manifest = await sandbox._snapshot_incremental(snapshot_dir, file_changes)

    # Verify all operations recorded
    assert "created.txt" in manifest.changed_files
    assert "modified.txt" in manifest.changed_files
    assert "DELETED:deleted.txt" in manifest.changed_files

    # Verify physical files
    assert (snapshot_dir / "created.txt").exists()
    assert (snapshot_dir / "modified.txt").exists()
    assert not (snapshot_dir / "deleted.txt").exists()

    assert manifest.file_count == 2  # created + modified


@pytest.mark.asyncio
async def test_incremental_snapshot_deletion_only(tmp_path):
    """Test incremental snapshot containing only deletions.

    Hypothesis: Snapshot with only deletions should have zero file_count.
    Expected: Manifest contains DELETED: markers, file_count=0.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    file_changes = [
        FileChange(path="removed1.txt", operation="deleted"),
        FileChange(path="removed2.txt", operation="deleted"),
        FileChange(path="removed3.txt", operation="deleted"),
    ]

    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    manifest = await sandbox._snapshot_incremental(snapshot_dir, file_changes)

    # Verify all deletions recorded
    assert manifest.file_count == 0
    assert manifest.size_bytes == 0
    assert len(manifest.changed_files) == 3
    assert all(path.startswith("DELETED:") for path in manifest.changed_files)


@pytest.mark.asyncio
async def test_incremental_snapshot_nested_path_deletions(tmp_path):
    """Test incremental snapshot with nested directory deletions.

    Hypothesis: Nested paths should be handled correctly.
    Expected: DELETED: markers preserve full path structure.
    """
    config = SandboxConfig(driver="tempdir")
    sandbox = Sandbox(config=config, task_id="test_task")

    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Create nested structure
    (sandbox.sandbox_dir / "src").mkdir()
    (sandbox.sandbox_dir / "src" / "utils").mkdir()
    (sandbox.sandbox_dir / "src" / "utils" / "helper.py").write_text("code")

    file_changes = [
        FileChange(path="src/main.py", operation="deleted"),
        FileChange(path="src/utils/helper.py", operation="modified"),
        FileChange(path="tests/test_main.py", operation="deleted"),
    ]

    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()

    manifest = await sandbox._snapshot_incremental(snapshot_dir, file_changes)

    # Verify nested deletions preserved
    assert "DELETED:src/main.py" in manifest.changed_files
    assert "DELETED:tests/test_main.py" in manifest.changed_files
    assert "src/utils/helper.py" in manifest.changed_files

    # Verify nested file structure created
    assert (snapshot_dir / "src" / "utils" / "helper.py").exists()
