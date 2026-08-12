"""Run a single task inside a fresh Docker container.

Host-side counterpart of the in-container ``coder-eval _run-task-internal``
subcommand. Responsible for: rendering the docker-run argv, bind-mounting task
inputs and an output dir, streaming container stdout to the host log, and
reading back ``task.json`` (the only artifact that crosses the boundary).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import yaml

from coder_eval.logging_config import DEFAULT_LOG_TAIL_MAX_BYTES
from coder_eval.models import (
    AGENT_HOME,
    CONTAINER_AGENT_WORK_DIR,
    CONTAINER_INPUT_DIR,
    CONTAINER_OUTPUT_DIR,
    CONTAINER_TASK_DIR,
    CONTAINER_WORK_DIR,
    RESERVED_CONTAINER_DIRS,
    AgentKind,
    DockerDriverConfig,
    EvaluationResult,
    FinalStatus,
    PreservationMode,
    ResourceLimits,
)
from coder_eval.streaming.callbacks import safe_emit
from coder_eval.streaming.wire import deserialize_event, has_prefix
from coder_eval.utils import get_default_docker_image_tag


if TYPE_CHECKING:
    from coder_eval.models import ResolvedTask
    from coder_eval.streaming.callbacks import StreamCallback


logger = logging.getLogger(__name__)


# Container-side paths (CONTAINER_WORK_DIR/_INPUT_DIR/_OUTPUT_DIR/_TASK_DIR,
# RESERVED_CONTAINER_DIRS) are imported above from models.container_paths and
# kept in lockstep with docker/coder_eval_entrypoint.sh.

# In-image path of the framework entrypoint, pinned by the host via
# `docker run --entrypoint` (the image bakes no ENTRYPOINT). MUST equal the
# `COPY` destination in docker/Dockerfile -- a drift guard test enforces that.
CONTAINER_ENTRYPOINT = "/usr/local/bin/coder_eval_entrypoint.sh"

# Docker Desktop's stable alias for the host, from inside a bridge-network
# container. Auto-resolves on macOS/Windows; on Linux it must be published
# explicitly via `--add-host host.docker.internal:host-gateway`.
_DOCKER_HOST_ALIAS = "host.docker.internal"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_AGENT_ISOLATION_MAX_PIDS = 512


def _rewrite_loopback_for_container(url: str) -> str | None:
    """Rewrite a loopback URL to the docker host alias, preserving scheme/port/path.

    Returns the rewritten URL, or None if the host is not loopback (forward as-is).
    A LiteLLM proxy on the HOST is unreachable at localhost from inside a bridge
    container, so ``http://localhost:4000`` -> ``http://host.docker.internal:4000``.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.hostname not in _LOOPBACK_HOSTS:
        return None
    netloc = _DOCKER_HOST_ALIAS if parts.port is None else f"{_DOCKER_HOST_ALIAS}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# Top-level entries under ~/.claude that the per-task RW copy SKIPS. We copy
# the host's ~/.claude into a throwaway tmp dir and mount that copy read-WRITE
# so the in-container CLI can write anywhere it needs without ever touching the
# host's real ~/.claude. The container needs only auth + settings + plugins;
# everything else under ~/.claude is heavy, transient, or host-local state it
# never reads, so we drop it to keep the per-task copy cheap. On a real host
# this is the difference between a ~300 MB copy and a few MB: `security/` (the
# security plugin's data) alone is often hundreds of MB, and `projects/`
# (transcripts), `cache/`, `file-history/`, `backups/`, `sessions/`,
# `telemetry/`, `downloads/`, and `shell-snapshots/` all accumulate without
# bound. `session-env/` (per-Bash ephemera) is recreated fresh in the copy by
# the container. The last group is volatile per-session churn the *running* CLI
# rewrites continuously (this harness itself runs inside Claude Code, so the live
# host ~/.claude is mutating while we copy): dropping it both keeps the copy lean
# AND shrinks the window for a mid-walk vanish/rewrite race under --max-parallel
# (the residual race is covered by the bounded retry in `_copy_claude_home`).
# Patterns match by basename at every level (shutil.ignore_patterns semantics), so
# this is a denylist: anything NOT listed here (settings.json, .credentials.json,
# plugins/) is copied through.
CLAUDE_COPY_IGNORE = (
    "projects",
    "shell-snapshots",
    "todos",
    "session-env",
    "security",
    "cache",
    "file-history",
    "backups",
    "downloads",
    "sessions",
    "telemetry",
    "history.jsonl",
    "*.lock",
    # Volatile per-session churn rewritten by the live host CLI (race-prone):
    "statsig",
    ".statusline_cache",
    "paste-cache",
    "tasks",
)

# Bounded retries for the lean ~/.claude copy. The live host dir is rewritten by
# the running CLI while we walk it, so a file can vanish mid-copy and raise; a
# couple of retries clears the transient case before we give up (see
# `_copy_claude_home`).
CLAUDE_COPY_MAX_ATTEMPTS = 3

# Host-side heartbeat: the runner touches this file every HEARTBEAT_INTERVAL
# seconds while alive. The in-container watchdog exits if the file is stale
# (older than HEARTBEAT_STALE_SECONDS) -- our only defence against the host
# being SIGKILL'd (e.g. Claude Code's Escape) before the asyncio cleanup
# runs. Lives in the output dir, which is bind-mounted into the container.
HEARTBEAT_FILENAME = ".coder_eval_host_heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 2.0
HEARTBEAT_STALE_SECONDS = 20

# asyncio's StreamReader caps a single line at 64 KiB by default. The
# container streams stream events as one NDJSON line each (wire.py), and a
# single event carrying a large tool input -- e.g. an agent Write of a whole
# .flow/.json file -- serialises well past 64 KiB. The default-limit reader
# then raises ValueError mid-stream, which tore the container down before it
# wrote task.json: the entire task was lost and the host recorded a bare
# ERROR with no per-task report. Give the line reader generous headroom (run()
# also degrades gracefully past it). Mirrors Orchestrator._POST_RUN_STREAM_LIMIT,
# the same guard on the orchestrator's post-run subprocesses.
STDOUT_LINE_LIMIT_BYTES = 64 * 1024 * 1024  # 64 MiB


async def _heartbeat_loop(heartbeat_path: Path) -> None:
    """Write a monotonic counter to ``heartbeat_path`` every interval until cancelled.

    Pair with the in-container watchdog in ``run_task_internal_command``:
    container exits when the counter stops advancing for longer than
    ``HEARTBEAT_STALE_SECONDS``. We write content (not just touch) because
    bind-mount mtime on macOS Docker Desktop's gRPC-FUSE / VirtioFS can
    lag by seconds; a content-encoded counter survives stalled mtime
    semantics. Falls back gracefully if writes start failing.
    """
    counter = 0
    try:
        while True:
            counter += 1
            try:
                await asyncio.to_thread(heartbeat_path.write_text, str(counter), encoding="utf-8")
            except OSError as exc:
                logger.warning("Heartbeat write failed: %s", exc)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


def _preflight() -> None:
    """Verify ``docker`` is on PATH and the daemon is reachable.

    Cheaper than letting ``docker run`` fail mid-flight: a missing binary
    yields a clear error before we stage inputs or burn the run_dir.
    """
    if shutil.which("docker") is None:
        raise DockerRunError(
            "docker CLI not found on PATH. Install Docker Desktop or set up Docker engine before driver: docker."
        )
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
    except FileNotFoundError as exc:
        # Race: PATH check passed but the binary disappeared before exec.
        raise DockerRunError("docker CLI vanished between PATH check and exec.") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DockerRunError("docker daemon is not responding. Start Docker Desktop or check `docker info`.") from exc


