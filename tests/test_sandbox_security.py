"""Security tests for sandbox - path traversal and absolute path validation.

Tests prevent malicious task definitions from escaping the sandbox.
"""

import os
from pathlib import Path

import pytest

from coder_eval.models.sandbox import SandboxConfig
from coder_eval.models.templates import StarterFile, StarterFilesSource
from coder_eval.sandbox import Sandbox


def test_starter_files_rejects_path_traversal(tmp_path):
    """Test that ../paths in starter files raise security error.

    Hypothesis: Malicious task definitions should not escape sandbox.
    Expected: RuntimeError raised before any file creation.

    Context: Lines 205-208 in sandbox.py implement path traversal guard.
    """
    # Create starter files with path traversal attempt
    malicious_files = StarterFilesSource(files=[StarterFile(path="../evil.txt", content="malicious")])

    config = SandboxConfig(
        driver="tempdir",
        template_sources=[malicious_files],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Attempt to apply starter files
    with pytest.raises(RuntimeError, match="Invalid file path \\(outside sandbox\\)"):
        sandbox._apply_starter_files_source(malicious_files)

    # Verify file not created outside sandbox
    evil_path = tmp_path / "evil.txt"
    assert not evil_path.exists(), "File should not be created outside sandbox"


def test_starter_files_rejects_absolute_paths(tmp_path):
    """Test that absolute paths in starter files raise security error.

    Hypothesis: Absolute paths like /etc/passwd should be blocked.
    Expected: RuntimeError raised before any file creation.

    Context: Lines 205-208 in sandbox.py implement path validation.
    """
    # Create starter files with absolute path
    malicious_files = StarterFilesSource(files=[StarterFile(path="/etc/passwd", content="malicious")])

    config = SandboxConfig(
        driver="tempdir",
        template_sources=[malicious_files],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Attempt to apply starter files
    with pytest.raises(RuntimeError, match="Invalid file path \\(outside sandbox\\)"):
        sandbox._apply_starter_files_source(malicious_files)

    # Verify system file not modified (only on Unix where /etc/passwd exists)
    if os.name != "nt":
        assert Path("/etc/passwd").read_text() != "malicious", "System file should not be modified"


def test_starter_files_rejects_nested_traversal(tmp_path):
    """Test that deeply nested path traversal is also blocked.

    Hypothesis: Multiple ../ components should also be caught.
    Expected: RuntimeError raised.
    """
    malicious_files = StarterFilesSource(files=[StarterFile(path="../../../../../../etc/passwd", content="malicious")])

    config = SandboxConfig(
        driver="tempdir",
        template_sources=[malicious_files],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    with pytest.raises(RuntimeError, match="Invalid file path \\(outside sandbox\\)"):
        sandbox._apply_starter_files_source(malicious_files)


def test_starter_files_allows_safe_paths(tmp_path):
    """Test that safe relative paths within sandbox work correctly.

    Hypothesis: Normal file operations should work without restriction.
    Expected: File created successfully within sandbox.
    """
    safe_files = StarterFilesSource(
        files=[
            StarterFile(path="src/main.py", content="print('hello')"),
            StarterFile(path="tests/test_main.py", content="def test(): pass"),
        ]
    )

    config = SandboxConfig(
        driver="tempdir",
        template_sources=[safe_files],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Should succeed without errors
    sandbox._apply_starter_files_source(safe_files)

    # Verify files created
    assert (sandbox.sandbox_dir / "src" / "main.py").exists()
    assert (sandbox.sandbox_dir / "tests" / "test_main.py").exists()
    assert (sandbox.sandbox_dir / "src" / "main.py").read_text() == "print('hello')"
