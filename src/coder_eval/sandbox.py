"""Sandbox manager for isolated execution environments."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .models import (
    FileChange,
    RepoSource,
    SandboxConfig,
    SnapshotManifest,
    SnapshotMode,
    StarterFilesSource,
    TemplateDirSource,
)
from .resources import get_ignore_patterns, should_ignore_path


# Module logger (inherits from coder_eval logger)
logger = logging.getLogger(__name__)


class Sandbox:
    """Manages sandboxed execution environments for agent tasks.

    Supports multiple drivers (tempdir, docker) and provides isolated
    environments with virtual environments and resource limits.
    """

    def __init__(self, config: SandboxConfig, task_id: str, task_dir: Path | None = None):
        """Initialize the sandbox.

        Args:
            config: Sandbox configuration
            task_id: Unique identifier for this task (used in paths)
            task_dir: Directory containing the task YAML file (exposed as TASK_DIR env var in run_command)
        """
        self.config = config
        self.task_id = task_id
        self.task_dir = task_dir
        self.sandbox_dir: Path | None = None
        self.venv_dir: Path | None = None
        self._cleanup_on_exit = True
        self.installed_tool_versions: dict[str, str] = {}

    @property
    def _venv_scripts_dir(self) -> Path | None:
        """Return the platform-appropriate scripts directory inside the venv."""
        if self.venv_dir is None:
            return None
        return self.venv_dir / ("Scripts" if os.name == "nt" else "bin")

    @property
    def is_persistent(self) -> bool:
        """Whether this sandbox was created with a persistent target directory.

        When True, the sandbox was created in a specific target directory (typically
        the artifacts directory) and will not be deleted on cleanup().
        """
        return not self._cleanup_on_exit

    def setup(self, target_dir: Path | None = None) -> Path:
        """Set up the sandbox environment.

        The sandbox is a plain temporary directory on the host -- there is no
        container or cgroup isolation.  Only command-level timeouts are enforced;
        memory and disk limits in :class:`ResourceLimits` are not enforced.

        Args:
            target_dir: If provided, use this directory instead of creating a temp dir.
                        The directory will NOT be deleted on cleanup (persistent mode).

        Returns:
            Path to the sandbox directory

        Raises:
            ValueError: If driver is not supported
            RuntimeError: If setup fails
        """
        if self.config.driver == "tempdir":
            return self._setup_tempdir(target_dir=target_dir)
        else:
            raise ValueError(f"Unsupported sandbox driver: {self.config.driver}")

    def _setup_tempdir(self, target_dir: Path | None = None) -> Path:
        """Set up a sandbox directory.

        Args:
            target_dir: If provided, use this directory instead of creating a temp dir.
                        Sets _cleanup_on_exit=False so cleanup() preserves the directory.

        Returns:
            Path to the sandbox directory
        """
        if target_dir is not None:
            # Persistent mode: work directly in the target directory
            target_dir.mkdir(parents=True, exist_ok=True)
            self.sandbox_dir = target_dir
            self._cleanup_on_exit = False
        else:
            # Default: create a temporary directory
            self.sandbox_dir = Path(tempfile.mkdtemp(prefix=f"coder_eval_{self.task_id}_"))

        try:
            # Setup template content (repo, directory, or inline files)
            self._setup_template()

            # Set up Python virtual environment (only if python config is provided)
            if self.config.python:
                self._setup_virtualenv()

                # Install required packages
                if self.config.python.env_packages:
                    self._install_packages()

            # Install Node.js packages
            if self.config.node and self.config.node.env_packages:
                self._install_node_packages()
        except Exception:
            # Clean up directory if setup fails partway through
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)
            self.sandbox_dir = None
            self._cleanup_on_exit = True  # Reset flag on failure
            raise

        return self.sandbox_dir

    def _apply_repo_source(self, source: RepoSource) -> None:
        """Clone a git repository into the sandbox.

        Args:
            source: Repository source configuration

        Raises:
            RuntimeError: If git clone or checkout fails

        Note:
            Repos are cloned into sandbox_dir/repo/ subdirectory.
            RepoSource should always be first (git clone requires empty target).
        """
        assert self.sandbox_dir is not None, "Sandbox directory not initialized"

        repo_dir = self.sandbox_dir / "repo"
        cmd = ["git", "clone", source.url, str(repo_dir)]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)

            # Checkout specific commit if specified
            if source.commit:
                subprocess.run(
                    ["git", "checkout", source.commit],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to clone repository: {e.stderr}") from e

    def _setup_template(self) -> None:
        """Setup template files/directory in sandbox.

        Applies template sources sequentially in order. Later sources can overwrite
        files from earlier sources (last-wins conflict resolution).
        """
        sources = self.config.template_sources or []

        for source in sources:
            if isinstance(source, RepoSource):
                self._apply_repo_source(source)
            elif isinstance(source, TemplateDirSource):
                self._apply_template_dir_source(source)
            elif isinstance(source, StarterFilesSource):
                self._apply_starter_files_source(source)

    def _apply_template_dir_source(self, source: TemplateDirSource) -> None:
        """Copy template directory contents to sandbox with overwrite tracking.

        Args:
            source: Template directory source configuration

        Raises:
            RuntimeError: If template directory doesn't exist or is not a directory
        """
        assert self.sandbox_dir is not None, "Sandbox directory not initialized"

        template_path = Path(source.path)
        logger = logging.getLogger(__name__)

        if not template_path.exists():
            raise RuntimeError(f"Template directory not found: {template_path}")

        if not template_path.is_dir():
            raise RuntimeError(f"Template path is not a directory: {template_path}")

        # Resolve mount point inside the sandbox and ensure it stays within bounds
        mount_root = (self.sandbox_dir / source.mount_point).resolve()
        sandbox_root = self.sandbox_dir.resolve()
        if mount_root != sandbox_root and sandbox_root not in mount_root.parents:
            raise RuntimeError(f"Template mount_point escapes sandbox: {source.mount_point!r} -> {mount_root}")
        mount_root.mkdir(parents=True, exist_ok=True)

        # Track overwrites for logging
        overwrites: set[str] = set()

        # Copy contents with ignore patterns
        for item in template_path.rglob("*"):
            if self._should_ignore_template_file(item):
                continue

            # Calculate relative path
            rel_path = item.relative_to(template_path)
            dest_path = mount_root / rel_path

            if item.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                # Track overwrites
                if dest_path.exists():
                    overwrites.add(str(rel_path))

                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest_path)

        # Enhanced logging based on overwrite count
        if overwrites:
            if len(overwrites) <= 5:
                logger.debug(f"Overwrote {len(overwrites)} files from {source.path}: {', '.join(sorted(overwrites))}")
            else:
                logger.debug(f"Overwrote {len(overwrites)} files from {source.path}")

    def _apply_starter_files_source(self, source: StarterFilesSource) -> None:
        """Create inline starter files in sandbox with overwrite tracking.

        Args:
            source: Starter files source configuration

        Raises:
            RuntimeError: If file path escapes sandbox (path traversal)
        """
        assert self.sandbox_dir is not None, "Sandbox directory not initialized"

        logger = logging.getLogger(__name__)
        overwrites: set[str] = set()

        for starter_file in source.files:
            file_path = self.sandbox_dir / starter_file.path

            # Security: prevent path traversal
            try:
                file_path.resolve().relative_to(self.sandbox_dir.resolve())
            except ValueError as e:
                raise RuntimeError(f"Invalid file path (outside sandbox): {starter_file.path}") from e

            # Track overwrites
            if file_path.exists():
                overwrites.add(starter_file.path)

            # Create parent directories
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Write file content
            file_path.write_text(starter_file.content, encoding="utf-8")

        # Enhanced logging based on overwrite count
        if overwrites:
            if len(overwrites) <= 5:
                logger.debug(f"Overwrote {len(overwrites)} files from starter_files: {', '.join(sorted(overwrites))}")
            else:
                logger.debug(f"Overwrote {len(overwrites)} files from starter_files")

    def _should_ignore_template_file(self, path: Path) -> bool:
        """Check if template file/directory should be ignored."""
        patterns = get_ignore_patterns(self.config.ignore_patterns)
        return should_ignore_path(path, patterns)

    def _setup_virtualenv(self) -> None:
        """Create a Python virtual environment in the sandbox."""
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox directory not initialized")

        self.venv_dir = self.sandbox_dir / ".venv"

        # Use uv to create virtual environment (faster than venv)
        try:
            # Check if uv is available
            subprocess.run(["uv", "--version"], check=True, capture_output=True, timeout=5)
            # Use uv to create venv
            cmd = ["uv", "venv", str(self.venv_dir)]
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback to standard venv if uv is not available
            import venv

            venv.create(self.venv_dir, with_pip=True)

    def _install_packages(self) -> None:
        """Install required Python packages in the virtual environment."""
        if not self.config.python or not self.config.python.env_packages or not self.venv_dir:
            return

        # Get path to pip in the virtual environment
        scripts_dir = self._venv_scripts_dir
        assert scripts_dir is not None  # guaranteed by venv_dir guard above
        pip_path = scripts_dir / "pip"

        # Try uv first, fall back to pip
        try:
            subprocess.run(["uv", "--version"], check=True, capture_output=True, timeout=5)
            # Use uv pip for faster installation
            cmd = ["uv", "pip", "install", *self.config.python.env_packages]
            env = os.environ.copy()
            env["VIRTUAL_ENV"] = str(self.venv_dir)
            env["PATH"] = f"{scripts_dir}{os.pathsep}{env['PATH']}"
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback to regular pip
            cmd = [str(pip_path), "install", *self.config.python.env_packages]
            env = None

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300, env=env)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to install packages: {e.stderr}") from e

    def _install_node_packages(self) -> None:
        """Install npm packages locally in the sandbox directory."""
        if not self.config.node or not self.config.node.env_packages or not self.sandbox_dir:
            return

        packages = self.config.node.env_packages

        # Try bun first, fall back to npm
        try:
            subprocess.run(["bun", "--version"], check=True, capture_output=True, timeout=5)
            cmd = ["bun", "add", *packages]
        except (subprocess.CalledProcessError, FileNotFoundError):
            cmd = ["npm", "install", *packages]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300, cwd=self.sandbox_dir)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to install node packages: {e.stderr}") from e

        # Capture installed versions
        self._capture_node_tool_versions()

    def _capture_node_tool_versions(self) -> None:
        """Capture installed versions for explicitly requested npm packages only."""
        if not self.sandbox_dir or not self.config.node:
            return

        node_modules = self.sandbox_dir / "node_modules"
        if not node_modules.exists():
            return

        # Extract package names from specifiers (strip version: "@uipath/cli@0.1.5" -> "@uipath/cli")
        requested_names: set[str] = set()
        for spec in self.config.node.env_packages:
            if spec.startswith("@"):
                # Scoped: "@scope/pkg@version" -> "@scope/pkg"
                requested_names.add("@" + spec[1:].split("@", 1)[0])
            else:
                # Unscoped: "pkg@version" -> "pkg"
                requested_names.add(spec.split("@", 1)[0])

        # Read package.json for each requested package
        for name in requested_names:
            if name.startswith("@") and "/" in name:
                # Scoped: @scope/pkg -> node_modules/@scope/pkg
                scope, pkg = name.split("/", 1)
                pkg_dir = node_modules / scope / pkg
            else:
                pkg_dir = node_modules / name

            pkg_json = pkg_dir / "package.json"
            if pkg_json.exists():
                try:
                    data = json.loads(pkg_json.read_text())
                    version = data.get("version", "unknown")
                    self.installed_tool_versions[name] = version
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug(
                        "Failed to read or parse package.json for %s at %s: %s",
                        name,
                        pkg_json,
                        exc,
                    )

    def run_command(self, command: str, timeout: float | int | None = None) -> tuple[int, str, str]:
        """Run a command in the sandbox environment.

        Args:
            command: Command to execute
            timeout: Timeout in seconds (uses config default if not specified)

        Returns:
            Tuple of (exit_code, stdout, stderr)

        Raises:
            RuntimeError: If sandbox is not set up
            subprocess.TimeoutExpired: If command times out
        """
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox not set up. Call setup() first.")

        # Use timeout from argument or config
        if timeout is None:
            timeout = self.config.limits.timeout

        # Prepare environment with virtual environment activated
        env = os.environ.copy()
        if self.venv_dir:
            env["VIRTUAL_ENV"] = str(self.venv_dir)
            env["PATH"] = f"{self._venv_scripts_dir}{os.pathsep}{env['PATH']}"
        # Add node_modules/.bin to PATH if it exists
        node_bin = self.sandbox_dir / "node_modules" / ".bin"
        if node_bin.exists():
            env["PATH"] = f"{node_bin}{os.pathsep}{env['PATH']}"

        if self.task_dir:
            env["TASK_DIR"] = str(self.task_dir)

        try:
            # Shell execution is intentional for sandbox - allows pipes, redirects, and complex commands
            result = subprocess.run(
                command,
                shell=True,  # nosec B602 - Required for sandbox command execution
                cwd=self.sandbox_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            # Log command completion
            logger.debug(f"Command '{command}' exited with code {result.returncode}")

            # Log stdout if non-empty (pre-strip for cleaner code)
            stdout_content = result.stdout.strip()
            if stdout_content:
                logger.debug(f"STDOUT:\n---\n{stdout_content}\n---")

            # Log stderr if non-empty
            stderr_content = result.stderr.strip()
            if stderr_content:
                logger.debug(f"STDERR:\n---\n{stderr_content}\n---")

            return result.returncode, result.stdout, result.stderr

        except subprocess.TimeoutExpired:
            error_msg = f"Command '{command}' timed out after {timeout} seconds"
            logger.warning(error_msg)
            return -1, "", error_msg

    # NOTE: get_file_content, file_exists, and list_files intentionally do NOT validate
    # path traversal. The sandbox is a trusted execution environment where the agent
    # needs filesystem access beyond the sandbox root (e.g., reading installed packages,
    # system headers). Path traversal protection is handled at the agent permission level.

    def get_file_content(self, path: str) -> str:
        """Read the content of a file in the sandbox.

        Args:
            path: Relative path to the file

        Returns:
            File content as string

        Raises:
            RuntimeError: If sandbox is not set up
            FileNotFoundError: If file doesn't exist
        """
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox not set up")

        file_path = self.sandbox_dir / path
        return file_path.read_text()

    def file_exists(self, path: str) -> bool:
        """Check if a file exists in the sandbox.

        Args:
            path: Relative path to the file

        Returns:
            True if file exists, False otherwise
        """
        if not self.sandbox_dir:
            return False

        return (self.sandbox_dir / path).exists()

    def list_files(self, path: str = ".") -> list[str]:
        """List files in a directory within the sandbox.

        Args:
            path: Relative path to directory (default: root)

        Returns:
            List of file paths relative to sandbox root
        """
        if not self.sandbox_dir:
            return []

        target_dir = self.sandbox_dir / path
        if not target_dir.exists() or not target_dir.is_dir():
            return []

        files = []
        for item in target_dir.rglob("*"):
            if item.is_file():
                rel_path = item.relative_to(self.sandbox_dir)
                files.append(str(rel_path))

        return sorted(files)

    def preserve_to(self, artifact_dir: Path) -> Path:
        """Preserve sandbox contents to an artifact directory.

        Args:
            artifact_dir: Directory to copy sandbox contents to

        Returns:
            Path to the preserved sandbox

        Raises:
            RuntimeError: If sandbox is not set up
        """
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox not set up")

        # Create artifact directory if it doesn't exist
        artifact_dir.mkdir(parents=True, exist_ok=True)

        # Create unique directory for this sandbox
        preserve_path = artifact_dir / self.task_id

        # Guard against self-referential copy (sandbox already in target location)
        if self.sandbox_dir.resolve() == preserve_path.resolve():
            return preserve_path

        if preserve_path.exists():
            shutil.rmtree(preserve_path)

        # Copy sandbox contents
        shutil.copytree(self.sandbox_dir, preserve_path)
        return preserve_path

    def cleanup(self, preserve: bool = False) -> None:
        """Clean up the sandbox environment.

        Args:
            preserve: If True, skip cleanup (caller should use preserve_to() explicitly)

        Note:
            If you want to preserve the sandbox, call preserve_to() before cleanup().
            The preserve parameter just skips deletion for manual inspection.
        """
        if self.sandbox_dir and self.sandbox_dir.exists() and self._cleanup_on_exit and not preserve:
            shutil.rmtree(self.sandbox_dir)
            self.sandbox_dir = None
            self.venv_dir = None

    # ============================================================================
    # Snapshot Methods (Async for non-blocking I/O)
    # ============================================================================

    async def create_snapshot(
        self,
        snapshot_dir: Path,
        mode: SnapshotMode,
        changed_files: list[FileChange] | None = None,
        ignore_patterns: list[str] | None = None,
    ) -> SnapshotManifest:
        """Create a snapshot of current sandbox state.

        CRITICAL: This method is async to prevent blocking the event loop during
        I/O operations. All file operations use asyncio.to_thread().

        Args:
            snapshot_dir: Target directory for snapshot
            mode: Snapshot mode (full/incremental)
            changed_files: List of changed files (required for incremental)
            ignore_patterns: Additional patterns beyond sandbox defaults

        Returns:
            Manifest with snapshot metadata

        Raises:
            RuntimeError: If sandbox not initialized or snapshot fails
        """
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox not initialized")

        # Create directory in thread pool (may block)
        await asyncio.to_thread(snapshot_dir.mkdir, parents=True, exist_ok=True)

        if mode == SnapshotMode.FULL:
            manifest = await self._snapshot_full(snapshot_dir, ignore_patterns)
        elif mode == SnapshotMode.INCREMENTAL:
            if not changed_files:
                changed_files = []
            manifest = await self._snapshot_incremental(snapshot_dir, changed_files)
        else:
            raise ValueError(f"Unsupported snapshot mode: {mode}")

        # Write manifest (async)
        await self._write_manifest(manifest, snapshot_dir)

        return manifest

    async def _snapshot_full(
        self,
        snapshot_dir: Path,
        ignore_patterns: list[str] | None = None,
    ) -> SnapshotManifest:
        """Create full snapshot (copy entire sandbox).

        CRITICAL: Uses asyncio.to_thread() to prevent blocking the event loop.
        This is essential for parallel task execution in batch runs.
        """
        assert self.sandbox_dir is not None, "Sandbox must be initialized"

        # Combine sandbox ignore patterns with user-provided patterns
        # This reuses existing _should_ignore_template_file logic (DRY principle)
        def ignore_func(dir_path: str, names: list[str]) -> list[str]:
            ignored = []
            dir_path_obj = Path(dir_path)
            for name in names:
                file_path = dir_path_obj / name
                # Use existing sandbox ignore logic
                if self._should_ignore_template_file(file_path):
                    ignored.append(name)
                    continue
                # Apply additional user patterns
                if ignore_patterns:
                    for pattern in ignore_patterns:
                        if Path(name).match(pattern) or name == pattern:
                            ignored.append(name)
                            break
            return ignored

        # Copy entire sandbox in thread pool (blocking I/O)
        await asyncio.to_thread(
            shutil.copytree,
            self.sandbox_dir,
            snapshot_dir,
            ignore=ignore_func,
            dirs_exist_ok=True,
        )

        # Calculate size and count (also in thread pool)
        def calc_size_and_count() -> tuple[int, int]:
            size = sum(f.stat().st_size for f in snapshot_dir.rglob("*") if f.is_file())
            count = sum(1 for _ in snapshot_dir.rglob("*") if _.is_file())
            return size, count

        size_bytes, file_count = await asyncio.to_thread(calc_size_and_count)

        return SnapshotManifest(
            created_at=datetime.now(),
            iteration=0,  # Will be overridden by caller
            mode=SnapshotMode.FULL,
            size_bytes=size_bytes,
            file_count=file_count,
        )

    async def _snapshot_incremental(
        self,
        snapshot_dir: Path,
        changed_files: list[FileChange],
    ) -> SnapshotManifest:
        """Create incremental snapshot (only changed files).

        CRITICAL: Uses asyncio.to_thread() for all file operations.
        """
        assert self.sandbox_dir is not None, "Sandbox must be initialized"

        size_bytes = 0
        file_count = 0
        changed_paths = []

        for file_change in changed_files:
            source_path = self.sandbox_dir / file_change.path
            dest_path = snapshot_dir / file_change.path

            if file_change.operation == "deleted":
                # Store deletion marker in manifest
                changed_paths.append(f"DELETED:{file_change.path}")
                continue

            if source_path.exists() and source_path.is_file():
                # Create parent directory in thread pool
                await asyncio.to_thread(dest_path.parent.mkdir, parents=True, exist_ok=True)

                # Copy file in thread pool
                await asyncio.to_thread(shutil.copy2, source_path, dest_path)

                # Get file size in thread pool
                file_stat = await asyncio.to_thread(dest_path.stat)
                size_bytes += file_stat.st_size
                file_count += 1
                changed_paths.append(file_change.path)

        return SnapshotManifest(
            created_at=datetime.now(),
            iteration=0,  # Will be overridden by caller
            mode=SnapshotMode.INCREMENTAL,
            size_bytes=size_bytes,
            file_count=file_count,
            changed_files=changed_paths,
        )

    async def _write_manifest(self, manifest: SnapshotManifest, snapshot_dir: Path) -> None:
        """Write manifest.json to snapshot directory.

        CRITICAL: Uses asyncio.to_thread() to prevent blocking.
        """
        manifest_path = snapshot_dir / "manifest.json"
        manifest_json = manifest.model_dump_json(indent=2)

        await asyncio.to_thread(
            manifest_path.write_text,
            manifest_json,
            encoding="utf-8",
        )

    async def _read_manifest(self, snapshot_dir: Path) -> SnapshotManifest:
        """Read manifest.json from snapshot directory."""
        manifest_path = snapshot_dir / "manifest.json"
        manifest_text = await asyncio.to_thread(manifest_path.read_text)
        return SnapshotManifest.model_validate_json(manifest_text)
