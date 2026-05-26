"""Tests for the sandbox manager."""

import os
import sys
from pathlib import Path

import pytest

from coder_eval.models import SandboxConfig
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


def _install_fake_uip_tree(root: Path) -> Path:
    """Create a realistic ``node_modules/@uipath/cli/dist/`` install under ``root``.

    Returns the `bin/` dir containing a `uip` symlink that resolves into the
    fake cli dist. Used by MST-9795 PLUGIN_TOOLS_DIR-pin tests so the
    canonical tools dir is at a known, isolated tmp path.
    """
    tools_dir = root / "node_modules" / "@uipath"
    cli_dist = tools_dir / "cli" / "dist"
    cli_dist.mkdir(parents=True)
    real_uip = cli_dist / "index.js"
    real_uip.write_text("#!/usr/bin/env node\nconsole.log('1.1.0');\n")
    real_uip.chmod(0o755)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    # Symlink mirrors how `bun install -g @uipath/cli` materializes the
    # binary in production (~/.bun/bin/uip -> install/global/node_modules/...).
    (bin_dir / "uip").symlink_to(real_uip)
    return bin_dir


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Fixture uses POSIX symlink + extensionless `uip`; `shutil.which` on Windows needs PATHEXT match.",
)
def test_refresh_plugin_tools_dir_resolves_canonical_at_symlink_target(monkeypatch, tmp_path):
    """MST-9795: discovery follows the `uip` symlink and walks up to @uipath."""
    bin_dir = _install_fake_uip_tree(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_plugin_tools_dir_resolve")
    try:
        sandbox.setup()
        # `setup()` already called `_refresh_plugin_tools_dir()` for us.
        expected = str((tmp_path / "node_modules" / "@uipath").resolve(strict=True))
        assert sandbox.plugin_tools_dir == expected
    finally:
        sandbox.cleanup()


def test_refresh_plugin_tools_dir_returns_none_when_uip_absent(monkeypatch, tmp_path):
    """No `uip` on PATH → pin stays unset; CLI falls back to walk-based discovery."""
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_plugin_tools_dir_absent")
    try:
        sandbox.setup()
        assert sandbox.plugin_tools_dir is None
    finally:
        sandbox.cleanup()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Fixture uses POSIX symlink + extensionless `uip`; `shutil.which` on Windows needs PATHEXT match.",
)
def test_refresh_plugin_tools_dir_none_when_uip_outside_uipath_tree(monkeypatch, tmp_path):
    """`uip` resolves but is NOT under `.../node_modules/@uipath` → pin stays unset.

    Covers development monorepo runs where the on-PATH `uip` is a wrapper
    script or globally-installed shim that doesn't live inside an `@uipath`
    package tree.
    """
    # Install `uip` directly under tmp_path/bin pointing at a sibling JS file —
    # no `node_modules/@uipath` anywhere in the parent chain.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    standalone = tmp_path / "uip-standalone.js"
    standalone.write_text("#!/usr/bin/env node\nconsole.log('1.1.0');\n")
    standalone.chmod(0o755)
    (bin_dir / "uip").symlink_to(standalone)
    monkeypatch.setenv("PATH", str(bin_dir))
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_plugin_tools_dir_outside_tree")
    try:
        sandbox.setup()
        assert sandbox.plugin_tools_dir is None
    finally:
        sandbox.cleanup()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Fixture uses POSIX symlink + extensionless `uip`; `shutil.which` on Windows needs PATHEXT match.",
)
def test_set_command_base_path_refreshes_plugin_tools_dir(monkeypatch, tmp_path):
    """PATH alignment from the agent should re-derive the canonical tools dir.

    Without this, criterion subprocesses might pin to a tools dir derived from
    a `uip` that the agent never resolved to.
    """
    pre_root = tmp_path / "pre"
    pre_root.mkdir()
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    pre_bin = _install_fake_uip_tree(pre_root)
    agent_bin = _install_fake_uip_tree(agent_root)
    # Initial PATH points at the "pre" tools tree; agent PATH override (later)
    # points at a different "agent" tree. `set_command_base_path` must
    # re-resolve and update.
    monkeypatch.setenv("PATH", str(pre_bin))
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_plugin_tools_dir_refresh")
    try:
        sandbox.setup()
        pre_expected = str((pre_root / "node_modules" / "@uipath").resolve(strict=True))
        assert sandbox.plugin_tools_dir == pre_expected
        sandbox.set_command_base_path(str(agent_bin))
        agent_expected = str((agent_root / "node_modules" / "@uipath").resolve(strict=True))
        assert sandbox.plugin_tools_dir == agent_expected
    finally:
        sandbox.cleanup()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Fixture uses POSIX symlink + extensionless `uip`; `shutil.which` on Windows needs PATHEXT match.",
)
def test_build_run_command_env_exports_plugin_tools_dir(monkeypatch, tmp_path):
    """`PLUGIN_TOOLS_DIR` env var must reach criterion subprocesses."""
    bin_dir = _install_fake_uip_tree(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_plugin_tools_dir_env")
    try:
        sandbox.setup()
        env = sandbox._build_run_command_env()
        expected = str((tmp_path / "node_modules" / "@uipath").resolve(strict=True))
        assert env["PLUGIN_TOOLS_DIR"] == expected
    finally:
        sandbox.cleanup()


def test_build_run_command_env_omits_plugin_tools_dir_when_uip_absent(monkeypatch, tmp_path):
    """No `uip` on PATH → no `PLUGIN_TOOLS_DIR` exported (CLI uses default walks)."""
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.delenv("PLUGIN_TOOLS_DIR", raising=False)
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_plugin_tools_dir_env_absent")
    try:
        sandbox.setup()
        env = sandbox._build_run_command_env()
        assert "PLUGIN_TOOLS_DIR" not in env
    finally:
        sandbox.cleanup()


def test_build_run_command_env_preserves_external_plugin_tools_dir(monkeypatch, tmp_path):
    """Parent-process pin wins over the auto-derived one — operators can override."""
    bin_dir = _install_fake_uip_tree(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PLUGIN_TOOLS_DIR", "/override/node_modules/@uipath")
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_plugin_tools_dir_external_override")
    try:
        sandbox.setup()
        env = sandbox._build_run_command_env()
        assert env["PLUGIN_TOOLS_DIR"] == "/override/node_modules/@uipath"
    finally:
        sandbox.cleanup()


def test_maybe_remediate_home_plugins_pollution_off_by_default(monkeypatch, tmp_path):
    """No flag → never delete. The dir is a user-owned resource."""
    fake_home = tmp_path / "home"
    pollution = fake_home / "node_modules" / "@uipath"
    pollution.mkdir(parents=True)
    (pollution / "marker").write_text("STILL HERE")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv(Sandbox.REMEDIATE_HOME_PLUGINS_ENV, raising=False)
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_remediate_off")
    try:
        sandbox.setup()
        assert pollution.is_dir()
        assert (pollution / "marker").read_text() == "STILL HERE"
    finally:
        sandbox.cleanup()


def test_maybe_remediate_home_plugins_pollution_deletes_when_enabled(monkeypatch, tmp_path, caplog):
    """Flag on AND dir present AND under HOME → delete with WARNING log."""
    import logging as _logging

    fake_home = tmp_path / "home"
    pollution = fake_home / "node_modules" / "@uipath"
    pollution.mkdir(parents=True)
    (pollution / "marker").write_text("STALE")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv(Sandbox.REMEDIATE_HOME_PLUGINS_ENV, "1")
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_remediate_on")
    caplog.set_level(_logging.WARNING, logger="coder_eval.sandbox")
    try:
        sandbox.setup()
        assert not pollution.exists()
        assert any("MST-9795 remediation" in rec.message for rec in caplog.records)
    finally:
        sandbox.cleanup()


def test_maybe_remediate_home_plugins_pollution_no_op_when_dir_absent(monkeypatch, tmp_path):
    """Flag on but nothing to remediate → silent no-op (no crash)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()  # No node_modules/@uipath created.
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv(Sandbox.REMEDIATE_HOME_PLUGINS_ENV, "1")
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_remediate_absent")
    try:
        sandbox.setup()  # Must not raise.
    finally:
        sandbox.cleanup()


def test_maybe_remediate_home_plugins_pollution_refuses_when_home_is_root(monkeypatch, caplog):
    """Defense-in-depth: HOME=/ must NOT cause /node_modules/@uipath deletion."""
    import logging as _logging
    import shutil as _shutil
    from pathlib import Path as _Path

    monkeypatch.setenv("HOME", "/")
    monkeypatch.setenv(Sandbox.REMEDIATE_HOME_PLUGINS_ENV, "1")

    real_is_dir = _Path.is_dir
    real_resolve = _Path.resolve
    fake_root = _Path("/")
    fake_target = _Path("/node_modules/@uipath")

    def fake_is_dir(self):
        if self == fake_target:
            return True
        return real_is_dir(self)

    def fake_resolve(self, strict=False):
        if self == fake_target:
            return fake_target
        if self == fake_root:
            return fake_root
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(_Path, "is_dir", fake_is_dir)
    monkeypatch.setattr(_Path, "resolve", fake_resolve)

    # `sandbox.py` does `import shutil` — patching the global module's rmtree
    # reaches the same callable the sandbox sees, no dual-import needed.
    rmtree_calls: list[object] = []
    monkeypatch.setattr(_shutil, "rmtree", lambda *args, **_: rmtree_calls.append(args))

    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_remediate_home_root")
    caplog.set_level(_logging.WARNING, logger="coder_eval.sandbox")

    result = sandbox._maybe_remediate_home_plugins_pollution()
    assert result is None
    assert rmtree_calls == [], "rmtree must NOT be called when HOME resolves to filesystem root"
    assert any("filesystem root" in rec.message for rec in caplog.records)


def test_maybe_remediate_home_plugins_pollution_accepts_truthy_strings(monkeypatch, tmp_path):
    """Documented truthy strings (`true`, `yes`, case-insensitive) trigger."""
    for raw in ("true", "True", "YES", "1"):
        fake_home = tmp_path / f"home_{raw}"
        pollution = fake_home / "node_modules" / "@uipath"
        pollution.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv(Sandbox.REMEDIATE_HOME_PLUGINS_ENV, raw)
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id=f"test_remediate_{raw}")
        try:
            sandbox.setup()
            assert not pollution.exists(), f"Did not remediate for flag={raw!r}"
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


def test_sandbox_default_setup_handles_slashed_task_id():
    """Dataset row task IDs ("parent/row") must not break tempdir creation."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="dataset-example/alpha")

    try:
        sandbox_dir = sandbox.setup()
        assert sandbox_dir.exists()
        # Slash must be flattened so mkdtemp does not look for a missing parent dir.
        assert "/" not in sandbox_dir.name
        assert "coder_eval_dataset-example_alpha_" in sandbox_dir.name
    finally:
        sandbox.cleanup()


# ==================== preserve_to move semantics (MST-10032) ====================


def test_preserve_to_moves_instead_of_copying(tmp_path):
    """preserve_to() leaves no source dir behind (move, not copytree+rmtree)."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="move-task")

    try:
        original_dir = sandbox.setup()
        (original_dir / "output.txt").write_text("agent output")

        artifacts = tmp_path / "artifacts"
        preserved = sandbox.preserve_to(artifacts)

        # Original tempdir is gone the moment preserve_to returns -- no
        # second-pass rmtree needed during cleanup().
        assert not original_dir.exists()
        assert preserved.exists()
        assert (preserved / "output.txt").read_text() == "agent output"

        # State has flipped to persistent so cleanup() is a no-op.
        assert sandbox.sandbox_dir == preserved
        assert sandbox.is_persistent
        sandbox.cleanup()
        assert preserved.exists()
    finally:
        # Sandbox is_persistent now; tmp_path autoremoves the rest.
        pass


def test_preserve_to_creates_parent_dirs_for_slashed_task_id(tmp_path):
    """Dataset-row task IDs ("parent/row") need their parent created in artifacts."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="dataset/row-0")

    try:
        sandbox.setup()
        artifacts = tmp_path / "artifacts"
        preserved = sandbox.preserve_to(artifacts)
        assert preserved == artifacts / "dataset" / "row-0"
        assert preserved.exists()
    finally:
        sandbox.cleanup()


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


@pytest.mark.skipif(os.name == "nt", reason="printf-binary stunt relies on a POSIX shell")
def test_run_command_handles_non_utf8_output():
    """run_command must not raise UnicodeDecodeError on non-UTF-8 stdout.

    Agents can produce raw bytes (binary tool output, locale-encoded errors on
    Windows). With ``errors="replace"`` the decode falls back to U+FFFD instead
    of crashing the run mid-criterion.
    """
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_non_utf8")
    try:
        sandbox.setup()
        # printf interprets \x80 as a raw byte — invalid UTF-8 lead byte.
        exit_code, stdout, _stderr = sandbox.run_command(r"printf '\x80\x81'")
        assert exit_code == 0
        # Got back a string (replacement char or any decoded form), not a crash.
        assert isinstance(stdout, str)
    finally:
        sandbox.cleanup()


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
