"""Tests for the sandbox manager."""

import asyncio
from pathlib import Path

import pytest

from coder_eval.models import FileChange, SandboxConfig, SnapshotMode
from coder_eval.sandbox import Sandbox


def test_tempdir_sandbox_basic():
    """Test basic tempdir sandbox creation and cleanup."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)

    sandbox = Sandbox(config, task_id="test_basic")

    try:
        # Setup
        sandbox_dir = sandbox.setup()
        assert sandbox_dir.exists()
        assert sandbox_dir.is_dir()

        # Check venv was created
        venv_dir = sandbox_dir / ".venv"
        assert venv_dir.exists()
        assert (venv_dir / "bin" / "python").exists()

    finally:
        # Cleanup
        sandbox.cleanup()
        assert not sandbox_dir.exists()


def test_sandbox_run_command():
    """Test running commands in the sandbox."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)

    sandbox = Sandbox(config, task_id="test_run_cmd")

    try:
        sandbox.setup()

        # Run a simple command
        exit_code, stdout, _stderr = sandbox.run_command("echo 'Hello, World!'")
        assert exit_code == 0
        assert "Hello, World!" in stdout

        # Run Python command
        exit_code, stdout, _stderr = sandbox.run_command("python --version")
        assert exit_code == 0
        assert "Python" in stdout

    finally:
        sandbox.cleanup()


def test_sandbox_with_packages():
    """Test sandbox with package installation."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=["requests"], network_enabled=False)

    sandbox = Sandbox(config, task_id="test_packages")

    try:
        sandbox.setup()

        # Test that requests is installed
        exit_code, stdout, stderr = sandbox.run_command('python -c "import requests; print(requests.__version__)"')
        if exit_code != 0:
            print(f"STDOUT: {stdout}")
            print(f"STDERR: {stderr}")
        assert exit_code == 0, f"Command failed with stderr: {stderr}"
        assert len(stdout.strip()) > 0  # Should print version

    finally:
        sandbox.cleanup()


def test_sandbox_file_operations():
    """Test file operations in the sandbox."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)

    sandbox = Sandbox(config, task_id="test_files")

    try:
        sandbox_dir = sandbox.setup()

        # Create a test file
        test_file = sandbox_dir / "test.txt"
        test_file.write_text("Hello from test!")

        # Check file exists
        assert sandbox.file_exists("test.txt")

        # Read file content
        content = sandbox.get_file_content("test.txt")
        assert content == "Hello from test!"

        # List files
        files = sandbox.list_files()
        # Filter out .venv directory
        user_files = [f for f in files if not f.startswith(".venv")]
        assert "test.txt" in user_files

    finally:
        sandbox.cleanup()


def test_sandbox_timeout():
    """Test command timeout enforcement."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)

    sandbox = Sandbox(config, task_id="test_timeout")

    try:
        sandbox.setup()

        # Run a command that sleeps longer than timeout
        exit_code, _stdout, stderr = sandbox.run_command("sleep 10", timeout=1)
        assert exit_code == -1
        assert "timed out" in stderr.lower()

    finally:
        sandbox.cleanup()


def test_sandbox_preserve():
    """Test preserving sandbox to artifact directory."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)

    sandbox = Sandbox(config, task_id="test_preserve")
    artifact_dir = Path("artifacts/test")

    try:
        sandbox_dir = sandbox.setup()

        # Create a test file
        test_file = sandbox_dir / "preserved.txt"
        test_file.write_text("This should be preserved")

        # Preserve and cleanup
        preserved_path = sandbox.preserve_to(artifact_dir)
        assert preserved_path.exists()
        assert (preserved_path / "preserved.txt").exists()
        assert (preserved_path / "preserved.txt").read_text() == "This should be preserved"

        # Now cleanup
        sandbox.cleanup()

        # Original should be gone but preserved should remain
        assert not sandbox_dir.exists()
        assert preserved_path.exists()

    finally:
        # Clean up artifacts
        if artifact_dir.exists():
            import shutil

            shutil.rmtree(artifact_dir.parent)


# ==================== Snapshot Tests ====================


