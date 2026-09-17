"""Sandbox manager for isolated execution environments."""

import contextlib
import fnmatch
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from .errors.checker_misuse import CheckerMisuseError
from .fs_permissions import RESTRICTED_MODE, set_permissions
from .invocation_log import render_recorder, sidecar_source
from .models import (
    IN_CONTAINER_ENV,
    RECORD_CLI_DIR,
    RECORD_CLI_LOG,
    SIDECAR_MODULES,
    RepoSource,
    SandboxConfig,
    StarterFilesSource,
    TemplateDirSource,
)
from .path_utils import VENV_DIRNAME
from .resources import get_ignore_patterns, should_ignore_path


# Module logger (inherits from coder_eval logger)
logger = logging.getLogger(__name__)


# Entries excluded from Sandbox.capture_to (docker WORKDIR-alignment): a SECURITY
# denylist of credential stores that must never reach uploaded artifacts, plus NOISE
# written by uv/pip/npm/shell when WORKDIR overlaps HOME. Matched by basename at
# every level via shutil.ignore_patterns.
# Rationale: .claude/notes/isolation.md § preserve_to, capture_to, and the capture denylist
_WORKSPACE_CAPTURE_IGNORE = (
    # --- Security: credential stores ---
    ".claude",  # RW lean copy of host ~/.claude (carries .credentials.json)
    ".aws",  # AWS credentials / config
    ".ssh",  # SSH keys
    ".gnupg",  # GPG keys
    ".docker",  # Docker auth (config.json)
    ".azure",  # Azure CLI credentials
    ".netrc",  # FTP/curl/git credentials
    ".gitconfig",  # May embed PATs via credential.helper
    # --- Noise: Python / JS build infra ---
    ".venv",
    ".npm-prefix",
    "node_modules",
    # --- Noise: home-dir caches & config (uv, pip, npm, etc.) ---
    ".cache",
    ".config",
    ".npm",
    ".local",
    # --- Noise: shell dotfiles pre-baked into the image ---
    ".bashrc",
    ".bash_history",
    ".bash_logout",
    ".profile",
    ".wget-hsts",
)

# Characters that make a criterion `path` eligible for glob expansion. Eligible,
# not automatic: `Sandbox.resolve_files` tries the literal path first.
_GLOB_METACHARACTERS = "*?["

# HAZARD: the message is persisted to task.json and injected into judge prompts,
# so an unbounded listing over a wide pattern is a real payload.
_MAX_LISTED_MATCHES = 10


def _is_glob(path: str) -> bool:
    """Return whether ``path`` contains a glob metacharacter."""
    return any(c in path for c in _GLOB_METACHARACTERS)


def _format_matches(matches: list[Path], root: Path) -> str:
    """Render matches as sandbox-relative paths, truncated to a bounded list."""
    listed = ", ".join(str(p.relative_to(root)) for p in matches[:_MAX_LISTED_MATCHES])
    remaining = len(matches) - _MAX_LISTED_MATCHES
    return f"{listed}, +{remaining} more" if remaining > 0 else listed


def _grant_read_traverse(root: Path) -> None:
    """Recursively apply ``chmod a+rX`` semantics under ``root``.

    Preserved artifacts produced inside a root-owned ``driver:docker`` container
    land on the host bind-mount owned by root, with mkdtemp's 0700 sandbox root.
    The host user (a different uid) can't traverse that, so the blob upload and
    any ``ls`` see an empty dir. This grants group+other read everywhere and
    group+other execute only where the owner already has it (dirs, exec files),
    matching ``a+rX``. Symlinks are skipped: their mode is ignored on Linux and
    ``chmod`` would alter the target instead.
    """

    def _add_bits(path: str) -> None:
        try:
            if os.path.islink(path):
                return
            mode = os.stat(path).st_mode
            new = mode | 0o044  # r for group + other
            if mode & 0o100:  # owner-executable -> dir or exec file: add g+x, o+x
                new |= 0o011
            if new != mode:
                os.chmod(path, new)
        except OSError as e:
            logger.debug("grant_read_traverse: could not chmod %s: %s", path, e)

    _add_bits(str(root))
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            _add_bits(os.path.join(dirpath, name))


