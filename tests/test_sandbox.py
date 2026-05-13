"""Tests for the sandbox manager."""

import asyncio
import os
from pathlib import Path

import pytest

from coder_eval.models import FileChange, SandboxConfig, SnapshotMode
from coder_eval.sandbox import Sandbox


def test_tempdir_sandbox_basic():
    """Test basic tempdir sandbox creation and cleanup."""
    config = SandboxConfig(driver="tempdir")

    sandbox = Sandbox(config, task_id="test_basic")

    try:
        # Setup
        sandbox_dir = sandbox.setup()
        assert sandbox_dir.exists()
        assert sandbox_dir.is_dir()

        # Check venv was created
        venv_dir = sandbox_dir / ".venv"
        assert venv_dir.exists()
        scripts_dir = "Scripts" if os.name == "nt" else "bin"
        python_name = "python.exe" if os.name == "nt" else "python"
        assert (venv_dir / scripts_dir / python_name).exists()

    finally:
        # Cleanup
        sandbox.cleanup()
        assert not sandbox_dir.exists()


def test_sandbox_without_python_config():
    """Test sandbox with python=None skips venv creation."""
    config = SandboxConfig(driver="tempdir", python=None)

    sandbox = Sandbox(config, task_id="test_no_venv")

    try:
        sandbox_dir = sandbox.setup()
        assert sandbox_dir.exists()

        # No .venv should be created
        assert not (sandbox_dir / ".venv").exists()
        assert sandbox.venv_dir is None

        # Commands still work (using system PATH)
        exit_code, stdout, _stderr = sandbox.run_command("python -c \"print('hello')\"")
        assert exit_code == 0
        assert "hello" in stdout

    finally:
        sandbox.cleanup()


def test_sandbox_run_command():
    """Test running commands in the sandbox."""
    config = SandboxConfig(driver="tempdir")

    sandbox = Sandbox(config, task_id="test_run_cmd")

    try:
        sandbox.setup()

        # Run a simple command
        exit_code, stdout, _stderr = sandbox.run_command("python -c \"print('Hello, World!')\"")
        assert exit_code == 0
        assert "Hello, World!" in stdout

        # Run Python command
        exit_code, stdout, _stderr = sandbox.run_command("python --version")
        assert exit_code == 0
        assert "Python" in stdout

    finally:
        sandbox.cleanup()


def test_sandbox_run_command_task_dir_set():
    """Test that TASK_DIR env var is set when task_dir is provided."""
    config = SandboxConfig(driver="tempdir")
    task_dir = Path("/some/task/dir")

    sandbox = Sandbox(config, task_id="test_task_dir", task_dir=task_dir)

    try:
        sandbox.setup()

        exit_code, stdout, _stderr = sandbox.run_command("python -c \"import os; print(os.environ['TASK_DIR'])\"")
        assert exit_code == 0
        assert stdout.strip() == str(task_dir)

    finally:
        sandbox.cleanup()


def test_sandbox_run_command_task_dir_absent():
    """Test that TASK_DIR env var is absent when task_dir is not provided."""
    config = SandboxConfig(driver="tempdir")

    sandbox = Sandbox(config, task_id="test_no_task_dir")

    try:
        sandbox.setup()

        exit_code, stdout, _stderr = sandbox.run_command(
            "python -c \"import os; print(os.environ.get('TASK_DIR', 'NOT_SET'))\""
        )
        assert exit_code == 0
        assert stdout.strip() == "NOT_SET"

    finally:
        sandbox.cleanup()


def test_sandbox_run_command_uses_agent_command_base_path(monkeypatch, tmp_path):
    """Criteria commands can be pinned to the PATH seen by the agent."""
    from tests._path_helpers import write_uip_shim

    stale_bin = tmp_path / "stale"
    agent_bin = tmp_path / "agent"
    stale_bin.mkdir()
    agent_bin.mkdir()
    write_uip_shim(stale_bin, "stale")
    write_uip_shim(agent_bin, "agent")
    monkeypatch.setenv("PATH", str(stale_bin))

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_agent_path")

    try:
        sandbox.setup()
        sandbox.set_command_base_path(f"{agent_bin}{os.pathsep}{stale_bin}")

        exit_code, stdout, _stderr = sandbox.run_command("uip")
        assert exit_code == 0
        assert stdout.strip() == "agent"
        # Read-only view exposes the same value `set_…` accepts.
        assert sandbox.command_base_path == f"{agent_bin}{os.pathsep}{stale_bin}"
    finally:
        sandbox.cleanup()