@pytest.mark.asyncio
async def test_snapshot_full():
    """Test full snapshot creation."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_snapshot_full")

    try:
        sandbox_dir = sandbox.setup()

        # Create test files
        (sandbox_dir / "file1.txt").write_text("Content 1")
        (sandbox_dir / "file2.py").write_text("print('hello')")
        (sandbox_dir / "subdir").mkdir()
        (sandbox_dir / "subdir" / "file3.txt").write_text("Content 3")

        # Create snapshot directory
        snapshot_dir = sandbox_dir.parent / "snapshots" / "iter_0"

        # Create full snapshot
        manifest = await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.FULL,
            changed_files=None,
            ignore_patterns=None,
        )

        # Verify snapshot directory exists
        assert snapshot_dir.exists()
        assert (snapshot_dir / "file1.txt").exists()
        assert (snapshot_dir / "file2.py").exists()
        assert (snapshot_dir / "subdir" / "file3.txt").exists()

        # Verify manifest
        assert manifest.mode == SnapshotMode.FULL
        assert manifest.file_count > 0
        assert manifest.size_bytes > 0
        assert manifest.iteration == 0  # Default value

        # Verify manifest file was written
        manifest_file = snapshot_dir / "manifest.json"
        assert manifest_file.exists()

        # Read manifest back
        loaded_manifest = await sandbox._read_manifest(snapshot_dir)
        assert loaded_manifest.mode == SnapshotMode.FULL
        assert loaded_manifest.file_count == manifest.file_count

    finally:
        sandbox.cleanup()
        # Clean up snapshot directory
        snapshot_parent = sandbox_dir.parent / "snapshots"
        if snapshot_parent.exists():
            import shutil

            shutil.rmtree(snapshot_parent)


@pytest.mark.asyncio
async def test_snapshot_incremental():
    """Test incremental snapshot with changed files."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_snapshot_incremental")

    try:
        sandbox_dir = sandbox.setup()

        # Create initial files
        (sandbox_dir / "file1.txt").write_text("Content 1")
        (sandbox_dir / "file2.txt").write_text("Content 2")
        (sandbox_dir / "file3.txt").write_text("Content 3")

        # Simulate file changes
        changed_files = [
            FileChange(path="file1.txt", operation="modified"),
            FileChange(path="file4.txt", operation="created"),
            FileChange(path="file5.txt", operation="deleted"),  # Should be tracked
        ]

        # Create the new file
        (sandbox_dir / "file4.txt").write_text("New content")

        # Create snapshot directory
        snapshot_dir = sandbox_dir.parent / "snapshots" / "iter_1"

        # Create incremental snapshot
        manifest = await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.INCREMENTAL,
            changed_files=changed_files,
            ignore_patterns=None,
        )

        # Verify only changed files are in snapshot
        assert (snapshot_dir / "file1.txt").exists()
        assert (snapshot_dir / "file4.txt").exists()
        assert not (snapshot_dir / "file2.txt").exists()  # Unchanged, not copied
        assert not (snapshot_dir / "file3.txt").exists()  # Unchanged, not copied

        # Verify manifest
        assert manifest.mode == SnapshotMode.INCREMENTAL
        assert manifest.file_count == 2  # file1.txt and file4.txt
        assert "file1.txt" in manifest.changed_files
        assert "file4.txt" in manifest.changed_files
        assert "DELETED:file5.txt" in manifest.changed_files

        # Verify manifest file was written
        manifest_file = snapshot_dir / "manifest.json"
        assert manifest_file.exists()

    finally:
        sandbox.cleanup()
        # Clean up snapshot directory
        snapshot_parent = sandbox_dir.parent / "snapshots"
        if snapshot_parent.exists():
            import shutil

            shutil.rmtree(snapshot_parent)


@pytest.mark.asyncio
async def test_snapshot_ignores_patterns():
    """Test that snapshot respects ignore patterns."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_snapshot_ignore")

    try:
        sandbox_dir = sandbox.setup()

        # Create files including ones that should be ignored
        (sandbox_dir / "important.txt").write_text("Keep this")
        (sandbox_dir / "secret.env").write_text("API_KEY=secret")
        (sandbox_dir / ".DS_Store").write_text("Mac metadata")
        (sandbox_dir / "__pycache__").mkdir()
        (sandbox_dir / "__pycache__" / "module.pyc").write_text("bytecode")

        # Create snapshot with additional ignore patterns
        snapshot_dir = sandbox_dir.parent / "snapshots" / "iter_0"

        await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.FULL,
            changed_files=None,
            ignore_patterns=["*.env"],  # Additional pattern
        )

        # Verify important file was copied
        assert (snapshot_dir / "important.txt").exists()

        # Verify ignored files were NOT copied
        assert not (snapshot_dir / "secret.env").exists()  # Custom pattern
        assert not (snapshot_dir / ".DS_Store").exists()  # Default pattern
        assert not (snapshot_dir / "__pycache__").exists()  # Default pattern

        # Verify .venv was ignored (default behavior)
        venv_in_snapshot = snapshot_dir / ".venv"
        assert not venv_in_snapshot.exists()

    finally:
        sandbox.cleanup()
        # Clean up snapshot directory
        snapshot_parent = sandbox_dir.parent / "snapshots"
        if snapshot_parent.exists():
            import shutil

            shutil.rmtree(snapshot_parent)


@pytest.mark.asyncio
async def test_snapshot_async_behavior():
    """Test that snapshot operations are truly async and don't block."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_snapshot_async")

    try:
        sandbox_dir = sandbox.setup()

        # Create a moderate number of files to ensure I/O operations take time
        for i in range(50):
            (sandbox_dir / f"file_{i}.txt").write_text(f"Content {i}" * 100)

        snapshot_dir = sandbox_dir.parent / "snapshots" / "iter_0"

        # Run snapshot creation - should not block event loop
        start = asyncio.get_event_loop().time()
        manifest = await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.FULL,
            changed_files=None,
            ignore_patterns=None,
        )
        elapsed = asyncio.get_event_loop().time() - start

        # Verify snapshot was created
        assert manifest.file_count >= 50
        assert snapshot_dir.exists()

        # Verify it took some measurable time (but not too long)
        assert elapsed > 0.0
        assert elapsed < 10.0  # Should be fast on modern hardware

    finally:
        sandbox.cleanup()
        # Clean up snapshot directory
        snapshot_parent = sandbox_dir.parent / "snapshots"
        if snapshot_parent.exists():
            import shutil

            shutil.rmtree(snapshot_parent)