class Sandbox:
    """Manages sandboxed execution environments for agent tasks.

    Supports multiple drivers (tempdir, docker) and provides isolated
    environments with virtual environments and resource limits.
    """

    REMEDIATE_HOME_PLUGINS_ENV = "CODER_EVAL_REMEDIATE_HOME_PLUGINS"
    """Env-var flag gating destructive ``$HOME/node_modules/@uipath`` cleanup."""

    def __init__(
        self,
        config: SandboxConfig,
        task_id: str,
        task_dir: Path | None = None,
        reference_dir: Path | None = None,
    ):
        """Initialize the sandbox.

        Args:
            config: Sandbox configuration
            task_id: Unique identifier for this task (used in paths)
            task_dir: Directory containing the task YAML file (exposed as TASK_DIR env var in run_command)
            reference_dir: Per-run staged copy of ``task.reference.directory``
                (exposed as the REFERENCE_DIR env var in run_command). A
                constructor argument for the same reason ``task_dir`` is: both
                feed host-directory env vars seven lines apart in
                ``_build_run_command_env``, and a construction site that set one
                but not the other produced a ``$REFERENCE_DIR`` that expanded to
                nothing and a criterion scored 0.0 with no diagnostic. The
                orchestrator still re-assigns the attribute after
                ``_stage_reference``, which runs later than construction.
        """
        self.config = config
        self.task_id = task_id
        self.task_dir = task_dir
        self.reference_dir: Path | None = reference_dir
        self.sandbox_dir: Path | None = None
        self.venv_dir: Path | None = None
        self._cleanup_on_exit = True
        # True once adopt() takes over an existing workspace. Read by the
        # Orchestrator to suppress every step that would MUTATE the tree it was
        # asked to grade, and to keep sandbox_path in the result.
        # Rationale: .claude/notes/isolation.md § Why pre_run and post_run each run exactly once
        self.was_adopted = False
        self.installed_tool_versions: dict[str, str] = {}
        self._command_base_path: str | None = None
        # Cached canonical `node_modules/@uipath`; pins UiPath CLI plugin discovery
        # via PLUGIN_TOOLS_DIR to bypass CWD-walk contamination.
        self._plugin_tools_dir: str | None = None

    @property
    def enforces_permission_windows(self) -> bool:
        """Whether a chmod window is a real, safe control in this sandbox.

        True only inside a ``driver: docker`` container. SAFE because the
        filesystem is private to this one task; REAL because that container drops
        ``DAC_OVERRIDE`` / ``DAC_READ_SEARCH``, without which a mode-000 directory
        is still readable by container root and the window is a silent no-op.
        COUNTERPART to ``docker_runner._build_argv``'s cap drops.

        On the host (``driver: tempdir``) it is a deliberate no-op: parallel tasks
        share the checked-out ``tasks/<name>/`` tree, so enforcing would chmod the
        user's own working copy across tasks.

        NOTE the predicate is the ``CODER_EVAL_IN_CONTAINER`` env var, NOT
        ``config.driver``: the host stages a container's task with ``driver: tempdir``,
        so keying on the driver would silently disable the window on the path that
        needs it.

        Rationale: .claude/notes/isolation.md § Capability drops and the anti-cheat window
        """
        return os.environ.get(IN_CONTAINER_ENV) == "1"

    def set_permissions(
        self,
        paths: Iterable[Path | None],
        *,
        mode: int = RESTRICTED_MODE,
    ) -> AbstractAsyncContextManager[None]:
        """Chmod ``paths`` to ``mode`` for the block, if this sandbox enforces that.

        The driver-aware wrapper around
        :func:`coder_eval.fs_permissions.set_permissions`: a no-op
        context manager when :attr:`enforces_permission_windows` is False, so
        callers can wrap unconditionally without branching on the driver.

        Windows stack -- see the underlying function for the nesting contract.

        ``strict=True`` whenever the window IS enforced: a chmod that fails on a
        path that exists (foreign owner, read-only mount, missing capability)
        means the agent can read the reference for the whole turn. Left as a
        warning, that run completes and is scored exactly like a protected one,
        so a broken anti-cheat control is indistinguishable from a working one
        in every downstream consumer. Fail the run instead.
        """
        if not self.enforces_permission_windows:
            return contextlib.nullcontext()
        return set_permissions(paths, mode=mode, strict=True)

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
        if self.config.driver == "docker":
            # Dispatched at the orchestrator-entry boundary; inside the container
            # the task is re-run with driver=tempdir, so this is unreachable on a
            # correctly routed call.
            raise RuntimeError(
                "Sandbox.setup() called with driver='docker' -- Docker tasks must be "
                + "dispatched via DockerRunner from the host. This indicates a routing bug."
            )
        raise ValueError(f"Unsupported sandbox driver: {self.config.driver}")

    def adopt(self, workspace: Path) -> Path:
        """Use ``workspace`` **as** the sandbox, materializing nothing into it.

        The grade-in-place counterpart to :meth:`setup`: it takes a workspace that
        already exists -- an ``execute`` run's artifacts, or a verifier's ``/app``
        -- and derives only the *environment* the criteria need (mock-dir ``+x``,
        venv discovery, the plugin-tools pin). In-place is more CORRECT here, not
        merely faster.

        "Materializing nothing" means it writes no FILES; it does still chmod
        ``+x`` over the task's declared mock-PATH directories, a mode change the
        criteria need to resolve the same shimmed binaries the agent did.

        The caller keeps ownership: ``_cleanup_on_exit`` stays False, so
        ``cleanup()`` never deletes an adopted directory. Criteria CAN still
        mutate the tree (a ``run_command`` that writes), which is why the copy
        path stays the default for a bare user-supplied work dir.

        Rationale: .claude/notes/isolation.md § Detached grading and `Sandbox.adopt`

        Args:
            workspace: An existing directory to grade in place.

        Returns:
            Path to the sandbox directory (``workspace``).

        Raises:
            RuntimeError: If the driver is ``docker`` (a container workspace is
                not reachable from the host), or ``workspace`` is not a directory.
        """
        if self.config.driver == "docker":
            raise RuntimeError(
                "Sandbox.adopt() is host-side only; a driver='docker' workspace lives inside "
                + "the container. Grade it from within the container, or copy it out first."
            )
        if not workspace.is_dir():
            raise RuntimeError(f"Cannot adopt {workspace}: not an existing directory")

        self.sandbox_dir = workspace.resolve()
        # Never flipped True: an adopted directory belongs to the caller.
        self._cleanup_on_exit = False
        self.was_adopted = True

        # Only NON-materializing steps below, EXCEPT the re-provisioning fallback
        # just below: _setup_template (overwrites the tree being graded),
        # _generate_cli_recorders (writes shims into it), and
        # _maybe_remediate_home_plugins_pollution (destructive on $HOME, and
        # remediation rather than derivation) are still deliberately skipped.
        self._prepare_mock_path_dirs()

        # DISCOVER rather than create when the venv survived -- so criteria get
        # the SAME VIRTUAL_ENV/PATH the agent had, not a freshly reinstalled one.
        # But a workspace ADOPTED FROM A CAPTURED WORKDIR (Sandbox.capture_to, the
        # docker-WORKDIR-alignment / Harbor `--workspace-dir` path) never has one:
        # `.venv` / `node_modules` / `.npm-prefix` are in `_WORKSPACE_CAPTURE_IGNORE`
        # as noise, so the execute phase's own install is silently gone by the time
        # grading adopts this workspace -- `run_command` criteria then see a bare
        # interpreter with none of `env_packages` installed. Re-provision from
        # scratch whenever the venv is missing AND there is something to install --
        # never for the common `env_packages: []` case, which has nothing worth a
        # venv for and must stay a no-op (a bare `sandbox.python` block, the
        # default, is not itself a request for a venv).
        # Rationale: .claude/notes/isolation.md § Why the venv gets system site packages
        if self.config.python:
            candidate = self.sandbox_dir / VENV_DIRNAME
            if candidate.is_dir():
                self.venv_dir = candidate
            elif self.config.python.env_packages:
                self._setup_virtualenv()
                self._install_packages()

        if self.config.node and self.config.node.env_packages and not (self.sandbox_dir / "node_modules").is_dir():
            self._install_node_packages()

        self._check_parent_node_modules_contamination()
        self._refresh_plugin_tools_dir()

        return self.sandbox_dir

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
            # Default: create a temporary directory. Dataset row tasks have IDs like
            # "parent/row" -- flatten path separators so they don't become subdirectories
            # under /tmp (mkdtemp does not auto-create parent dirs).
            safe_task_id = self.task_id.replace("/", "_").replace("\\", "_")
            # On Windows, root off the home dir: Git Bash mounts /tmp onto the base
            # temp dir while mkdtemp honors %TEMP%, giving the sandbox a divergent
            # /tmp twin the grader never reads. POSIX has one namespace.
            self.sandbox_dir = Path(
                tempfile.mkdtemp(prefix=f"coder_eval_{safe_task_id}_", dir=Path.home() if os.name == "nt" else None)
            )

        try:
            # Setup template content (repo, directory, or inline files)
            self._setup_template()

            # Generate recording shims for `record_cli` tools (before the +x pass
            # below, which also covers them)
            self._generate_cli_recorders()

            # Mark mock binaries executable so the agent's PATH can shadow real CLIs
            self._prepare_mock_path_dirs()

            # With system site packages -- see _setup_virtualenv for why an
            # isolated venv was actively harmful.
            if self.config.python:
                self._setup_virtualenv()

                # Install required packages
                if self.config.python.env_packages:
                    self._install_packages()

            # Install Node.js packages
            if self.config.node and self.config.node.env_packages:
                self._install_node_packages()

            # MST-9674: report (without remediating) parent-dir node_modules
            # contamination that could perturb Node module resolution.
            self._check_parent_node_modules_contamination()
            # Opt-in destructive cleanup of $HOME/node_modules/@uipath (eval-host-only).
            self._maybe_remediate_home_plugins_pollution()
            # Cache canonical @uipath dir for PLUGIN_TOOLS_DIR pin; no-op if `uip` absent.
            self._refresh_plugin_tools_dir()
        except Exception:
            # ONLY a temp dir we created ourselves: a caller-supplied target_dir
            # (DIRECT_WRITE) may be a pre-existing artifacts dir whose contract is
            # never to be cleared.
            if self._cleanup_on_exit:
                shutil.rmtree(self.sandbox_dir, ignore_errors=True)
                self.sandbox_dir = None
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
        # HAZARD: `--` before the URL. Without it a value beginning with `-` parses
        # as an OPTION (`--upload-pack=...` runs a command of the caller's
        # choosing), and that URL is task-authored.
        # Rationale: .claude/notes/isolation.md § Materializing a template into the sandbox
        cmd = ["git", "clone", "--", source.url, str(repo_dir)]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", timeout=60)

            # Checkout specific commit if specified
            if source.commit:
                subprocess.run(
                    ["git", "checkout", source.commit],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to clone repository: {e.stderr}") from e

    def _resolve_within_sandbox(self, rel: str, *, field: str) -> Path:
        """Join ``rel`` with the sandbox root, resolve, and reject if it escapes.

        Used by every code path that consumes a sandbox-relative path supplied by
        a task author (``template_dir.mount_point``, ``starter_files`` paths,
        ``mock_path_dirs`` entries). The result is allowed to equal the sandbox
        root (e.g. an empty ``mount_point``); anything that resolves outside
        raises ``RuntimeError`` with the originating ``field`` named so the task
        author can locate the bad entry in their YAML.

        Args:
            rel: The user-supplied path. May be a relative path, an absolute
                path (joining discards the sandbox prefix), or a string with
                ``..`` segments.
            field: Human-readable label of the YAML field being checked,
                surfaced in the error message.

        Returns:
            Absolute, resolved path that is guaranteed to be inside the sandbox.
        """
        assert self.sandbox_dir is not None, "Sandbox directory not initialized"
        sandbox_root = self.sandbox_dir.resolve()
        candidate = (self.sandbox_dir / rel).resolve()
        if candidate != sandbox_root and sandbox_root not in candidate.parents:
            raise RuntimeError(f"{field} escapes sandbox: {rel!r} -> {candidate}")
        return candidate

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

        mount_root = self._resolve_within_sandbox(source.mount_point, field="Template mount_point")
        mount_root.mkdir(parents=True, exist_ok=True)

        # Track overwrites for logging
        overwrites: set[str] = set()

        # Copy contents with ignore patterns
        for item in template_path.rglob("*"):
            # Calculate relative path
            rel_path = item.relative_to(template_path)
            # Template-RELATIVE, not absolute: an ancestor named `dist`, `build`,
            # `env`, `venv` or `node_modules` would filter out the whole template.
            if self._should_ignore_template_file(rel_path) and not self._matches_template_include_pattern(
                rel_path, source.include_patterns
            ):
                continue

            dest_path = mount_root / rel_path

            # is_symlink() first -- is_dir()/is_file() follow symlinks, so a link
            # to a dir would produce an empty dir at the destination.
            # Rationale: .claude/notes/isolation.md § Materializing a template into the sandbox
            if item.is_symlink():
                # `is_symlink()` before `exists()`, which follows the link: a
                # BROKEN symlink at dest is still an overwrite to clear.
                if dest_path.is_symlink() or dest_path.exists():
                    # Only a real directory needs rmtree; unlink() removes the
                    # link rather than its target.
                    if dest_path.is_dir() and not dest_path.is_symlink():
                        shutil.rmtree(dest_path)
                    else:
                        dest_path.unlink()
                    overwrites.add(str(rel_path))
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                # Windows `os.symlink` needs `target_is_directory=True` for
                # directory targets; POSIX ignores it. Targets are preserved
                # verbatim, absolute ones included -- template authors are trusted
                # infra, so this is intended, not a defense boundary.
                os.symlink(
                    os.readlink(item),
                    dest_path,
                    target_is_directory=item.is_dir(),
                )
            elif item.is_dir():
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

    def _prepare_mock_path_dirs(self) -> None:
        """Apply +x to plain files in each ``mock_path_dirs`` entry.

        Resolves each configured directory against the sandbox root and, for every
        plain file directly under it, ORs in the user/group/other execute bits.
        Required on NTFS and after copies that drop the +x bit; a no-op when the
        bit is already set. Missing entries and non-files (e.g. fixture
        subdirectories) are skipped silently. PATH wiring happens in the agent --
        this method only owns the filesystem side.
        """
        assert self.sandbox_dir is not None, "Sandbox directory not initialized"

        for dir_path in self.resolved_mock_path_dirs:
            for entry in dir_path.iterdir():
                if entry.is_file():
                    entry.chmod(entry.stat().st_mode | 0o111)

    @property
    def resolved_mock_path_dirs(self) -> list[Path]:
        """Absolute paths of mock dirs that exist on disk, in PATH-prepend order.

        The generated ``record_cli`` directory comes first when configured, then the
        entries in ``SandboxConfig.mock_path_dirs`` in order;
        non-existent and non-directory entries are filtered out so the caller
        can pass the result straight to PATH-prepend logic.

        Entries that resolve outside the sandbox root are rejected with a
        RuntimeError -- a typo like ``"../mocks"`` would otherwise let
        ``_prepare_mock_path_dirs`` chmod +x files on the host filesystem.
        Mirrors the ``mount_point`` containment check in
        :meth:`_apply_template_dir_source`.
        """
        if self.sandbox_dir is None:
            return []
        resolved: list[Path] = []
        # Generated recorders go FIRST, and refuse to generate a shim whose name a
        # user mock dir already provides, so this can never shadow a task's own mock.
        # Rationale: .claude/notes/isolation.md § The criterion environment, layer by layer
        if self.config.record_cli:
            generated = self._resolve_within_sandbox(RECORD_CLI_DIR, field="record_cli directory")
            if generated.is_dir():
                resolved.append(generated)
        for rel in self.config.mock_path_dirs or []:
            candidate = self._resolve_within_sandbox(rel, field="mock_path_dirs entry")
            if candidate.is_dir():
                resolved.append(candidate)
        return resolved

    def _generate_cli_recorders(self) -> None:
        """Write a recording shim for every ``SandboxConfig.record_cli`` entry.

        Each shim runs inside the sandbox, where ``coder_eval`` is not
        installed, so it carries its configuration as literals and imports
        nothing installed. An entry that declares ``responses`` also gets every
        :data:`SIDECAR_MODULES` file written into the recorder directory beside
        it — the argv matcher it dispatches on, which it imports as a sibling
        rather than the harness splicing that source into the shim. A rules-less
        entry needs no matcher, so no sidecar is written for it.
        A ``.cmd`` twin is written beside each shim so a bare ``uip`` also
        resolves through Windows PATHEXT lookup on the tempdir driver.

        Raises:
            RuntimeError: a task's own ``mock_path_dirs`` already provides an
                executable with the same name. Generating ours anyway would make
                which one runs depend on directory order — a silent, confusing
                override — so the collision is surfaced instead.
        """
        assert self.sandbox_dir is not None, "Sandbox directory not initialized"
        if not self.config.record_cli:
            return

        for rel in self.config.mock_path_dirs or []:
            user_dir = self._resolve_within_sandbox(rel, field="mock_path_dirs entry")
            if not user_dir.is_dir():
                continue
            for spec in self.config.record_cli:
                # Every generated name, not just the bare one: Windows PATHEXT
                # resolves `uip` to `uip.cmd` ahead of the task's own mock.
                clash = next(
                    (
                        user_dir / name
                        for name in (spec.tool, f"{spec.tool}.cmd", f"{spec.tool}.bat", f"{spec.tool}.exe")
                        if (user_dir / name).exists()
                    ),
                    None,
                )
                if clash is not None:
                    msg = (
                        f"record_cli would generate a '{spec.tool}' shim, but mock_path_dirs entry "
                        f"'{rel}' already provides one ({rel}/{clash.name}). "
                        "Remove the record_cli entry to keep your own mock, or drop the file to use "
                        "the generated recorder."
                    )
                    raise RuntimeError(msg)

        recorder_dir = self._resolve_within_sandbox(RECORD_CLI_DIR, field="record_cli directory")
        # Wipe rather than reuse: DIRECT_WRITE does not clear the target dir, so a
        # reused --run-dir would leave a previous run's log to be scored as this one's.
        if recorder_dir.exists():
            shutil.rmtree(recorder_dir, ignore_errors=True)
        recorder_dir.mkdir(parents=True, exist_ok=True)

        # Seeded so it always exists: `cli_called` reads a MISSING log as a harness
        # fault, which is wrong for a run that legitimately called nothing.
        log_path = self.sandbox_dir / RECORD_CLI_LOG
        log_path.write_text("", encoding="utf-8")

        interpreter = os.path.realpath(sys.executable)
        for spec in self.config.record_cli:
            shim = recorder_dir / spec.tool
            if shim.exists():
                msg = (
                    f"record_cli would overwrite '{RECORD_CLI_DIR}/{spec.tool}', already written this "
                    "setup. Two entries generating the same filename?"
                )
                raise RuntimeError(msg)
            shim.write_text(render_recorder(spec, interpreter), encoding="utf-8", newline="\n")
            # Not left to _prepare_mock_path_dirs: the shim must be executable
            # even if the recorder dir is consumed some other way.
            shim.chmod(shim.stat().st_mode | 0o111)
            # `python "%~dp0<tool>" %*` — the extensionless script beside this file.
            cmd_lines = [
                "@echo off",
                "REM Generated by coder_eval SandboxConfig.record_cli.",
                "REM Windows PATHEXT lookup resolves this; POSIX uses the extensionless twin.",
                f'"{interpreter}" "%~dp0{spec.tool}" %*',
            ]
            (recorder_dir / f"{spec.tool}.cmd").write_text(
                "\r\n".join(cmd_lines) + "\r\n",
                encoding="utf-8",
                newline="",
            )

        # Once for the directory, not per entry: every rules-bearing shim imports
        # the same sidecar. Skipped when no entry declares rules.
        sidecars = sorted(SIDECAR_MODULES) if any(spec.responses for spec in self.config.record_cli) else []
        for module in sidecars:
            (recorder_dir / module).write_text(sidecar_source(module), encoding="utf-8", newline="\n")

        summary = ", ".join(
            f"{s.tool}(exit {s.exit_code}" + (f", {len(s.responses)} rule(s)" if s.responses else "") + ")"
            for s in self.config.record_cli
        )
        # Names the sidecar: an operator debugging a `sidecar_error` needs setup-time
        # confirmation that the file was actually written.
        beside = f" (+ {', '.join(sidecars)})" if sidecars else ""
        logger.info(f"Generated {len(self.config.record_cli)} CLI recorder(s) in {RECORD_CLI_DIR}/{beside}: {summary}")

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
            # HAZARD: reject path traversal before any filesystem write. The helper
            # allows the resolved path to EQUAL sandbox_root, harmless for a file
            # because the mkdir/write_text below fails on an empty name anyway.
            file_path = self._resolve_within_sandbox(starter_file.path, field="starter_files path")

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

    def _matches_template_include_pattern(self, rel_path: Path, include_patterns: list[str]) -> bool:
        """Return whether a template-relative path is explicitly re-included.

        Patterns are matched with :func:`fnmatch.fnmatchcase` against the
        forward-slash form of ``rel_path``. Note that ``*`` does NOT stop at
        ``/`` (unlike gitignore), so ``tools/*/dist`` will also match
        ``tools/a/b/dist``. This is permissive by design: include patterns can
        only re-include paths the default ignore list rejected, so widening is
        safer than narrowing. A leading ``./`` on the pattern is stripped.
        """
        if not include_patterns:
            return False
        normalized_path = rel_path.as_posix()
        for pattern in include_patterns:
            normalized_pattern = pattern.replace("\\", "/").removeprefix("./")
            if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
                return True
        return False

    def _setup_virtualenv(self) -> None:
        """Create a Python virtual environment in the sandbox, with system site packages.

        ``--system-site-packages`` is load-bearing, not a convenience: an ISOLATED
        venv on the criterion PATH shadows the interpreter while providing nothing,
        so inside a task image that provisions packages globally ``python`` could
        not import what ``pip`` reported present. System site packages keeps both
        halves -- the image's globals stay importable and installs still land in
        the venv, so a task's ``env_packages`` cannot leak into the image.

        Note the venv is NOT on the agent's own PATH, so the contradiction is a
        property of criterion and pre/post-run subprocesses.

        Rationale: .claude/notes/isolation.md § Why the venv gets system site packages
        """
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox directory not initialized")

        self.venv_dir = self.sandbox_dir / VENV_DIRNAME

        # Use uv to create virtual environment (faster than venv)
        try:
            # Check if uv is available
            subprocess.run(["uv", "--version"], check=True, capture_output=True, timeout=5)
            # Use uv to create venv
            cmd = ["uv", "venv", "--system-site-packages", str(self.venv_dir)]
            subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", timeout=60)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # The two paths do not produce the same artifact -- this one seeds pip,
            # `uv venv` does not -- so say which shape this host got.
            import venv

            logger.warning("uv unavailable; created %s with stdlib venv (pip seeded)", self.venv_dir)
            venv.create(self.venv_dir, with_pip=True, system_site_packages=True)

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
            subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", timeout=300, env=env)
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
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
                cwd=self.sandbox_dir,
            )
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
                    data = json.loads(pkg_json.read_text(encoding="utf-8"))
                    version = data.get("version", "unknown")
                    self.installed_tool_versions[name] = version
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug(
                        "Failed to read or parse package.json for %s at %s: %s",
                        name,
                        pkg_json,
                        exc,
                    )

    def set_command_base_path(self, path: str | None) -> None:
        """Set the parent PATH used by sandbox command checks.

        The orchestrator uses this to align success-criteria commands with the
        PATH passed to the agent SDK. Sandbox-local venv and node bin entries
        are still prepended by ``run_command``.

        Also re-derives the canonical ``PLUGIN_TOOLS_DIR`` (MST-9795): the
        resolved ``uip`` binary depends on PATH, and the path-aligned criterion
        is the canonical lookup. Failures are swallowed — the env var simply
        stays unset and the CLI falls back to its walk-based discovery.

        Passing ``None`` clears the agent-aligned PATH prefix and re-derives
        ``PLUGIN_TOOLS_DIR`` from ``os.environ['PATH']`` alone. The new pin
        may differ from the previous one if the parent PATH resolves ``uip``
        to a different install — by design, since dropping the agent
        alignment means the criterion subprocess should now match the parent
        environment.
        """
        self._command_base_path = path or None
        self._refresh_plugin_tools_dir()

    @property
    def command_base_path(self) -> str | None:
        """Read-only view of the configured base PATH (or ``None`` when unset).

        Tests can observe orchestrator-set overrides without touching the
        underlying private slot. Mutate via :meth:`set_command_base_path`.
        """
        return self._command_base_path

    @property
    def plugin_tools_dir(self) -> str | None:
        """Canonical ``node_modules/@uipath`` derived from the resolved ``uip``.

        Populated by :meth:`_refresh_plugin_tools_dir` after the agent's PATH
        is captured. When non-None, ``_build_run_command_env`` exports it as
        ``PLUGIN_TOOLS_DIR`` so the UiPath CLI pins plugin discovery instead
        of walking up from CWD — eliminating MST-9795's host-pollution
        asymmetry between authoring-time and criterion-time validation.

        Returns ``None`` when ``uip`` is not on PATH or the resolved binary
        does not live inside a recognizable ``node_modules/@uipath`` tree
        (e.g. development monorepo runs).
        """
        return self._plugin_tools_dir

    @property
    def uip_search_path(self) -> str:
        """The PATH used to resolve ``uip`` — agent-aligned prefix + process PATH.

        The same PATH ``run_command`` subprocesses and the agent SDK env see,
        so a binary resolved against it is the one task commands actually
        executed.
        """
        search_path = os.environ.get("PATH", "")
        if self._command_base_path:
            search_path = f"{self._command_base_path}{os.pathsep}{search_path}"
        return search_path

    def refresh_plugin_tools_dir(self) -> None:
        """Re-derive :attr:`plugin_tools_dir` for the current PATH.

        Public hook for callers that need post-task state: the UiPath CLI
        auto-installs/upgrades its tool plugins on first use, so a pin derived
        at setup time can be stale (or ``None``) by the time the task ends.
        """
        self._refresh_plugin_tools_dir()

    def _refresh_plugin_tools_dir(self) -> None:
        """Resolve the canonical ``node_modules/@uipath`` for the current PATH.

        Delegates to :func:`coder_eval.utils.resolve_uipath_plugin_dir` against
        ``uip_search_path`` (``command_base_path + os.environ['PATH']`` — the
        same PATH ``run_command`` and the agent SDK will see), then stores the
        result as a string on ``self._plugin_tools_dir`` (or ``None`` if no
        usable ``uip`` is on PATH). Idempotent across calls; safe to call from
        both ``setup`` (initial value when no command_base_path yet) and
        ``set_command_base_path`` (re-derive after PATH alignment).
        """
        from .utils import resolve_uipath_plugin_dir

        resolved = resolve_uipath_plugin_dir(self._plugin_discovery_path())
        self._plugin_tools_dir = str(resolved) if resolved is not None else None

    def _plugin_discovery_path(self) -> str:
        """``uip_search_path`` minus the generated recorder dir.

        A recording shim is not the real CLI, so letting it win the `uip` lookup
        made resolve_uipath_plugin_dir return None (a shim is not inside a
        node_modules/@uipath tree) and silently drop the PLUGIN_TOOLS_DIR pin for
        every run_command criterion -- for `record_cli: [{tool: uip}]`, the
        documented example. A hand-written mock under mock_path_dirs shadows the
        lookup the same way, but that predates this feature and changing it would
        alter existing tasks.
        """
        search_path = self.uip_search_path
        if not self.config.record_cli or self.sandbox_dir is None:
            return search_path
        recorder_dir = str((self.sandbox_dir / RECORD_CLI_DIR).resolve())
        kept = [entry for entry in search_path.split(os.pathsep) if entry and os.path.realpath(entry) != recorder_dir]
        return os.pathsep.join(kept)

    def _maybe_remediate_home_plugins_pollution(self) -> Path | None:
        """Optionally delete ``$HOME/node_modules/@uipath`` before the task runs.

        Gated on the ``CODER_EVAL_REMEDIATE_HOME_PLUGINS`` env var being a
        truthy string (``"1"``/``"true"``/``"yes"``, case-insensitive). Off
        by default — silent deletion of a user-owned directory is
        destructive and a generic eval framework should not own that
        decision. Operators of dedicated eval runners (Azure) flip the flag
        on at host-bring-up time because every task there is poisoned by
        sibling tasks leaking installs into ``$HOME`` (MST-9674 / MST-9795).

        Returns the deleted directory on success, ``None`` when no action
        was taken (flag off, dir absent, or under-test ``$HOME`` mismatch).

        TOCTOU: there is a small window between ``Path.resolve(strict=True)``
        and ``shutil.rmtree`` during which the target could be replaced.
        Combined defenses: (a) the resolved-anchor check rejects HOME=/,
        (b) ``resolved_home in resolved_target.parents`` confines deletion
        under HOME, (c) the operator opt-in gates the entire path. Failures
        in ``rmtree`` are silenced (``ignore_errors=True``) and surfaced via
        a residual-presence warning rather than a raise.
        """
        flag = os.environ.get(self.REMEDIATE_HOME_PLUGINS_ENV, "").strip().lower()
        if flag not in {"1", "true", "yes"}:
            return None
        home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if not home:
            return None
        target = Path(home) / "node_modules" / "@uipath"
        if not target.is_dir():
            return None
        # HAZARD: refuse to touch anything outside the configured HOME. The path
        # construction above already anchors there; this is belt-and-suspenders.
        try:
            resolved_target = target.resolve(strict=True)
            resolved_home = Path(home).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            logger.warning(
                "Cannot resolve %s for remediation: %s",
                target,
                exc,
            )
            return None
        if resolved_home == Path(resolved_home.anchor):
            logger.warning(
                "Refusing to remediate %s: HOME=%s resolves to the filesystem root",
                target,
                resolved_home,
            )
            return None
        if resolved_home not in resolved_target.parents:
            logger.warning(
                "Refusing to remediate %s: resolved target %s is not under HOME %s",
                target,
                resolved_target,
                resolved_home,
            )
            return None
        logger.warning(
            "MST-9795 remediation: removing host-pollution dir %s (gated on %s; sibling tasks leaked installs)",
            resolved_target,
            self.REMEDIATE_HOME_PLUGINS_ENV,
        )
        shutil.rmtree(resolved_target, ignore_errors=True)
        if resolved_target.exists():
            logger.warning(
                "MST-9795 remediation: %s still present after rmtree (partial delete; check fs busy/locked files)",
                resolved_target,
            )
        return resolved_target

    def _build_run_command_env(self) -> dict[str, str]:
        """Build the environment for ``run_command``.

        Each layer is independent -- none breaks if another is absent: the parent
        env, the agent's captured SDK PATH (PREPENDED, so system binaries stay
        reachable), the sandbox venv, ``<sandbox>/node_modules/.bin``,
        ``NODE_PATH=""``, a sandbox-scoped ``NPM_CONFIG_PREFIX``, ``TASK_DIR``,
        ``REFERENCE_DIR``, and ``PLUGIN_TOOLS_DIR`` (which defers to an inherited
        value).

        Rationale: .claude/notes/isolation.md § The criterion environment, layer by layer
        """
        assert self.sandbox_dir is not None
        env = os.environ.copy()
        if self._command_base_path:
            env["PATH"] = f"{self._command_base_path}{os.pathsep}{env['PATH']}"
        if self.venv_dir:
            env["VIRTUAL_ENV"] = str(self.venv_dir)
            env["PATH"] = f"{self._venv_scripts_dir}{os.pathsep}{env['PATH']}"
        node_bin = self.sandbox_dir / "node_modules" / ".bin"
        if node_bin.exists():
            env["PATH"] = f"{node_bin}{os.pathsep}{env['PATH']}"
        # MST-9674: keep Node and npm resolution sandbox-local so concurrent
        # tasks cannot poison each other through shared parent-dir node_modules.
        env["NODE_PATH"] = ""
        env["NPM_CONFIG_PREFIX"] = str(self.sandbox_dir / ".npm-prefix")
        # Pin UiPath CLI plugin discovery so the criterion subprocess uses the
        # same @uipath tools the agent authored against (defers to an external pin).
        if self._plugin_tools_dir and "PLUGIN_TOOLS_DIR" not in env:
            env["PLUGIN_TOOLS_DIR"] = self._plugin_tools_dir
        if self.task_dir:
            env["TASK_DIR"] = str(self.task_dir)
        # Safe to expose: `run_command` criteria execute AFTER the agent's turn,
        # outside the mode-000 anti-cheat window. Absent for tasks with no
        # `reference:` block.
        if self.reference_dir:
            env["REFERENCE_DIR"] = str(self.reference_dir)
        return env

    def _check_parent_node_modules_contamination(self) -> list[Path]:
        """Walk up from ``sandbox_dir`` and report any ancestor that has a
        populated ``node_modules/`` directory.

        Detection only: it logs one warning per directory and returns the list.
        Auto-remediation is intentionally avoided -- those dirs may legitimately
        belong to the user. The check stays scope-agnostic because the failure mode
        is generic to Node module resolution.

        Rationale: .claude/notes/isolation.md § The criterion environment, layer by layer
        """
        if self.sandbox_dir is None:
            return []
        offenders: list[Path] = []
        seen: set[Path] = set()
        # Walk strictly upward; do not include the sandbox itself (its
        # node_modules is intentional).
        for parent in self.sandbox_dir.resolve().parents:
            if parent in seen:
                continue
            seen.add(parent)
            node_modules_dir = parent / "node_modules"
            if not node_modules_dir.is_dir():
                continue
            try:
                # Dot-entries are package-manager bookkeeping, not installed
                # packages that would shadow a sandbox-local install.
                entries = sorted(p.name for p in node_modules_dir.iterdir() if not p.name.startswith("."))
            except OSError:
                # Permission denied / race-with-delete — skip silently.
                continue
            if not entries:
                continue
            offenders.append(node_modules_dir)
            sample = ", ".join(entries[:5])
            more = f" (+{len(entries) - 5} more)" if len(entries) > 5 else ""
            logger.warning(
                "Parent-dir node_modules contamination detected at %s — "
                + "Node's parent-walking resolver may pick this up before the "
                + "sandbox-local install (see MST-9674). Contents: %s%s",
                node_modules_dir,
                sample,
                more,
            )
        return offenders

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

        env = self._build_run_command_env()

        try:
            # Shell execution is intentional here: pipes, redirects and compound
            # commands. Decoded with `errors="replace"` so an agent emitting
            # non-UTF-8 output does not kill the run with UnicodeDecodeError.
            result = subprocess.run(
                command,
                shell=True,  # nosec B602 - Required for sandbox command execution
                cwd=self.sandbox_dir,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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

    # HAZARD: only ``list_files`` skips containment -- it joins and rglobs directly.
    # ``get_file_content`` and ``file_exists`` go through ``resolve_files``, hence
    # ``_within_sandbox`` and ``_reject_escaped``. Do not read this as "containment
    # lives elsewhere": for a criterion path it lives HERE.
    # Rationale: .claude/notes/isolation.md § Criterion paths are contained, quietly

    def _within_sandbox(self, candidate: Path) -> bool:
        """Whether a resolved criterion path stays inside the sandbox.

        The read-side twin of :meth:`_resolve_within_sandbox`, which every OTHER
        task-authored path already goes through.

        Returns False rather than raising: an out-of-sandbox path is
        indistinguishable to the criterion from a file that is not there, and
        raising would book a config error as an agent crash (CE039). Silent by
        design -- :meth:`resolve_files` reports the escape ONCE per criterion,
        naming the pattern the task author actually wrote.

        Rationale: .claude/notes/isolation.md § Criterion paths are contained, quietly
        """
        assert self.sandbox_dir is not None
        root = self.sandbox_dir.resolve()
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        return resolved == root or root in resolved.parents

    def _warn_escaped(self, path: str) -> None:
        """Report that some GLOB matches left the sandbox and were dropped.

        A warning, not an error, only because a glob is a search: filtering out
        the matches that escaped (a symlink pointing out of the tree, say) while
        keeping the ones inside is the useful behaviour. The literal branch is
        the opposite case and raises — see ``_reject_escaped``.
        """
        logger.warning(
            "Criterion path %r matched files outside the sandbox (%s); those matches were dropped.",
            path,
            self.sandbox_dir,
        )

    def _reject_escaped(self, path: str, candidate: Path) -> None:
        """Refuse a criterion path that names an existing file OUTSIDE the sandbox.

        No agent behaviour can ever satisfy such a path, so it is not a verdict
        about the agent -- precisely the distinction CE039 enforces, with
        ``CheckerMisuseError`` as its prescribed signal.

        Note the guard fires only when the escaping path EXISTS. A merely-absent
        absolute path still resolves to "no match", an ordinary failing verdict.

        Rationale: .claude/notes/isolation.md § Criterion paths are contained, quietly
        """
        raise CheckerMisuseError(
            f"Criterion path {path!r} resolves to {candidate}, outside the sandbox ({self.sandbox_dir}). "
            + "Criterion paths are sandbox-relative; an absolute path is joined onto the sandbox root, "
            + "which discards the prefix. To assert on a file baked into the container image rather than "
            + "produced by the agent, use a `run_command` criterion (e.g. `test -f /opt/marker`)."
        )

    def resolve_files(self, path: str) -> list[Path]:
        """Resolve a criterion ``path`` to the sandbox files it addresses.

        A path that names an existing file or directory resolves to itself, **even
        when it contains a glob metacharacter**. Only when the literal does not
        exist is it expanded against the sandbox root, filtered through the
        sandbox's ignore patterns -- and only for segments the glob *discovered*,
        so ``dist/**/*.js`` is an explicit opt-in that survives. Matches are sorted
        for determinism and directories dropped.

        Rationale: .claude/notes/isolation.md § Criterion paths are contained, quietly

        Args:
            path: Relative path or glob pattern

        Returns:
            Sorted matching files; empty when nothing matches
        """
        if not self.sandbox_dir:
            return []

        # Literal first: an existing path is never reinterpreted as a pattern.
        candidate = self.sandbox_dir / path
        if candidate.exists():
            if self._within_sandbox(candidate):
                return [candidate]
            self._reject_escaped(path, candidate)

        if not _is_glob(path):
            return []

        patterns = get_ignore_patterns(self.config.ignore_patterns)
        pinned = {segment for segment in path.split("/") if segment and not _is_glob(segment)}

        matches: list[Path] = []
        escaped = False
        for match in self.sandbox_dir.glob(path):
            if not match.is_file():
                continue
            if not self._within_sandbox(match):
                escaped = True
                continue
            discovered = [part for part in match.relative_to(self.sandbox_dir).parts if part not in pinned]
            if discovered and should_ignore_path(Path(*discovered), patterns):
                continue
            matches.append(match)

        if escaped:
            self._warn_escaped(path)
        return sorted(matches)

    def resolved_path_label(self, path: str) -> str | None:
        """Sandbox-relative path a glob resolved to, for grading transparency.

        With exactly-one-match semantics on content reads, *which* file was
        graded is most of the signal. Returns ``None`` for a literal path
        (nothing was inferred) and for a pattern that did not resolve to
        exactly one file.

        Args:
            path: Relative path or glob pattern

        Returns:
            Sandbox-relative path of the single match, or ``None``
        """
        if not self.sandbox_dir or not _is_glob(path):
            return None

        matches = self.resolve_files(path)
        if len(matches) != 1:
            return None

        return str(matches[0].relative_to(self.sandbox_dir))

    def get_file_content(self, path: str) -> str:
        """Read the content of a file in the sandbox.

        Args:
            path: Relative path to the file, or a glob pattern matching exactly
                one file

        Returns:
            File content as string

        Raises:
            RuntimeError: If sandbox is not set up
            FileNotFoundError: If nothing matches ``path``
            ValueError: If a glob matches more than one file
        """
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox not set up")

        matches = self.resolve_files(path)
        if not matches:
            raise FileNotFoundError(f"No file matches '{path}' in the sandbox")
        if len(matches) > 1:
            raise ValueError(
                f"Pattern '{path}' matches {len(matches)} files — refusing to guess which to grade: "
                + _format_matches(matches, self.sandbox_dir)
            )

        return matches[0].read_text(encoding="utf-8")

    def file_exists(self, path: str) -> bool:
        """Check if a file exists in the sandbox.

        Args:
            path: Relative path to the file, or a glob pattern

        Returns:
            True if at least one file matches, False otherwise
        """
        return bool(self.resolve_files(path))

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

    def grant_read_access(self) -> None:
        """Apply ``chmod a+rX`` across the sandbox tree (in place).

        For DIRECT_WRITE preservation the sandbox already lives in the
        artifacts dir, so ``preserve_to`` (which would otherwise grant this)
        never runs. A root-owned docker container leaves the tree at 0700, so
        the host user can't traverse it; granting group+other read/traverse
        keeps the artifacts visible across the uid boundary. No-op-ish on the
        host path, where the sandbox is already owner-readable.
        """
        if self.sandbox_dir is not None and self.sandbox_dir.exists():
            _grant_read_traverse(self.sandbox_dir)

    def preserve_to(self, artifact_dir: Path) -> Path:
        """Preserve sandbox contents to an artifact directory.

        Uses ``shutil.move``: an atomic rename when source and destination
        share a filesystem, copy+remove otherwise. Either way the source side
        is gone the moment this method returns (no second-pass rmtree).

        Args:
            artifact_dir: Directory to move sandbox contents into.

        Returns:
            Path to the preserved sandbox.

        Raises:
            RuntimeError: If sandbox is not set up.
        """
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox not set up")

        # task_id may contain "/" (dataset row tasks); ensure the parent exists.
        preserve_path = artifact_dir / self.task_id
        preserve_path.parent.mkdir(parents=True, exist_ok=True)

        # Guard against self-referential move (sandbox already at target).
        if self.sandbox_dir.resolve() == preserve_path.resolve():
            return preserve_path

        if preserve_path.exists():
            shutil.rmtree(preserve_path)

        old_sandbox_dir = self.sandbox_dir
        shutil.move(str(old_sandbox_dir), str(preserve_path))

        # mkdtemp creates the sandbox root at 0700, and under driver:docker the
        # tree lands owned by container root -- the host user (a different uid)
        # then cannot traverse it and silently sees no artifacts.
        # Rationale: .claude/notes/isolation.md § preserve_to, capture_to, and the capture denylist
        _grant_read_traverse(preserve_path)

        # Repoint so a subsequent cleanup() is a no-op. Absolute paths inside the
        # venv are not rewritten (same as the prior copy-based code).
        self.sandbox_dir = preserve_path
        if self.venv_dir is not None:
            try:
                rel = self.venv_dir.relative_to(old_sandbox_dir)
                self.venv_dir = preserve_path / rel
            except ValueError:
                # Defensive: venv_dir is always created under sandbox_dir today.
                # If that changes, leave the pointer untouched.
                pass
        self._cleanup_on_exit = False
        return preserve_path

    def capture_to(self, artifact_dir: Path) -> Path:
        """Copy an in-place workspace out to ``artifact_dir/<task_id>`` (docker WORKDIR mode).

        Sibling to :meth:`preserve_to`, but COPIES instead of ``shutil.move``: the
        sandbox here is the container's own WORKDIR, discarded with ``--rm``, and
        the orchestrator's own cwd may sit under it. Excludes the credential and
        noise entries in :data:`_WORKSPACE_CAPTURE_IGNORE`, because the WORKDIR can
        BE ``$HOME``.

        HAZARD: ``symlinks=True`` + ``ignore_dangling_symlinks=True`` are both
        required -- without the second, one dangling link raises ``shutil.Error``
        and fails artifact capture for the whole task.

        Returns the destination path; unlike preserve_to it does NOT repoint
        ``self.sandbox_dir``.

        Rationale: .claude/notes/isolation.md § preserve_to, capture_to, and the capture denylist
        """
        if not self.sandbox_dir:
            raise RuntimeError("Sandbox not set up")

        # task_id may contain "/" (dataset row tasks); ensure the parent exists.
        preserve_path = artifact_dir / self.task_id
        preserve_path.parent.mkdir(parents=True, exist_ok=True)

        # Guard against a self-referential copy (workspace already at target).
        if self.sandbox_dir.resolve() == preserve_path.resolve():
            _grant_read_traverse(preserve_path)
            return preserve_path

        if preserve_path.exists():
            shutil.rmtree(preserve_path)
        shutil.copytree(
            self.sandbox_dir,
            preserve_path,
            symlinks=True,
            ignore_dangling_symlinks=True,
            ignore=shutil.ignore_patterns(*_WORKSPACE_CAPTURE_IGNORE),
        )
        _grant_read_traverse(preserve_path)
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