def test_write_uip_shim_rejects_unsafe_labels(tmp_path):
    """Foot-gun guard: labels are restricted to a safe ASCII alphabet."""
    import pytest as _pytest

    from tests._path_helpers import write_uip_shim

    with _pytest.raises(ValueError, match="label must match"):
        write_uip_shim(tmp_path, "; rm -rf /")
    with _pytest.raises(ValueError, match="label must match"):
        write_uip_shim(tmp_path, "$(whoami)")


def test_sandbox_with_packages():
    """Test sandbox with package installation."""
    from coder_eval.models import PythonEnvConfig

    config = SandboxConfig(driver="tempdir", python=PythonEnvConfig(env_packages=["requests"]))

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
    config = SandboxConfig(driver="tempdir", python=None)

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
    config = SandboxConfig(driver="tempdir", python=None)

    sandbox = Sandbox(config, task_id="test_timeout")

    try:
        sandbox.setup()

        # Run a command that sleeps longer than timeout
        exit_code, _stdout, stderr = sandbox.run_command('python -c "import time; time.sleep(10)"', timeout=0.1)
        assert exit_code == -1
        assert "timed out" in stderr.lower()

    finally:
        sandbox.cleanup()


def test_sandbox_preserve():
    """Test preserving sandbox to artifact directory."""
    config = SandboxConfig(driver="tempdir", python=None)

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


# ==================== Persistent Target Dir Tests ====================


def test_sandbox_setup_with_target_dir(tmp_path):
    """Test sandbox setup with a persistent target directory instead of tempdir."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_target_dir")

    target = tmp_path / "artifacts" / "test_target_dir"
    sandbox_dir = sandbox.setup(target_dir=target)

    assert sandbox_dir == target
    assert sandbox_dir.exists()
    assert sandbox.is_persistent

    # Create a test file
    (sandbox_dir / "hello.txt").write_text("persistent")

    # Cleanup should NOT delete the directory
    sandbox.cleanup(preserve=False)
    assert target.exists()
    assert (target / "hello.txt").read_text() == "persistent"


def test_sandbox_setup_target_dir_cleanup_on_exit_false(tmp_path):
    """Test that cleanup is a no-op when target_dir was used."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_noop_cleanup")

    target = tmp_path / "persist"
    sandbox.setup(target_dir=target)
    (target / "data.txt").write_text("keep me")

    # Multiple cleanups should all be no-ops
    sandbox.cleanup(preserve=False)
    sandbox.cleanup(preserve=False)
    assert target.exists()
    assert (target / "data.txt").exists()


def test_sandbox_setup_target_dir_failure_cleans_up(tmp_path, monkeypatch):
    """Test that a failed setup with target_dir still cleans up and resets state."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_fail_cleanup")

    target = tmp_path / "will_fail"

    # Force _setup_template to raise
    monkeypatch.setattr(sandbox, "_setup_template", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        sandbox.setup(target_dir=target)

    # Directory should be cleaned up and state reset
    assert not target.exists()
    assert sandbox.sandbox_dir is None
    assert not sandbox.is_persistent


def test_sandbox_preserve_to_self_referential_guard(tmp_path):
    """Test that preserve_to() is a no-op when sandbox is already at the target path."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_self_ref")

    # Set up sandbox in a persistent target dir (simulates orchestrator behavior)
    artifacts_dir = tmp_path / "artifacts"
    target = artifacts_dir / "test_self_ref"
    sandbox.setup(target_dir=target)

    # Create a test file to verify contents are preserved
    (target / "result.txt").write_text("important output")

    # Call preserve_to with the parent artifacts dir — this would normally
    # copy sandbox_dir to artifacts_dir/task_id, but since sandbox is already
    # there, the guard should short-circuit and return the existing path.
    preserved_path = sandbox.preserve_to(artifacts_dir)

    assert preserved_path == target
    assert preserved_path.exists()
    assert (preserved_path / "result.txt").read_text() == "important output"


