"""Tests for NodeEnvConfig and SandboxConfig.node field."""

import json
from unittest.mock import patch

from coder_eval.models import NodeEnvConfig, SandboxConfig
from coder_eval.sandbox import Sandbox


def test_node_env_config_defaults():
    """NodeEnvConfig has empty env_packages by default."""
    config = NodeEnvConfig()
    assert config.env_packages == []


def test_node_env_config_with_packages():
    """NodeEnvConfig accepts package specifiers."""
    config = NodeEnvConfig(env_packages=["@uipath/uipcli@0.1.5", "typescript@5.0.0"])
    assert len(config.env_packages) == 2
    assert config.env_packages[0] == "@uipath/uipcli@0.1.5"


def test_sandbox_config_node_default_none():
    """SandboxConfig.node defaults to None."""
    config = SandboxConfig()
    assert config.node is None


def test_sandbox_config_with_node():
    """SandboxConfig accepts node configuration."""
    config = SandboxConfig(node=NodeEnvConfig(env_packages=["@uipath/uipcli@0.1.5"]))
    assert config.node is not None
    assert config.node.env_packages == ["@uipath/uipcli@0.1.5"]


def test_sandbox_config_from_dict():
    """SandboxConfig can be created from dict (simulating YAML parsing)."""
    data = {"driver": "tempdir", "node": {"env_packages": ["@uipath/uipcli@0.1.5"]}}
    config = SandboxConfig(**data)
    assert config.node is not None
    assert config.node.env_packages == ["@uipath/uipcli@0.1.5"]


def test_install_node_packages_called_when_configured():
    """Sandbox._setup_tempdir calls _install_node_packages when node config has packages."""
    config = SandboxConfig(
        driver="tempdir",
        python=None,
        node=NodeEnvConfig(env_packages=["@uipath/uipcli@0.1.5"]),
    )
    sandbox = Sandbox(config, task_id="test_node")

    with patch.object(sandbox, "_install_node_packages") as mock_install:
        try:
            sandbox.setup()
            mock_install.assert_called_once()
        finally:
            sandbox.cleanup()


def test_install_node_packages_not_called_when_no_config():
    """Sandbox._setup_tempdir does NOT call _install_node_packages when node is None."""
    config = SandboxConfig(driver="tempdir", python=None, node=None)
    sandbox = Sandbox(config, task_id="test_no_node")

    with patch.object(sandbox, "_install_node_packages") as mock_install:
        try:
            sandbox.setup()
            mock_install.assert_not_called()
        finally:
            sandbox.cleanup()


def test_install_node_packages_not_called_when_empty_packages():
    """Sandbox._setup_tempdir does NOT call _install_node_packages when env_packages is empty."""
    config = SandboxConfig(driver="tempdir", python=None, node=NodeEnvConfig(env_packages=[]))
    sandbox = Sandbox(config, task_id="test_empty_node")

    with patch.object(sandbox, "_install_node_packages") as mock_install:
        try:
            sandbox.setup()
            mock_install.assert_not_called()
        finally:
            sandbox.cleanup()


def test_installed_tool_versions_accessible():
    """Sandbox.installed_tool_versions is a dict accessible after setup."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_versions")

    try:
        sandbox.setup()
        assert isinstance(sandbox.installed_tool_versions, dict)
        assert sandbox.installed_tool_versions == {}
    finally:
        sandbox.cleanup()


def test_run_command_includes_node_modules_bin_in_path():
    """run_command() includes node_modules/.bin in PATH when it exists."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_node_path")

    try:
        sandbox.setup()

        # Create node_modules/.bin and verify it appears in PATH
        node_bin = sandbox.sandbox_dir / "node_modules" / ".bin"
        node_bin.mkdir(parents=True)

        exit_code, stdout, _stderr = sandbox.run_command("python -c \"import os; print(os.environ['PATH'])\"")
        assert exit_code == 0
        assert str(node_bin) in stdout
    finally:
        sandbox.cleanup()


class TestCaptureNodeToolVersions:
    """Tests for _capture_node_tool_versions parsing logic."""

    def _setup_sandbox_with_packages(self, env_packages: list[str]) -> Sandbox:
        """Create a sandbox with node config, mocking actual npm install."""
        config = SandboxConfig(driver="tempdir", python=None, node=NodeEnvConfig(env_packages=env_packages))
        sandbox = Sandbox(config, task_id="test_versions")
        with patch.object(sandbox, "_install_node_packages"):
            sandbox.setup()
        return sandbox

    def _create_package_json(self, sandbox: Sandbox, pkg_path: str, version: str) -> None:
        """Create a fake package.json at node_modules/<pkg_path>/package.json."""
        assert sandbox.sandbox_dir is not None
        pkg_dir = sandbox.sandbox_dir / "node_modules" / pkg_path
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "package.json").write_text(json.dumps({"name": pkg_path, "version": version}))

    def test_scoped_with_version(self):
        """@uipath/uipcli@0.1.5 resolves to @uipath/uipcli."""
        sandbox = self._setup_sandbox_with_packages(["@uipath/uipcli@0.1.5"])
        try:
            self._create_package_json(sandbox, "@uipath/uipcli", "0.1.5")
            sandbox._capture_node_tool_versions()
            assert sandbox.installed_tool_versions == {"@uipath/uipcli": "0.1.5"}
        finally:
            sandbox.cleanup()

    def test_scoped_without_version(self):
        """@uipath/uipcli (no version) resolves to @uipath/uipcli."""
        sandbox = self._setup_sandbox_with_packages(["@uipath/uipcli"])
        try:
            self._create_package_json(sandbox, "@uipath/uipcli", "1.0.0")
            sandbox._capture_node_tool_versions()
            assert sandbox.installed_tool_versions == {"@uipath/uipcli": "1.0.0"}
        finally:
            sandbox.cleanup()

    def test_unscoped_with_version(self):
        """typescript@5.0.0 resolves to typescript."""
        sandbox = self._setup_sandbox_with_packages(["typescript@5.0.0"])
        try:
            self._create_package_json(sandbox, "typescript", "5.0.0")
            sandbox._capture_node_tool_versions()
            assert sandbox.installed_tool_versions == {"typescript": "5.0.0"}
        finally:
            sandbox.cleanup()

    def test_unscoped_without_version(self):
        """leftpad (no version) resolves to leftpad."""
        sandbox = self._setup_sandbox_with_packages(["leftpad"])
        try:
            self._create_package_json(sandbox, "leftpad", "0.0.1")
            sandbox._capture_node_tool_versions()
            assert sandbox.installed_tool_versions == {"leftpad": "0.0.1"}
        finally:
            sandbox.cleanup()

    def test_multiple_packages(self):
        """Multiple packages are all captured."""
        sandbox = self._setup_sandbox_with_packages(["@uipath/uipcli@0.1.5", "typescript@5.0.0"])
        try:
            self._create_package_json(sandbox, "@uipath/uipcli", "0.1.5")
            self._create_package_json(sandbox, "typescript", "5.0.0")
            sandbox._capture_node_tool_versions()
            assert sandbox.installed_tool_versions == {"@uipath/uipcli": "0.1.5", "typescript": "5.0.0"}
        finally:
            sandbox.cleanup()

    def test_missing_package_json_skipped(self):
        """Package without package.json is silently skipped."""
        sandbox = self._setup_sandbox_with_packages(["nonexistent@1.0.0"])
        try:
            sandbox._capture_node_tool_versions()
            assert sandbox.installed_tool_versions == {}
        finally:
            sandbox.cleanup()