def _preflight_image_version(image: str) -> None:
    """Assert the image's ``coder_eval`` label matches the host BEFORE running.

    The PR's original mismatch warning ran *after* ``task.json`` was parsed
    — i.e. after the billed LLM run. The whole point of ``--driver docker``
    is reproducibility; warning post-hoc is the wrong order. Here we inspect
    the image label and warn *before* spawning the container, so a stale
    ``:latest`` doesn't quietly waste a paid run.

    Missing image / missing label / no-host-version are all soft-fail: log
    and continue (image may have been built before the label was added, or
    coder-eval may be running from a source checkout without a packaged
    version).
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        host_version = version("coder-eval")
    except PackageNotFoundError:
        return
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{ index .Config.Labels "org.coder-eval.version" }}',
                image,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        # Image absent locally or inspect failed. Let `docker run` raise the
        # canonical error; suppress here so we don't double-fail in argv
        # logging paths that hit this even when the image is fine.
        logger.debug("Pre-flight image inspect failed for %s: %s", image, exc)
        return
    image_version = result.stdout.strip()
    if not image_version or image_version == "unknown":
        logger.warning(
            "Image %s has no org.coder-eval.version label; rebuild with `make docker-image` for pre-flight checks.",
            image,
        )
        return
    if image_version != host_version:
        logger.warning(
            "Image %s coder_eval %s != host %s. Rebuild with `make docker-image` to keep reproducibility.",
            image,
            image_version,
            host_version,
        )


def _image_supports_agent_isolation(image: str) -> bool:
    """Whether the image declares the UID/GID launch boundary capability.

    Raises :class:`DockerRunError` only when the capability cannot be checked
    at all (image missing, docker broken) -- an unlabeled image is a normal
    "no support" answer, not an error.
    """

    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{ index .Config.Labels "org.coder-eval.agent-isolation" }}',
                image,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise DockerRunError(
            f"cannot verify UID/GID isolation support for image {image!r}; build or pull the image first"
        ) from exc
    return result.stdout.strip() == "uid-gid-v1"


_CONTAINER_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_.-]")

# A leading Windows drive letter (``C:\foo`` / ``c:/foo``). Used so the colon
# in ``C:\foo`` is not misread as the ``src:dst`` separator when a Windows
# task author writes an extra_mounts entry. Bare ``C:`` (no path body) is
# intentionally not matched — that is malformed and should fail downstream.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")


def _sanitize_container_name_component(s: str) -> str:
    """Strip characters Docker rejects in `--name` so dataset row IDs work.

    Suite/row tasks have ids like ``suite_id/row_id``; ``/`` is invalid in
    Docker names. Anything outside ``[a-zA-Z0-9_.-]`` collapses to ``_``.
    """
    return _CONTAINER_NAME_INVALID.sub("_", s)


# Destinations that would shadow framework-owned mounts inside the container.
# Letting a user spec collide with these silently breaks input/output staging.
# Same reserved set the workspace-dir validator uses (single source of truth in
# models.container_paths). Extra-mount destinations and WORKDIR both reject these.
_RESERVED_MOUNT_DESTS = RESERVED_CONTAINER_DIRS


def _validate_extra_mount(spec: str) -> str:
    """Sanity-check a ``-v`` mount spec and return a normalized form.

    Defends against typos that would silently expose the host fs to the
    container, and against mount specs that shadow framework-owned mounts.
    Normalizes the source side by expanding ``~`` and ``$VAR`` so authors
    can write portable specs. Returns the (possibly rewritten) spec to
    feed back into argv.

    Notes:
      - Mode is REQUIRED. Forgetting ``:ro`` is the single most common way
        to accidentally hand the container RW access to a host directory,
        so we make the author write it explicitly.
      - Destinations colliding with framework mounts (``/work``, ``/``,
        etc.) are rejected outright.
    """
    # Split off an optional leading Windows drive letter so the colon in
    # ``C:\foo`` is not misread as the ``src:dst`` separator. The container
    # side is always POSIX (Docker containers are Linux), so only the source
    # side can carry a drive letter.
    if _DRIVE_PREFIX.match(spec):
        head, body = spec[:2], spec[2:]
    else:
        head, body = "", spec
    parts = body.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: expected `src:dst[:ro|rw]`.")
    src, dst = head + parts[0], parts[1]
    # Default to read-only when mode is omitted. Mounting host paths RW
    # by default is the wrong sandbox stance: the few RW use-cases are
    # better stated explicitly than implied by silence.
    mode = parts[2] if len(parts) == 3 else "ro"
    if not src:
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: empty source path.")
    if not dst:
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: empty destination path.")
    if not dst.startswith("/"):
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: destination must be an absolute path.")
    if mode not in ("ro", "rw"):
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: mode must be 'ro' or 'rw'.")
    # Expand ~ and $VAR in the source so authors can write portable specs.
    expanded_src = os.path.expandvars(os.path.expanduser(src))
    if not Path(expanded_src).exists():
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: source path does not exist on host.")
    # Reject destinations that shadow framework-owned mounts inside the
    # container. ``/work`` substrings are caught too -- /work/foo would
    # land underneath our staging dir and shadow the input/output tree.
    dst_norm = dst.rstrip("/") or "/"
    if dst_norm in _RESERVED_MOUNT_DESTS or dst_norm.startswith(CONTAINER_WORK_DIR + "/"):
        raise ValueError(
            f"Invalid extra_mounts entry {spec!r}: destination {dst_norm!r} shadows a framework-owned mount."
        )
    return f"{expanded_src}:{dst}:{mode}"


class DockerRunError(RuntimeError):
    """Raised when ``docker run`` exits non-zero AND no task.json was produced.

    Criterion failures do NOT raise this -- the container always writes
    task.json (with whatever results it has) before exiting, and the host
    parses that regardless of exit code. This is reserved for setup-time
    failures: missing image, daemon down, OOM-kill before the agent started,
    etc.
    """


class DockerBuildError(DockerRunError):
    """Raised when ``docker build`` itself fails (the image never builds).

    A subclass of :class:`DockerRunError` so existing ``except DockerRunError``
    handlers still catch it, but distinct so the failure is recorded as
    :data:`FinalStatus.BUILD_FAILED` (an environment/setup failure) rather than
    a generic ERROR. Carries the full build log so the runner can persist it to
    ``docker.log`` -- without this, a build failure happens before ``run_dir``
    exists and the task vanishes with no log and no task.json.
    """

    def __init__(self, message: str, *, build_log: str = "") -> None:
        super().__init__(message)
        self.build_log = build_log


def _assert_workspace_not_reserved(path: str) -> None:
    """Reject a workspace dir that collides with a framework-reserved container path.

    Defense-in-depth against the same ``RESERVED_CONTAINER_DIRS`` set the
    ``SandboxConfig`` validator uses: a concrete path is already validated at the
    model layer, but an ``"auto"``-detected image WORKDIR (or a directly built
    argv) has not been -- so re-check here before it reaches ``docker run -w``.
    """
    norm = path.rstrip("/") or "/"
    if norm in RESERVED_CONTAINER_DIRS or norm.startswith(CONTAINER_WORK_DIR + "/"):
        raise DockerRunError(
            f"working_dir {path!r} collides with a framework-reserved container path (/, /work, /work/*)."
        )


def _resolve_workspace_dir(cfg_working_dir: str | None, image: str) -> str | None:
    """Resolve the concrete agent workspace path (docker WORKDIR alignment).

    ``None`` -> ``None`` (feature off). A concrete path -> re-asserted + returned.
    ``"auto"`` -> the image's WORKDIR via ``docker image inspect`` (falling back to
    ``/root`` on an empty / ``"/"`` WORKDIR or any inspect failure -- never crash
    the run over WORKDIR detection, mirroring ``_preflight_image_version``).
    """
    if cfg_working_dir is None:
        return None
    if cfg_working_dir == "auto":
        resolved = "/root"
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Config.WorkingDir}}", image],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            workdir = result.stdout.strip()
            if workdir and workdir != "/":
                resolved = workdir
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug("WORKDIR inspect failed for %s; falling back to /root: %s", image, exc)
        cfg_working_dir = resolved
    _assert_workspace_not_reserved(cfg_working_dir)
    return cfg_working_dir


def _copy_claude_home(host_claude_dir: Path, claude_copy: Path) -> None:
    """Copy the host ``~/.claude`` into ``claude_copy`` with bounded retries.

    The harness itself runs inside Claude Code, so the *live* host ``~/.claude``
    is actively rewritten (small state JSON, session ephemera) while this walks
    it. Under ``--max-parallel>1`` N tasks copy it concurrently, and a file that
    vanishes or is rewritten mid-walk makes ``shutil.copytree`` raise
    ``FileNotFoundError`` / ``shutil.Error`` (both ``OSError`` subclasses). Left
    uncaught that propagates to ``run_single``'s broad ``except`` and flips an
    otherwise-passing task to ``FinalStatus.ERROR`` — scoring identical agent
    output differently by luck of timing. ``CLAUDE_COPY_IGNORE`` already drops the
    noisiest churn dirs; this retries the residual race a bounded number of times
    (clearing the partial copy between attempts) before giving up. Persistent
    failure still raises — at that point it is a real problem (e.g. perms), and
    the container could not authenticate without ``~/.claude`` anyway.
    """
    last_exc: OSError | None = None
    for attempt in range(1, CLAUDE_COPY_MAX_ATTEMPTS + 1):
        try:
            shutil.copytree(
                host_claude_dir,
                claude_copy,
                ignore=shutil.ignore_patterns(*CLAUDE_COPY_IGNORE),
                # Copy symlinks AS symlinks (do not follow): a plugin marketplace
                # cache can contain a self-referential symlink (e.g. uipath-marketplace
                # `plugins/uipath -> ..`) that makes a symlink-following walk recurse
                # infinitely ("too many levels of symbolic links") and abort the copy.
                # Copying them verbatim is correct and loop-proof. Dangling ones are
                # skipped via ignore_dangling_symlinks.
                symlinks=True,
                ignore_dangling_symlinks=True,
                dirs_exist_ok=True,
            )
            return
        except OSError as exc:  # FileNotFoundError / shutil.Error — transient under concurrent host churn
            last_exc = exc
            # Clear the partial tree so the retry (dirs_exist_ok) starts clean.
            shutil.rmtree(claude_copy, ignore_errors=True)
            logger.warning(
                "Copy of host ~/.claude failed (attempt %d/%d), retrying: %s",
                attempt,
                CLAUDE_COPY_MAX_ATTEMPTS,
                exc,
            )
    raise DockerRunError(
        f"Failed to copy host ~/.claude into the container staging dir after {CLAUDE_COPY_MAX_ATTEMPTS} "
        + f"attempts (last error: {last_exc}). The host dir may be churning faster than the copy "
        + "completes, or be unreadable."
    ) from last_exc


class DockerRunner:
    """Spawns a per-task container and reconstructs the EvaluationResult.

    One instance per task. Stateless across tasks -- batch execution just
    instantiates N runners concurrently.
    """

    def __init__(
        self,
        rt: ResolvedTask,
        preservation_mode: PreservationMode = PreservationMode.DIRECT_WRITE,
        stream_callback: StreamCallback | None = None,
        verbose: bool = False,
    ) -> None:
        self.rt = rt
        self.preservation_mode = preservation_mode
        self.stream_callback = stream_callback
        self.verbose = verbose
        # Set by _prepare_host_mounts: the tmp lean copy of ~/.claude that
        # _build_argv mounts read-write. None when there is no ~/.claude to
        # forward or the mount is opted out (CODER_EVAL_NO_CLAUDE_MOUNT).
        self._claude_mount_src: Path | None = None
        # Prepared before argv rendering. Raw sources are mounted under the
        # root-only grader parent at unrelated container paths.
        self._private_source_mounts: list[tuple[Path, str]] = []
        self._host_to_private_paths: dict[str, str] = {}
        # Resolved in run() (needs the built image for "auto"). Concrete WORKDIR the
        # agent runs at + copies out from; None = standard artifacts workspace.
        self._workspace_dir: str | None = None
        # Effective isolation for THIS run. Starts from config; run() downgrades
        # it to False (with one warning) when a prerequisite is missing -- an
        # unsupported agent type, an unlabeled image, working_dir, or
        # extra_mounts. Every isolation-conditional site below keys on this
        # resolved flag, never on the raw config value.
        self._isolation_active: bool = self.rt.task.sandbox.docker.agent_isolation

    @property
    def _docker_config(self) -> DockerDriverConfig:
        return self.rt.task.sandbox.docker

    @property
    def _limits(self) -> ResourceLimits:
        return self.rt.task.sandbox.limits

    async def run(self) -> EvaluationResult:
        """Run the task in a container and return the parsed EvaluationResult.

        The container is responsible for producing ``task.json`` in
        ``CONTAINER_OUTPUT_DIR``. On any path where the container exits
        without producing it, this raises ``DockerRunError`` and the batch
        dispatcher converts that to an ERROR-status EvaluationResult.
        """
        _preflight()
        self._resolve_agent_isolation()
        # Resolve the run image: build from a Dockerfile if configured (which
        # overrides `image`), else use the configured image. The build is
        # side-effecting, so it runs in a worker thread like the other docker
        # calls in this method.
        try:
            image = await asyncio.to_thread(self._build_image)
        except DockerBuildError as exc:
            # The build happens before run_dir/docker.log/task.json exist, so a
            # build failure would otherwise leave an empty result dir with no
            # trace. Persist the build log to docker.log and a BUILD_FAILED
            # synthetic task.json so the failure is visible per-task, then
            # re-raise for the batch dispatcher to record run-level.
            await self._record_build_failure(exc)
            raise
        # The version-label preflight only makes sense for the framework image;
        # a task-supplied Dockerfile won't carry the org.coder-eval.version label.
        if not self._docker_config.dockerfile_path:
            await asyncio.to_thread(_preflight_image_version, image)
        if self._isolation_active and not await asyncio.to_thread(_image_supports_agent_isolation, image):
            self._downgrade_isolation(f"image {image!r} does not declare org.coder-eval.agent-isolation=uid-gid-v1")
        await asyncio.to_thread(self.rt.run_dir.mkdir, parents=True, exist_ok=True)

        # Docker WORKDIR alignment: resolve the concrete workspace path
        # once, host-side (config value / "auto" -> inspect the built image / fallback
        # /root). Forwarded to the in-container orchestrator via the staged context
        # and rendered as `docker run -w`. None keeps the standard artifacts workspace.
        self._workspace_dir = await asyncio.to_thread(_resolve_workspace_dir, self._docker_config.working_dir, image)
        if self._isolation_active:
            # working_dir triggered a downgrade in _resolve_agent_isolation, so
            # the resolved workspace here is always the isolation-managed one.
            self._workspace_dir = CONTAINER_AGENT_WORK_DIR

        # Stage only the inputs (task YAML + context). The *output* dir is
        # the host's run_dir itself, bind-mounted at the same path inside
        # the container so the in-container Orchestrator writes
        # task.json/task.log/task.html/artifacts/ straight into the host
        # filesystem -- no copy step, paths are symmetric inside and out.
        # Sanitize task_id: dataset ids are ``suite_id/row_id`` and the ``/`` breaks mkdtemp (missing parent dir).
        safe_staging_id = _sanitize_container_name_component(self.rt.task.task_id)
        staging = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=f"coder_eval_docker_{safe_staging_id}_"))
        input_dir = staging / "input"
        await asyncio.to_thread(input_dir.mkdir)
        output_dir = self.rt.run_dir.resolve()

        try:
            await self._stage_inputs(input_dir)

            # Give the container a stable, *unique* name so cancellation can
            # target it. PID alone collides under --max-parallel >1 (same
            # host process spawns N concurrent containers); the uuid suffix
            # and replicate_index disambiguate. Sanitize+truncate task_id
            # so dataset row ids like ``suite/row`` don't break docker name
            # validation.
            short_uuid = uuid.uuid4().hex[:8]
            # Docker name limit is 253 chars; keep generous task_id headroom
            # so `docker ps` rows stay readable. Earlier 30-char cap collided
            # visibly on long shared prefixes; 80 covers all realistic ids
            # while leaving room for the suffix.
            safe_task_id = _sanitize_container_name_component(self.rt.task.task_id)[:80]
            container_name = f"coder-eval-{safe_task_id}-r{self.rt.replicate_index}-{os.getpid()}-{short_uuid}"
            # Side-effecting prep that _build_argv must NOT do (argv rendering
            # stays pure for testability). Makes a lean RW copy of ~/.claude
            # under `staging` and records it on self._claude_mount_src for
            # _build_argv to mount. Cleaned up with `staging` in the finally.
            await asyncio.to_thread(self._prepare_host_mounts, staging)
            argv = self._build_argv(input_dir, output_dir, container_name=container_name, image=image)
            logger.info("Running task '%s' in docker: %s", self.rt.task.task_id, " ".join(argv))
            # Prime the heartbeat before the container starts so the
            # watchdog never sees an initial stale state.
            heartbeat_path = output_dir / HEARTBEAT_FILENAME
            await asyncio.to_thread(heartbeat_path.touch)
            heartbeat_task = asyncio.create_task(_heartbeat_loop(heartbeat_path))
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=STDOUT_LINE_LIMIT_BYTES,
            )
            log_path = self.rt.run_dir / "docker.log"
            log_fh = await asyncio.to_thread(log_path.open, "w", encoding="utf-8")
            # Cancellation guard: `docker run --rm` does NOT propagate kill
            # to the container daemon-side. Without this `finally`, Ctrl-C
            # on the host leaves the container running and burning LLM
            # budget. Covers CancelledError, KeyboardInterrupt, and any
            # other exit-by-exception path uniformly.
            try:
                returncode = await self._stream_container_output(proc, log_fh)
            finally:
                heartbeat_task.cancel()
                # await the cancellation so the task doesn't outlive us;
                # narrow to CancelledError so genuine KeyboardInterrupt /
                # SystemExit from a parallel sibling still propagates.
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                await asyncio.to_thread(log_fh.close)
                # If proc is still alive we got cancelled mid-flight. Kill
                # the container *and* the docker CLI subprocess. Best-effort,
                # no exception leak from cleanup.
                if proc.returncode is None:
                    await self._kill_container(proc, container_name)

            return await self._parse_result_or_raise(output_dir, returncode, log_path)
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, ignore_errors=True)

    async def _stage_inputs(self, input_dir: Path) -> None:
        """Serialise the post-override TaskDefinition + lineage/variant context into the
        staging ``input_dir`` (``task.yaml`` + ``context.json``). Pure I/O off the event
        loop; no control-flow change.
        """
        # Always serialise the *post-override* TaskDefinition. We can't use
        # rt.source_yaml because that's the raw on-disk text -- _apply_cli_overrides
        # has since mutated rt.task in-memory (e.g. --model, -D run_limits.max_turns), and the
        # container needs to see those mutations.
        task_yaml_in = input_dir / "task.yaml"
        task_payload = self.rt.task.model_dump(mode="json")
        if self._isolation_active:
            await asyncio.to_thread(self._prepare_isolated_sources)
            task_payload = self._rewrite_task_paths(task_payload)

        def _dump_task_yaml() -> str:
            return yaml.safe_dump(task_payload, sort_keys=False)

        task_yaml_text = await asyncio.to_thread(_dump_task_yaml)
        await asyncio.to_thread(task_yaml_in.write_text, task_yaml_text, encoding="utf-8")
        # Lineage + variant metadata so the in-container Orchestrator
        # reconstructs the same context (variant_id is load-bearing for
        # report grouping). source_yaml carries the *raw* on-disk text
        # so the in-container Orchestrator records the same audit trail
        # as the in-process driver (task.json.task_config.source_yaml).
        context_payload = json.dumps(
            {
                "variant_id": self.rt.variant_id,
                "replicate_index": self.rt.replicate_index,
                "config_lineage": {k: v.model_dump(mode="json") for k, v in self.rt.config_lineage.items()},
                "preservation_mode": self.preservation_mode.value,
                "source_yaml": self.rt.source_yaml,
                # Docker WORKDIR alignment: concrete path the in-container
                # orchestrator runs at + captures out (None = standard workspace).
                "workspace_dir": self._workspace_dir,
            }
        )
        await asyncio.to_thread((input_dir / "context.json").write_text, context_payload, encoding="utf-8")

    def _downgrade_isolation(self, reason: str) -> None:
        """Turn agent isolation off for this run and say so once, loudly."""

        self._isolation_active = False
        logger.warning(
            "Agent isolation is unavailable for task '%s': %s. Running WITHOUT agent isolation "
            + "(normal single-identity container).",
            self.rt.task.task_id,
            reason,
        )

    def _resolve_agent_isolation(self) -> None:
        """Downgrade to a normal run when a config prerequisite is missing.

        Isolation is best-effort: a missing prerequisite runs the task in the
        pre-isolation single-identity container instead of failing the run.
        Dynamic criteria (``run_command``, ``uipath_eval``, ``agent_judge``)
        never gate isolation -- they execute in the grader phase, after the
        agent identity has been stopped and reaped. The image capability label
        is checked separately in run() once the image is built.
        """

        if not self._isolation_active:
            return
        agent_type = str(self.rt.task.agent.type) if self.rt.task.agent and self.rt.task.agent.type else ""
        supported_agents = {
            AgentKind.CLAUDE_CODE.value,
            AgentKind.CODEX.value,
            AgentKind.ANTIGRAVITY.value,
            AgentKind.NONE.value,
        }
        if agent_type not in supported_agents:
            self._downgrade_isolation(f"no verified UID-drop launch seam for agent type {agent_type!r}")
        elif self._docker_config.working_dir is not None:
            self._downgrade_isolation("docker.working_dir has no UID-drop support yet")
        elif self._docker_config.extra_mounts:
            self._downgrade_isolation("extra_mounts have an ambiguous agent/private audience")

    def _prepare_isolated_sources(self) -> None:
        """Prepare private raw-source mount mappings.

        This method never changes ownership or permissions on a source checkout.
        Raw sources remain read-only and are mounted only below the image's
        root-owned ``/opt/coder-eval/grader`` directory.
        """

        self._private_source_mounts = []
        self._host_to_private_paths = {}

        task_dir = self.rt.task_file.parent.resolve() if self.rt.task_file else None
        if task_dir is not None:
            self._host_to_private_paths[str(task_dir)] = CONTAINER_TASK_DIR

        if self.rt.task.agent and self.rt.task.agent.system_prompt_file:
            raise DockerRunError(
                "docker.agent_isolation requires system_prompt_file to be resolved to inline system_prompt "
                + "before container staging"
            )

        from coder_eval.models import TemplateDirSource

        template_index = 0
        for source in self.rt.task.sandbox.template_sources or []:
            if not isinstance(source, TemplateDirSource):
                continue
            host_path = Path(source.path).resolve()
            if task_dir is not None and (host_path == task_dir or task_dir in host_path.parents):
                continue
            self._register_private_mount(host_path, f"/opt/coder-eval/grader/templates/source-{template_index}")
            template_index += 1

        reference = self.rt.task.reference
        if reference is not None:
            if reference.directory:
                self._register_external_private_path(
                    Path(reference.directory), "/opt/coder-eval/grader/references/directory"
                )
            if reference.file:
                reference_file = Path(reference.file).resolve()
                self._register_external_private_path(
                    reference_file.parent, "/opt/coder-eval/grader/references/file-parent"
                )

    def _register_external_private_path(self, source: Path, target: str) -> None:
        source = source.resolve()
        task_dir = self.rt.task_file.parent.resolve() if self.rt.task_file else None
        if task_dir is not None and (source == task_dir or task_dir in source.parents):
            return
        self._register_private_mount(source, target)

    def _register_private_mount(self, source: Path, target: str) -> None:
        source = source.resolve()
        key = str(source)
        if key in self._host_to_private_paths:
            return
        self._host_to_private_paths[key] = target
        self._private_source_mounts.append((source, target))

    def _rewrite_task_paths(self, payload: dict[str, object]) -> dict[str, object]:
        """Rewrite host paths to their protected container mount locations."""

        replacements = sorted(self._host_to_private_paths.items(), key=lambda item: len(item[0]), reverse=True)

        def rewrite(value: object) -> object:
            if isinstance(value, str):
                for source, target in replacements:
                    value = value.replace(source, target)
                return value
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            return value

        rewritten = rewrite(payload)
        if not isinstance(rewritten, dict):
            raise DockerRunError("internal error rewriting staged task paths")
        return rewritten

    async def _stream_container_output(self, proc: asyncio.subprocess.Process, log_fh: TextIO) -> int:
        """Stream the container's stdout, returning its exit code.

        Wire-format lines emit to the host ``StreamCallback``; plain lines are written
        to ``docker.log``. A single over-limit line is dropped (degrade, not die) — the
        ``readline`` ``ValueError`` resyncs at the next newline. Runs as the inner-``try``
        body of ``run``; the caller owns the ``finally`` cleanup, so this helper never
        touches the heartbeat/log-fh/container teardown.
        """
        assert proc.stdout is not None
        # Explicit readline loop (not `async for`) so a single
        # over-limit line degrades to a dropped line instead of a
        # ValueError that tears the whole task down -- see below.
        while True:
            try:
                raw_line = await proc.stdout.readline()
            except ValueError:
                # A single line exceeded STDOUT_LINE_LIMIT_BYTES.
                # readline() drains the offending bytes and resyncs at
                # the next newline, so we keep streaming. The dropped
                # line is a STREAM_EVENT (host-side live render) or a
                # log line; task.json crosses via the bind mount, not
                # stdout, so the task result is unaffected. Degrade,
                # don't die.
                logger.warning(
                    "Dropped a stdout line over %d bytes from task %r's container; continuing to stream.",
                    STDOUT_LINE_LIMIT_BYTES,
                    self.rt.task.task_id,
                )
                continue
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            # Three-way split:
            #   - Has the wire-format prefix AND parses cleanly -> emit
            #     to the host StreamCallback; do not echo to docker.log
            #     (the StreamCallback is the canonical destination).
            #   - Has the prefix but parses badly -> wire bug;
            #     deserialize_event already logged a WARN. Preserve
            #     the raw line in docker.log so it isn't lost.
            #   - No prefix -> plain log line.
            if has_prefix(line):
                event = deserialize_event(line)
                if event is not None:
                    safe_emit(self.stream_callback, event)
                    continue
                # fall through to log preservation
            log_fn = logger.info if self.verbose else logger.debug
            log_fn("[docker:%s] %s", self.rt.task.task_id, line)
            await asyncio.to_thread(log_fh.write, line + "\n")
            await asyncio.to_thread(log_fh.flush)
        return await proc.wait()

    async def _kill_container(self, proc: asyncio.subprocess.Process, container_name: str) -> None:
        """Best-effort teardown when cancelled mid-stream with the container still alive.

        Called from ``run``'s inner ``finally`` (after heartbeat-cancel + log-fh close),
        guarded by ``if proc.returncode is None``. ``docker run --rm`` does NOT propagate
        a host-side kill to the daemon, so kill the container by name and then the docker
        CLI subprocess. No exception leaks from cleanup; suppression is narrowed to
        CancelledError so KeyboardInterrupt / SystemExit from parallel siblings propagate.
        """
        logger.warning("Cleanup: killing container %s", container_name)
        try:
            kill_result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "kill", container_name],
                capture_output=True,
                check=False,
                timeout=10,
            )
            if kill_result.returncode == 0:
                logger.info("Container %s killed cleanly.", container_name)
            else:
                # Non-zero from `docker kill` typically means the
                # container was already gone (race with --rm) OR
                # the daemon refused. Surface stderr so the
                # ambiguity is debuggable.
                logger.warning(
                    "docker kill %s returned %s; container may already be gone or daemon refused: %s",
                    container_name,
                    kill_result.returncode,
                    kill_result.stderr.decode("utf-8", errors="replace").strip(),
                )
        except subprocess.TimeoutExpired:
            # Daemon hung; container may now be orphaned daemon-side.
            # Loud so an operator notices and prunes manually.
            logger.error(
                "docker kill %s timed out after 10s; container may be orphaned. Investigate `docker ps`.",
                container_name,
            )
        except (OSError, subprocess.SubprocessError) as kill_exc:
            logger.warning("docker kill failed: %s", kill_exc)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # Narrow to CancelledError -- a generic BaseException
        # catch here would silently eat KeyboardInterrupt /
        # SystemExit propagation from parallel tasks.
        with contextlib.suppress(asyncio.CancelledError):
            await proc.wait()

    async def _parse_result_or_raise(self, output_dir: Path, returncode: int, log_path: Path) -> EvaluationResult:
        """Read back ``task.json`` (the only artifact crossing the boundary) and parse it.

        If the container exited without producing it, persist a synthetic ERROR
        task.json and raise ``DockerRunError`` so the batch dispatcher records the
        failure as an ERROR-status result.
        """
        task_json = output_dir / "task.json"
        if not await asyncio.to_thread(task_json.exists):
            # The container died before its orchestrator's `finally` could
            # write task.json (e.g. it was torn down by the cleanup above
            # after a host-side stream failure, or killed externally).
            # Persist a synthetic ERROR task.json so the test stays
            # visible on dashboards/timelines instead of silently
            # vanishing -- the batch layer's in-memory skeleton never
            # reaches the per-task dir.
            error = DockerRunError(
                f"Container exited with code {returncode} without producing task.json. "
                + f"See {log_path} for container output."
            )
            await self._write_synthetic_task_json(task_json, error)
            raise error

        # output_dir IS rt.run_dir -- no copy needed.
        task_json_text = await asyncio.to_thread(task_json.read_text, encoding="utf-8")
        try:
            result = EvaluationResult.model_validate_json(task_json_text)
        except ValueError as exc:
            # Present but unparseable (schema skew from a stale image, or a
            # truncated/torn write). Degrade like the missing-file branch
            # rather than crashing with an uncaught ValidationError/JSONDecodeError.
            raise await self._handle_malformed_task_json(task_json, log_path, exc) from exc
        self._warn_on_version_mismatch(result)
        return result

    async def _handle_malformed_task_json(self, task_json: Path, log_path: Path, exc: ValueError) -> DockerRunError:
        """Degrade a present-but-malformed task.json; return the DockerRunError to raise.

        Triggered by a present-but-unparseable task.json -- most realistically a
        schema skew between a stale ``:latest`` image and the host (the version
        checks only warn), or a truncated/torn write. Mirrors the missing-file
        branch and the batch.py recovery paths: log naming the path, move the
        original aside to ``task.json.malformed`` (so its possibly-recoverable
        content isn't masked AND so the synthetic write lands --
        ``_write_synthetic_task_json`` never overwrites an existing file),
        persist a synthetic ERROR record (per-task dashboard visibility), and
        return the error for the caller to raise (the batch layer records the
        run-level ERROR). Best-effort throughout: a failed move is logged, never
        masking the raise.
        """
        logger.warning("Malformed task.json at %s: %s", task_json, exc)
        sidecar = task_json.with_suffix(task_json.suffix + ".malformed")

        def _move() -> None:
            os.replace(task_json, sidecar)  # atomic; overwrites any stale prior .malformed

        try:
            await asyncio.to_thread(_move)
        except OSError as move_exc:
            logger.warning("Failed to preserve malformed task.json %s: %s", task_json, move_exc)

        error = DockerRunError(f"task.json at {task_json} is malformed. See {log_path} for container output.")
        await self._write_synthetic_task_json(task_json, error)
        return error

    async def _record_build_failure(self, exc: DockerBuildError) -> None:
        """Persist a failed image build so it is visible, not a silent empty dir.

        ``_build_image`` runs before ``run_dir``, ``docker.log``, or ``task.json``
        exist, so a build failure used to leave an empty result directory with no
        status and no log. This creates ``run_dir``, writes the captured build log
        to ``docker.log`` (where every per-task consumer already looks for
        container output), and writes a synthetic ``BUILD_FAILED`` task.json.
        Best-effort: any IO failure here is logged and never masks the
        ``DockerBuildError`` the caller re-raises.
        """
        try:
            await asyncio.to_thread(self.rt.run_dir.mkdir, parents=True, exist_ok=True)
            log_path = self.rt.run_dir / "docker.log"
            await asyncio.to_thread(log_path.write_text, exc.build_log or str(exc), encoding="utf-8")
            await self._write_synthetic_task_json(self.rt.run_dir / "task.json", exc, status=FinalStatus.BUILD_FAILED)
        except OSError as io_exc:  # pragma: no cover - defensive
            logger.warning("Failed to record build failure for %s: %s", self.rt.task.task_id, io_exc)

    async def _write_synthetic_task_json(
        self, target: Path, error: DockerRunError, *, status: FinalStatus = FinalStatus.ERROR
    ) -> None:
        """Persist a minimal error task.json for a container that died pre-write.

        A container killed mid-task (SIGKILL, or torn down by our own
        cancellation cleanup) never reaches the in-container `finally` that
        writes task.json, so without this the task is recorded only in the
        batch layer's in-memory error skeleton and vanishes from every
        per-task consumer (dashboard, timelines). Reuses
        :func:`build_error_result` -- the documented mirror of
        ``_create_error_task_result`` -- and the Orchestrator's own
        ``model_dump_json(indent=2)`` serialization so downstream readers
        parse it unchanged.

        Atomic (tmp + os.replace), never overwrites an existing task.json
        (if the container won the race after all, the real result wins), and
        best-effort: a write failure logs a warning and never masks the
        DockerRunError the caller is about to raise.
        """
        result = build_error_result(self.rt, error, status=status)

        def _write() -> None:
            if target.exists():
                return
            tmp = target.with_suffix(target.suffix + ".synthetic.tmp")
            tmp.write_text(result.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, target)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.warning("Failed to write synthetic task.json to %s: %s", target, exc)

    def _warn_on_version_mismatch(self, result: EvaluationResult) -> None:
        """Warn loudly if the in-container coder_eval version != the host's.

        Reproducibility is one of two reasons users pick driver:docker.
        Without this check, an outdated image silently runs stale code
        against a refreshed host -- a class of "works on my machine"
        regression that's near-impossible to debug. The host already
        embeds its own version in environment_info before this point.
        """
        from importlib.metadata import PackageNotFoundError, version

        try:
            host_version = version("coder-eval")
        except PackageNotFoundError:
            return
        env_info = result.environment_info or {}
        if "coder_eval" not in env_info:
            # Surface the silent-disable. Future refactor removing this key
            # would otherwise stop the version check without anyone noticing.
            logger.warning(
                "Cannot verify container coder_eval version: result.environment_info missing 'coder_eval' key."
            )
            return
        container_version = env_info["coder_eval"]
        if container_version and container_version != host_version:
            logger.warning(
                "coder_eval version mismatch -- host %s, container %s. Rebuild image with `make docker-image`.",
                host_version,
                container_version,
            )

    @staticmethod
    def _sensitive_source_paths() -> list[Path]:
        """Host paths whose auto-mount should emit a loud warning.

        Not a hard denylist: there are legitimate task shapes that need to
        read e.g. ``~/.aws`` (cloud-deploy validators). Warning gives the
        author visibility without breaking those tasks.
        """
        home = Path.home()
        candidates = [
            home / ".ssh",
            home / ".aws",
            home / ".gnupg",
            home / ".config" / "gh",
            home / ".kube",
            Path("/etc"),
        ]
        return [p.resolve() for p in candidates if p.exists()]

    def _prepare_host_mounts(self, staging: Path) -> None:
        """Side-effecting prep that ``_build_argv`` must not do.

        Makes a *lean copy* of the host's ``~/.claude`` into a throwaway dir
        under ``staging`` and records it on ``self._claude_mount_src``.
        ``_build_argv`` then bind-mounts that copy read-WRITE at the host's
        ``~/.claude`` path (HOME is forwarded, so the path is symmetric inside
        the container). Mounting a copy — rather than the host dir read-only —
        lets the in-container CLI write anywhere under ``~/.claude`` without
        ever mutating the host's real state.

        The copy skips heavy, container-irrelevant per-session state
        (``CLAUDE_COPY_IGNORE``) so it stays cheap even in parallel batches.

        The copy lives under ``staging``, which ``run()`` removes in its
        ``finally``, so there is no extra cleanup to track. Argv rendering must
        stay pure (it may run twice — for logging then exec), so the copy is
        made here, exactly once, rather than in ``_build_argv``.
        """
        if os.environ.get("CODER_EVAL_NO_CLAUDE_MOUNT"):
            return
        host_claude_dir = Path.home() / ".claude"
        if not host_claude_dir.is_dir():
            return
        claude_copy = staging / "claude-home"
        _copy_claude_home(host_claude_dir, claude_copy)
        self._claude_mount_src = claude_copy

    def _build_image(self) -> str:
        """Resolve the image to run, building from a Dockerfile when configured.

        When ``docker.dockerfile_path`` is set it overrides ``docker.image``:
        we shell out to ``docker build`` using the Dockerfile's parent directory
        as the build context (so relative ``COPY`` paths resolve) and tag the
        result with a deterministic, per-task name so Docker's layer cache is
        reused across runs. ``docker.build`` (:class:`DockerBuildConfig`) adds
        ``--build-arg`` / ``--secret`` / extra flags; the build runs with
        BuildKit enabled. Otherwise the configured ``image`` is returned
        unchanged.

        **Contract:** the container runs the coder-eval orchestrator. The image
        bakes no ``ENTRYPOINT``; the host pins it at run time via
        ``docker run --entrypoint`` (see :meth:`_build_argv`). A task Dockerfile
        must therefore start ``FROM coder-eval-agent:<version>`` and only ADD
        task-specific layers, so the runtime (the ``coder_eval_entrypoint.sh``
        script + the ``coder-eval`` CLI + the ``org.coder-eval.version`` label)
        is present. After building we assert that label is present and fail with
        an actionable error otherwise -- without this, a bare ``FROM ubuntu``
        image builds fine, then dies at ``docker run`` with a cryptic
        ``exec: "/usr/local/bin/coder_eval_entrypoint.sh": no such file``.

        Side-effecting (network + docker daemon state); call via
        ``asyncio.to_thread`` from :meth:`run`, never from :meth:`_build_argv`,
        which must stay pure.

        Returns:
            The image reference to pass to ``docker run``.

        Raises:
            DockerRunError: If ``docker build`` exits non-zero, or the built
                image is not a coder-eval runtime image (missing the
                ``org.coder-eval.version`` label).
        """
        cfg = self._docker_config
        if not cfg.dockerfile_path:
            return cfg.image
        dockerfile = Path(cfg.dockerfile_path)
        context = dockerfile.parent
        # Image repository names must be lowercase; task ids are typically
        # already kebab-case, but lowercase defensively. Deterministic tag ->
        # Docker layer cache is reused across runs of the same task.
        safe_id = _sanitize_container_name_component(self.rt.task.task_id).lower()
        image = f"coder-eval-task-{safe_id}:built"

        # Assemble the build argv from config: base flags, then task-supplied
        # --build-arg / --secret / extra flags, then the context (always last).
        build = cfg.build
        argv = ["docker", "build", "-t", image, "-f", str(dockerfile)]
        for key, value in build.args.items():
            argv += ["--build-arg", f"{key}={os.path.expandvars(value)}"]
        for spec in build.secrets:
            argv += ["--secret", spec]
        argv += build.extra_args
        argv.append(str(context))

        # BuildKit (required for `--secret`) is inherited from the invoking
        # environment by default; `build.buildkit` forces it on/off when set.
        env = os.environ.copy()
        if build.buildkit is not None:
            env["DOCKER_BUILDKIT"] = "1" if build.buildkit else "0"
        if build.secrets and env.get("DOCKER_BUILDKIT") != "1":
            logger.warning(
                "docker.build.secrets is set but BuildKit is not enabled (DOCKER_BUILDKIT=%s); "
                + "secrets require BuildKit. Set docker.build.buildkit: true or export DOCKER_BUILDKIT=1.",
                env.get("DOCKER_BUILDKIT", "<unset>"),
            )

        # Log only high-level info here.
        logger.info("Building docker image %s from %s (context %s)", image, dockerfile, context)
        try:
            subprocess.run(argv, check=True, capture_output=True, text=True, encoding="utf-8", env=env)
        except subprocess.CalledProcessError as exc:
            # Preserve the full build output (stdout+stderr) so run() can persist
            # it to docker.log; the message keeps the concise stderr tail.
            build_log = (exc.stdout or "") + (exc.stderr or "")
            raise DockerBuildError(
                f"Failed to build Docker image from {dockerfile}: {exc.stderr}", build_log=build_log
            ) from exc
        self._assert_runtime_image(image, dockerfile)
        return image

    def _assert_runtime_image(self, image: str, dockerfile: Path) -> None:
        """Fail fast unless the built image carries the coder-eval runtime.

        The host pins ``--entrypoint`` at run time, so we no longer inspect the
        baked ``ENTRYPOINT``; instead we verify the image is a coder-eval runtime
        image by checking for the ``org.coder-eval.version`` label, which
        docker/Dockerfile stamps and any ``FROM coder-eval-agent`` task inherits.
        This is the only pre-run validation for a ``dockerfile_path`` task
        (``run()`` skips :func:`_preflight_image_version` for that case), so
        without it a bare ``FROM ubuntu`` image would build, then die at
        ``docker run`` with a cryptic ``exec ...coder_eval_entrypoint.sh: no
        such file``. A docker/inspect failure is soft (debug-logged, no raise):
        the subsequent ``docker run`` surfaces any real problem.

        Raises:
            DockerRunError: If the image carries no ``org.coder-eval.version``
                label (i.e. it is not built ``FROM coder-eval-agent``).
        """
        try:
            result = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "org.coder-eval.version" }}',
                    image,
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug("Could not inspect labels of built image %s: %s", image, exc)
            return
        # `docker inspect` renders a missing label as the empty string (the Go
        # template's zero value); "<no value>" can occur on older clients.
        label = result.stdout.strip()
        if not label or label == "<no value>":
            base = get_default_docker_image_tag()
            raise DockerRunError(
                f"Image built from {dockerfile} is not a coder-eval runtime image "
                + "(missing the org.coder-eval.version label). The container must run the "
                + f"in-container orchestrator, so a task Dockerfile must start `FROM {base}` "
                + "(the framework image, built via `make docker-image`) and only add "
                + "task-specific layers on top. See docs/DOCKER_ISOLATION.md."
            )

    def _build_argv(  # noqa: PLR0912, PLR0915 - one ordered rendering pipeline mirrors docker-run argv
        self, input_dir: Path, output_dir: Path, *, container_name: str, image: str | None = None
    ) -> list[str]:
        cfg = self._docker_config
        # `image` is resolved by run() via _build_image() (which may shell out to
        # `docker build`). _build_argv stays pure -- no side effects -- so it
        # remains testable without a docker daemon. Fall back to the configured
        # image when called directly (e.g. unit tests of mount rendering).
        if image is None:
            image = cfg.image

        # tini as PID 1 reaps orphaned grandchildren. The harness also scans
        # and kills the dedicated agent UID before finalization; --init keeps a
        # double-forked process from surviving only as an unreapable zombie.
        argv: list[str] = ["docker", "run", "--rm", "--init", "--name", container_name]

        # Pin the framework entrypoint at run time rather than trusting whatever
        # the task image baked into ENTRYPOINT. This makes the orchestrator launch
        # robust to a task Dockerfile that sets its own ENTRYPOINT/CMD (or clears
        # it via `ENTRYPOINT []`). `--entrypoint` resets the image CMD, which is
        # fine -- the run command (`--output`/`--task-dir`, appended after the
        # image) is passed explicitly below and is forwarded to the entrypoint.
        argv += ["--entrypoint", CONTAINER_ENTRYPOINT]

        if cfg.network == "none":
            argv += ["--network", "none"]
        else:
            argv += ["--network", "bridge"]

        if self._limits.max_memory_mb:
            argv += ["--memory", f"{self._limits.max_memory_mb}m"]
        if self._limits.max_cpus is not None:
            argv += ["--cpus", str(self._limits.max_cpus)]
        if self._limits.max_pids is not None:
            argv += ["--pids-limit", str(self._limits.max_pids)]
        elif self._isolation_active:
            argv += ["--pids-limit", str(DEFAULT_AGENT_ISOLATION_MAX_PIDS)]

        # Forward environment variables: explicit allowlist (optionally extended via env_passthrough_extra).
        # `--env VAR` (name-only) tells docker to copy the value from our current env at
        # run time, so secrets stay out of the rendered argv list that we log.
        #
        # The run's backend rides this same path: API_BACKEND is in the default allowlist,
        # and `--backend` syncs it into os.environ at the CLI (run_command), so it forwards
        # here exactly like every other allowlisted var. A flag that only mutated in-process
        # Settings would be dropped at the container boundary and the in-container Settings
        # would silently default to DIRECT — downgrading the judge (and agent) route.
        merged_allowlist = set(cfg.env_passthrough) | set(cfg.env_passthrough_extra)
        for env_var in merged_allowlist:
            # LITELLM_BASE_URL / LITELLM_COST_LOG are forwarded below with a value
            # rewrite (host alias / absolute mount path), not name-only.
            if env_var in ("LITELLM_BASE_URL", "LITELLM_COST_LOG", "SKILLS_REPO_PATH"):
                continue
            if self._isolation_active and env_var == "HOME":
                continue
            if env_var in os.environ:
                argv += ["--env", env_var]

        if self._isolation_active and (skills_repo := os.environ.get("SKILLS_REPO_PATH")):
            resolved_skills = str(Path(skills_repo).expanduser().resolve())
            private_skills = self._host_to_private_paths.get(resolved_skills)
            if private_skills is not None:
                argv += ["--env", f"SKILLS_REPO_PATH={private_skills}"]
            else:
                logger.debug("Not forwarding unstaged SKILLS_REPO_PATH into protected container: %s", resolved_skills)

        # LITELLM_BASE_URL points at a proxy on the HOST. A bridge-network container
        # can't reach the host's loopback, so rewrite localhost/127.0.0.1 to the
        # docker host alias and publish that alias (`--add-host`) for Linux parity
        # (it's automatic on macOS/Windows Docker Desktop). It's only a URL, so an
        # explicit `--env VAR=value` is safe to render in the logged argv — unlike
        # the auth token, which stays name-only above. Skipped when the container
        # has no network (the proxy is unreachable anyway → validation errors).
        litellm_base_url = os.environ.get("LITELLM_BASE_URL")
        if litellm_base_url and "LITELLM_BASE_URL" in merged_allowlist and cfg.network != "none":
            rewritten = _rewrite_loopback_for_container(litellm_base_url)
            if rewritten is not None:
                argv += ["--env", f"LITELLM_BASE_URL={rewritten}", "--add-host", f"{_DOCKER_HOST_ALIAS}:host-gateway"]
            else:
                argv += ["--env", "LITELLM_BASE_URL"]

        # LITELLM_COST_LOG is the proxy's per-call cost log, written on the HOST by
        # the proxy; the in-container Orchestrator's actual-cost join READS it. So
        # bind-mount its directory at the SAME host path (read-only — the container
        # only reads; the host proxy is the sole writer) and forward the resolved
        # ABSOLUTE path so it points at the mount regardless of a relative/env value.
        # Skipped when the dir is absent → the join no-ops and the run keeps static
        # pricing, exactly as a local run does when the log is missing.
        litellm_cost_log = os.environ.get("LITELLM_COST_LOG")
        if litellm_cost_log and "LITELLM_COST_LOG" in merged_allowlist and cfg.network != "none":
            abs_log = Path(litellm_cost_log).expanduser().resolve()
            if abs_log.parent.is_dir():
                argv += ["-v", f"{abs_log.parent}:{abs_log.parent}:ro", "--env", f"LITELLM_COST_LOG={abs_log}"]

        # Signal to in-container agents that the harness already provides OS-level
        # isolation. The Codex agent reads this to fall back to its full-access
        # sandbox: Codex's Landlock-backed read-only / workspace-write sandboxes
        # can't initialize inside a container and otherwise fail writes silently.
        argv += ["--env", "CODER_EVAL_IN_CONTAINER=1"]
        if self._isolation_active:
            argv += ["--env", "CODER_EVAL_AGENT_ISOLATION=1"]

        # Hard-disable telemetry INSIDE the container. The app ships a baked-in
        # default connection string, so without this the in-container orchestrator
        # would emit CoderEval.Task.End — and the host re-emits the same event after
        # the container result is parsed (orchestration/batch.py), double-counting
        # every docker-driver task. The invariant is "container silent, host emits
        # once"; this restores it regardless of the host's own telemetry setting.
        # Explicit value (not name-only) so it overrides any inherited/baked value.
        argv += ["--env", "TELEMETRY_ENABLED=false"]

        argv += ["-v", f"{input_dir.resolve()}:{CONTAINER_INPUT_DIR}:ro"]
        # Mount the host run_dir to the container's standard output location
        # so the in-container Orchestrator writes task.json/task.log/etc.
        # directly to the host filesystem via bind-mount.
        argv += ["-v", f"{output_dir}:{CONTAINER_OUTPUT_DIR}"]
        # Mount the original task dir at the SAME host path so the
        # in-container Orchestrator can set TASK_DIR (used by run_command
        # criteria via `$TASK_DIR/foo.json`) to a path that resolves
        # identically inside and outside the container.
        host_task_dir: Path | None = None
        if self.rt.task_file:
            host_task_dir = self.rt.task_file.parent.resolve()
            argv += ["-v", f"{host_task_dir}:{CONTAINER_TASK_DIR}:ro"]
        # Forward the host's Claude Code OAuth state so the in-container CLI
        # inherits the same login as the host. We mount a *throwaway lean copy*
        # of ~/.claude (made by _prepare_host_mounts) read-WRITE at the host's
        # ~/.claude path — HOME is forwarded, so the path is symmetric inside
        # the container. The container can therefore write anywhere under
        # ~/.claude (settings, session ephemera, cache) without ever mutating
        # the host's real ~/.claude. _claude_mount_src is None when ~/.claude
        # doesn't exist or the mount is opted out (CODER_EVAL_NO_CLAUDE_MOUNT=1).
        if self._claude_mount_src is not None:
            host_claude_dir = Path.home() / ".claude"
            claude_target = Path(AGENT_HOME) / ".claude" if self._isolation_active else host_claude_dir
            argv += ["-v", f"{self._claude_mount_src}:{claude_target}"]

        for source, target in self._private_source_mounts:
            argv += ["-v", f"{source.resolve()}:{target}:ro"]

        # Auto-mount host paths the task references so they resolve inside
        # the container at the *same* path they have on the host.
        # Includes:
        #   - Claude Code plugin dirs (`agent.plugins[].path`)
        #   - Template directories (`sandbox.template_sources[].path` for
        #     TemplateDirSource entries -- already absolute after
        #     resolve_template_paths runs on the host).
        # Reference files (`task.reference.file`) and `run_command`
        # criteria that use `$TASK_DIR/...` are covered by the symmetric
        # task_dir mount above. ``mounted`` dedupes overlapping entries.
        mounted: set[Path] = set()
        # Auto-mount sources that look like credential / secret dirs get a
        # loud warning. Task YAMLs typically come from in-house suite authors,
        # but the `plugin.path` / `reference.directory` / `template_sources`
        # fields are user-controlled strings, and a typo (or a hostile suite)
        # can silently expose `~/.ssh` etc. Warning, not hard fail, because
        # legitimate uses exist (a task that does in fact want to read
        # `~/.aws/config`). The warning surfaces the surprise.
        sensitive_sources = self._sensitive_source_paths()

        def _auto_mount(raw_path: str | None, *, dir_only: bool = True) -> None:
            if not raw_path:
                return
            resolved = Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()
            # File paths get mounted as the parent dir so a single -v covers
            # the file; container-side reads still resolve at the same path.
            target = resolved if (dir_only or resolved.is_dir()) else resolved.parent
            if target in mounted or not target.is_dir():
                return
            for sensitive in sensitive_sources:
                if target == sensitive or sensitive in target.parents:
                    logger.warning(
                        "Auto-mounting sensitive host path %s into container; fix task YAML if unintended.",
                        target,
                    )
                    break
            mounted.add(target)
            argv.extend(["-v", f"{target}:{target}:ro"])

        plugins = (self.rt.task.agent.plugins if self.rt.task.agent else None) or []
        for plugin in plugins:
            _auto_mount(plugin.get("path") if isinstance(plugin, dict) else None)

        from coder_eval.models import TemplateDirSource

        sandbox_cfg = self.rt.task.sandbox
        if not self._isolation_active:
            for source in (sandbox_cfg.template_sources or []) if sandbox_cfg else []:
                if isinstance(source, TemplateDirSource):
                    _auto_mount(source.path)

        # Defensive: system_prompt_file is normally inlined into
        # system_prompt by load_task / experiment resolution, but a variant
        # could conceivably inject an absolute path that survives. Cover
        # that path so the in-container Orchestrator can read it.
        agent_cfg = self.rt.task.agent
        if not self._isolation_active and agent_cfg and agent_cfg.system_prompt_file:
            _auto_mount(agent_cfg.system_prompt_file, dir_only=False)

        # reference.file / reference.directory: if a task ships absolute
        # paths (or relative paths that escape the task_dir mount via
        # ``..``), they must be mounted explicitly. Relative paths under
        # task_dir are already covered by the symmetric task_dir mount.
        reference = self.rt.task.reference
        if not self._isolation_active and reference is not None:
            _auto_mount(reference.file, dir_only=False)
            _auto_mount(reference.directory)
        for mount in cfg.extra_mounts:
            normalized = _validate_extra_mount(mount)
            argv += ["-v", normalized]

        # Docker WORKDIR alignment: run the agent at the image's own WORKDIR. Set
        # the container's initial cwd via `-w` (the in-container orchestrator also
        # runs the agent there). NO bind mount targets it -- capture is a copy-out
        # (see Orchestrator._cleanup), not a mount, so baked inputs/HOME survive.
        if self._workspace_dir is not None:
            # Under isolation the workspace is the framework's OWN agent dir
            # (assigned above, not task-authored), and that path is deliberately
            # in RESERVED_CONTAINER_DIRS. The assertion guards task/image-supplied
            # values, so exempt exactly the isolation-managed constant.
            if not (self._isolation_active and self._workspace_dir == CONTAINER_AGENT_WORK_DIR):
                _assert_workspace_not_reserved(self._workspace_dir)
            argv += ["-w", self._workspace_dir]

        argv += [image]
        # Pass the container-side output path (the input/output are bound at
        # container-side defaults, so we just use those).
        if self.verbose:
            argv += ["-v"]
        argv += ["--output", str(CONTAINER_OUTPUT_DIR)]
        if host_task_dir is not None:
            argv += ["--task-dir", str(CONTAINER_TASK_DIR)]
        return argv


def build_error_result(
    rt: ResolvedTask, exc: BaseException, *, status: FinalStatus = FinalStatus.ERROR
) -> EvaluationResult:
    """Synthesize an error-status EvaluationResult for a Docker-runner failure.

    Mirrors the shape produced by ``_create_error_task_result`` in
    ``orchestration.batch`` so downstream reporting code doesn't have to
    special-case Docker failures. ``status`` lets the caller distinguish a
    failed image build (:data:`FinalStatus.BUILD_FAILED`) from a generic ERROR;
    for a build failure the full build log is carried into ``error_log_tail``.
    """
    build_log = getattr(exc, "build_log", "") or ""
    description = (
        "Docker image build failed"
        if status == FinalStatus.BUILD_FAILED
        else f"Docker run failed: {type(exc).__name__}"
    )
    return EvaluationResult(
        task_id=rt.task.task_id,
        task_description=description,
        variant_id=rt.variant_id,
        agent_type=AgentKind.UNKNOWN,
        started_at=datetime.now(),
        final_status=status,
        error_message=str(exc),
        # Match the orchestrator's task.log tail ceiling; docker.log keeps the
        # full unbounded build output regardless.
        error_log_tail=build_log[-DEFAULT_LOG_TAIL_MAX_BYTES:] if build_log else None,
        iteration_count=0,
        environment_info={},
    )