def test_sandbox_persistent_full_flow(tmp_path):
    """Integration test: persistent sandbox → files created → cleanup is no-op → files remain."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_flow")

    target = tmp_path / "artifacts" / "test_flow"
    sandbox_dir = sandbox.setup(target_dir=target)

    # Simulate agent work
    (sandbox_dir / "hello.py").write_text("print('hello')")
    (sandbox_dir / "subdir").mkdir()
    (sandbox_dir / "subdir" / "data.json").write_text('{"key": "value"}')

    assert sandbox.is_persistent

    # Cleanup should be a no-op for persistent dirs
    sandbox.cleanup(preserve=False)

    # All files should still be there
    assert target.exists()
    assert (target / "hello.py").read_text() == "print('hello')"
    assert (target / "subdir" / "data.json").read_text() == '{"key": "value"}'

    # sandbox_dir should NOT be reset (cleanup is a no-op)
    assert sandbox.sandbox_dir is not None


def test_sandbox_default_setup_still_uses_tempdir():
    """Test that setup() without target_dir still creates a temp directory."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_default_temp")

    try:
        sandbox_dir = sandbox.setup()
        assert sandbox_dir.exists()
        assert not sandbox.is_persistent
        assert "coder_eval_test_default_temp_" in str(sandbox_dir)
    finally:
        sandbox.cleanup()


# ==================== Snapshot Tests ====================


@pytest.mark.asyncio
async def test_snapshot_full():
    """Test full snapshot creation."""
    config = SandboxConfig(driver="tempdir")
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
    config = SandboxConfig(driver="tempdir")
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
    config = SandboxConfig(driver="tempdir")
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
    config = SandboxConfig(driver="tempdir")
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
    config = SandboxConfig(driver="tempdir")
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
    config = SandboxConfig(driver="tempdir")
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

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_logging")

    try:
        sandbox.setup()

        # Run command that produces output
        with caplog.at_level(logging.DEBUG):
            exit_code, stdout, stderr = sandbox.run_command(
                "python -c \"import sys; print('Hello'); print('Error', file=sys.stderr)\""
            )

        # Verify command succeeded
        assert exit_code == 0
        assert "Hello" in stdout
        assert "Error" in stderr

        # Verify logs were created with correct messages
        log_messages = [record.message for record in caplog.records if record.name == "coder_eval.sandbox"]
        assert any("Command 'python" in msg and "exited with code 0" in msg for msg in log_messages)
        assert any("STDOUT:" in msg and "Hello" in msg for msg in log_messages)
        assert any("STDERR:" in msg and "Error" in msg for msg in log_messages)

    finally:
        sandbox.cleanup()


def test_sandbox_run_command_timeout_logging(caplog, clean_logging):
    """Test that timeouts are logged at WARNING level."""
    import logging

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_timeout")

    try:
        sandbox.setup()

        with caplog.at_level(logging.WARNING):
            exit_code, _stdout, _stderr = sandbox.run_command('python -c "import time; time.sleep(10)"', timeout=0.1)

        # Verify timeout logged at WARNING
        assert exit_code == -1
        log_messages = [record.message for record in caplog.records if record.name == "coder_eval.sandbox"]
        assert any("timed out after 0.1 seconds" in msg for msg in log_messages)
        assert logging.WARNING in [rec.levelno for rec in caplog.records]

    finally:
        sandbox.cleanup()


def test_sandbox_run_command_empty_output_not_logged(caplog, clean_logging):
    """Test that empty stdout/stderr is not logged."""
    import logging

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_empty")

    try:
        sandbox.setup()

        with caplog.at_level(logging.DEBUG):
            sandbox.run_command('python -c ""')  # Command with no output

        # Should log command completion but not empty output blocks
        log_messages = [record.message for record in caplog.records if record.name == "coder_eval.sandbox"]
        assert any("exited with code 0" in msg for msg in log_messages)
        assert not any("STDOUT:" in msg for msg in log_messages)  # Empty, not logged
        assert not any("STDERR:" in msg for msg in log_messages)

    finally:
        sandbox.cleanup()


def test_mock_path_dirs_unset_returns_empty():
    """No mock_path_dirs configured -> resolved_mock_path_dirs is []. Opt-in by design."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="mock_unset")

    try:
        sandbox.setup()
        assert sandbox.resolved_mock_path_dirs == []
    finally:
        sandbox.cleanup()


def test_mock_path_dirs_missing_directory_skipped():
    """Configured directory that doesn't exist on disk is filtered out, not raised."""
    config = SandboxConfig(driver="tempdir", python=None, mock_path_dirs=["does_not_exist"])
    sandbox = Sandbox(config, task_id="mock_missing")

    try:
        sandbox.setup()
        assert sandbox.resolved_mock_path_dirs == []
    finally:
        sandbox.cleanup()