@pytest.mark.asyncio
async def test_snapshot_manifest_roundtrip():
    """Test manifest serialization and deserialization."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_manifest_roundtrip")

    try:
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "test.txt").write_text("Test content")

        snapshot_dir = sandbox_dir.parent / "snapshots" / "iter_0"

        # Create snapshot
        original_manifest = await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.FULL,
            changed_files=None,
            ignore_patterns=None,
        )

        # Read manifest back
        loaded_manifest = await sandbox._read_manifest(snapshot_dir)

        # Verify all fields match
        assert loaded_manifest.mode == original_manifest.mode
        assert loaded_manifest.file_count == original_manifest.file_count
        assert loaded_manifest.size_bytes == original_manifest.size_bytes
        assert loaded_manifest.iteration == original_manifest.iteration

    finally:
        sandbox.cleanup()
        # Clean up snapshot directory
        snapshot_parent = sandbox_dir.parent / "snapshots"
        if snapshot_parent.exists():
            import shutil

            shutil.rmtree(snapshot_parent)


@pytest.mark.asyncio
async def test_snapshot_without_sandbox_initialization():
    """Test that snapshot fails gracefully if sandbox not initialized."""
    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_snapshot_uninitialized")

    # Don't call sandbox.setup()
    snapshot_dir = Path("/tmp/should_not_exist")

    with pytest.raises(RuntimeError, match="Sandbox not initialized"):
        await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=SnapshotMode.FULL,
            changed_files=None,
            ignore_patterns=None,
        )


# ==================== Subprocess Logging Tests ====================


@pytest.fixture
def clean_logging():
    """Clean up logging handlers before and after test."""
    import logging

    # Store original handlers
    logger = logging.getLogger("coder_eval")
    original_handlers = logger.handlers.copy()
    original_level = logger.level
    original_propagate = logger.propagate

    # Clear handlers for clean slate
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = True

    yield

    # Restore original state
    logger.handlers.clear()
    logger.handlers.extend(original_handlers)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def test_sandbox_run_command_logging(caplog, clean_logging):
    """Test that run_command logs output at DEBUG level."""
    import logging

    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_logging")

    try:
        sandbox.setup()

        # Run command that produces output
        with caplog.at_level(logging.DEBUG):
            exit_code, stdout, stderr = sandbox.run_command("echo 'Hello' && echo 'Error' >&2")

        # Verify command succeeded
        assert exit_code == 0
        assert "Hello" in stdout
        assert "Error" in stderr

        # Verify logs were created with correct messages
        log_messages = [record.message for record in caplog.records if record.name == "coder_eval.sandbox"]
        assert any("Command 'echo" in msg and "exited with code 0" in msg for msg in log_messages)
        assert any("STDOUT:" in msg and "Hello" in msg for msg in log_messages)
        assert any("STDERR:" in msg and "Error" in msg for msg in log_messages)

    finally:
        sandbox.cleanup()


def test_sandbox_run_command_timeout_logging(caplog, clean_logging):
    """Test that timeouts are logged at WARNING level."""
    import logging

    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_timeout")

    try:
        sandbox.setup()

        with caplog.at_level(logging.WARNING):
            exit_code, _stdout, _stderr = sandbox.run_command("sleep 10", timeout=1)

        # Verify timeout logged at WARNING
        assert exit_code == -1
        log_messages = [record.message for record in caplog.records if record.name == "coder_eval.sandbox"]
        assert any("timed out after 1 seconds" in msg for msg in log_messages)
        assert logging.WARNING in [rec.levelno for rec in caplog.records]

    finally:
        sandbox.cleanup()


def test_sandbox_run_command_empty_output_not_logged(caplog, clean_logging):
    """Test that empty stdout/stderr is not logged."""
    import logging

    config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
    sandbox = Sandbox(config, task_id="test_empty")

    try:
        sandbox.setup()

        with caplog.at_level(logging.DEBUG):
            sandbox.run_command("true")  # Command with no output

        # Should log command completion but not empty output blocks
        log_messages = [record.message for record in caplog.records if record.name == "coder_eval.sandbox"]
        assert any("exited with code 0" in msg for msg in log_messages)
        assert not any("STDOUT:" in msg for msg in log_messages)  # Empty, not logged
        assert not any("STDERR:" in msg for msg in log_messages)

    finally:
        sandbox.cleanup()