def test_mock_path_dirs_chmod_applied_to_plain_files():
    """Plain files in a configured mock dir get +x; subdirectories are skipped.

    Anchors the contract documented on SandboxConfig.mock_path_dirs: each listed
    directory yields executable mock binaries. Subdirectories under it are treated
    as fixtures, not bins, and must NOT be touched.
    """
    from coder_eval.models.templates import StarterFile, StarterFilesSource

    config = SandboxConfig(
        driver="tempdir",
        python=None,
        template_sources=[
            StarterFilesSource(
                files=[
                    StarterFile(path="mocks/uip", content="#!/bin/sh\necho mock-uip\n"),
                    StarterFile(path="mocks/fixtures/data.json", content='{"x": 1}'),
                ]
            )
        ],
        mock_path_dirs=["mocks"],
    )
    sandbox = Sandbox(config, task_id="mock_chmod")

    try:
        sandbox_dir = sandbox.setup()

        mocks_dir = sandbox_dir / "mocks"
        # Sandbox returns the absolute path of the mocks dir for PATH-prepend.
        assert sandbox.resolved_mock_path_dirs == [mocks_dir.resolve()]

        # Plain file under mocks/ has the executable bit set on POSIX.
        # Windows does not represent +x in st_mode (execution is governed by
        # PATHEXT and ACLs), so the chmod call is a no-op there -- skip the
        # assertion rather than fake-pass on a constant.
        mock_uip = mocks_dir / "uip"
        if os.name != "nt":
            assert mock_uip.stat().st_mode & 0o111 != 0
        # Nested fixture file is preserved on disk; the chmod loop skips
        # subdirectories so we don't accidentally mark fixtures executable.
        assert (mocks_dir / "fixtures" / "data.json").is_file()
    finally:
        sandbox.cleanup()


def test_mock_path_dirs_rejects_traversal():
    """Sandbox-escaping entries (relative or absolute) must raise, not silently chmod the host.

    Mirrors the containment check enforced for `template_dir.mount_point` and the path-traversal
    rejection enforced for `starter_files`. Even with trusted task authors, a typo like
    `mock_path_dirs: ["../mocks"]` would otherwise let `_prepare_mock_path_dirs` apply +x to
    every file directly under the resolved escape target.
    """
    # Relative traversal: ".." escapes the sandbox parent.
    config_relative = SandboxConfig(driver="tempdir", python=None, mock_path_dirs=["../escape"])
    sandbox = Sandbox(config_relative, task_id="mock_traversal_relative")
    with pytest.raises(RuntimeError, match="mock_path_dirs entry escapes sandbox"):
        sandbox.setup()

    # Absolute traversal: joining an absolute path discards the sandbox prefix.
    # Pick a directory that exists on the target OS so we exercise the traversal
    # check, not the `is_dir()` filter.
    abs_target = "C:\\Windows" if os.name == "nt" else "/tmp"
    config_absolute = SandboxConfig(driver="tempdir", python=None, mock_path_dirs=[abs_target])
    sandbox = Sandbox(config_absolute, task_id="mock_traversal_absolute")
    with pytest.raises(RuntimeError, match="mock_path_dirs entry escapes sandbox"):
        sandbox.setup()


def test_mock_path_dirs_resolved_order_matches_config():
    """Resolved list preserves the order entries appear in config (PATH precedence matters)."""
    from coder_eval.models.templates import StarterFile, StarterFilesSource

    config = SandboxConfig(
        driver="tempdir",
        python=None,
        template_sources=[
            StarterFilesSource(
                files=[
                    StarterFile(path="a/x", content="x"),
                    StarterFile(path="b/y", content="y"),
                ]
            )
        ],
        mock_path_dirs=["b", "a"],
    )
    sandbox = Sandbox(config, task_id="mock_order")

    try:
        sandbox_dir = sandbox.setup()
        resolved = sandbox.resolved_mock_path_dirs
        assert resolved == [(sandbox_dir / "b").resolve(), (sandbox_dir / "a").resolve()]
    finally:
        sandbox.cleanup()


# ── MST-9674: Node / npm resolution isolation ────────────────────────────


def test_run_command_env_isolates_node_resolution():
    """NODE_PATH and NPM_CONFIG_PREFIX are pinned to the sandbox."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_node_isolation")
    try:
        sandbox_dir = sandbox.setup()
        env = sandbox._build_run_command_env()
        assert env["NODE_PATH"] == ""
        assert env["NPM_CONFIG_PREFIX"] == str(sandbox_dir / ".npm-prefix")
        # Parent env should still flow through so the agent's tools remain reachable.
        assert "PATH" in env
    finally:
        sandbox.cleanup()


def test_check_parent_node_modules_contamination_reports_scoped_offender(tmp_path, caplog):
    """An ancestor with any populated node_modules/ is reported but not removed.

    Uses a scoped package (``@some-org/some-tool``) to mirror the original
    MST-9674 contamination shape; the check itself is now scope-agnostic.
    """
    parent = tmp_path / "fake-home"
    contam = parent / "node_modules" / "@some-org" / "some-tool"
    contam.mkdir(parents=True)
    (contam / "package.json").write_text('{"name":"@some-org/some-tool","version":"0.9.0"}')

    inner = parent / "evals" / "skills" / "sandbox-xyz"
    inner.mkdir(parents=True)

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_contam_detect")
    sandbox.sandbox_dir = inner

    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="coder_eval.sandbox"):
        offenders = sandbox._check_parent_node_modules_contamination()

    expected = (parent / "node_modules").resolve()
    assert expected in [o.resolve() for o in offenders]
    # Auto-remediation is intentionally off — the install must still exist.
    assert contam.exists()
    # Logged message names the contaminated dir and lists what's inside.
    matching = [r for r in caplog.records if "Parent-dir node_modules contamination" in r.message]
    assert matching
    assert "@some-org" in matching[0].message


def test_check_parent_node_modules_contamination_reports_unscoped_offender(tmp_path, caplog):
    """An ancestor node_modules/ holding any installed package is reported.

    Locks in the generic check: previously the helper only caught
    ``@uipath/*`` and would have missed unscoped packages, leaving
    coder_eval consumers in other ecosystems uncovered.
    """
    parent = tmp_path / "fake-home"
    contam = parent / "node_modules" / "lodash"
    contam.mkdir(parents=True)
    (contam / "package.json").write_text('{"name":"lodash","version":"4.0.0"}')

    inner = parent / "evals" / "sandbox-abc"
    inner.mkdir(parents=True)

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_unscoped_detect")
    sandbox.sandbox_dir = inner

    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="coder_eval.sandbox"):
        offenders = sandbox._check_parent_node_modules_contamination()

    expected = (parent / "node_modules").resolve()
    assert expected in [o.resolve() for o in offenders]
    matching = [r for r in caplog.records if "lodash" in r.message]
    assert matching, "warning should list the contaminating package name"


def test_check_parent_node_modules_contamination_ignores_dot_entries(tmp_path):
    """``node_modules`` with only ``.bin`` / ``.cache`` does not trigger a warning.

    Those entries are package-manager bookkeeping, not installed packages
    that would shadow a sandbox-local install via parent-walking
    resolution.
    """
    parent = tmp_path / "fake-home"
    (parent / "node_modules" / ".bin").mkdir(parents=True)
    (parent / "node_modules" / ".cache").mkdir(parents=True)

    inner = parent / "sandbox-abc"
    inner.mkdir(parents=True)

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_dot_only")
    sandbox.sandbox_dir = inner

    assert sandbox._check_parent_node_modules_contamination() == []


def test_check_parent_node_modules_contamination_clean_tree(tmp_path):
    """No ancestor has a node_modules/ — returns empty list, logs nothing."""
    parent = tmp_path / "clean-tree"
    inner = parent / "sandbox-abc"
    inner.mkdir(parents=True)

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_clean_tree")
    sandbox.sandbox_dir = inner

    assert sandbox._check_parent_node_modules_contamination() == []


def test_run_command_npm_prefix_visible_to_subprocess():
    """End-to-end: NPM_CONFIG_PREFIX shows up in the actual command env."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_npm_prefix_e2e")
    try:
        sandbox_dir = sandbox.setup()
        exit_code, stdout, _stderr = sandbox.run_command(
            "python -c \"import os; print(os.environ.get('NPM_CONFIG_PREFIX', ''))\""
        )
        assert exit_code == 0
        assert stdout.strip() == str(sandbox_dir / ".npm-prefix")
    finally:
        sandbox.cleanup()
