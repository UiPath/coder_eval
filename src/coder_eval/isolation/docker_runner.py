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
    CONTAINER_GRADE_WORKSPACE,
    CONTAINER_INPUT_DIR,
    CONTAINER_OUTPUT_DIR,
    CONTAINER_REFERENCE_DIR,
    CONTAINER_TASK_DIR,
    CONTAINER_WORK_DIR,
    IN_CONTAINER_ENV,
    RESERVED_CONTAINER_DIRS,
    AgentKind,
    DockerDriverConfig,
    EvaluationResult,
    FinalStatus,
    PreservationMode,
    ResourceLimits,
)
from coder_eval.orchestration.evaluation import resolve_host_reference_dir
from coder_eval.orchestration.plugin_staging import PluginStagingError, resolve_plugin_path, scan_plugin_skills
from coder_eval.path_utils import (
    DOCKER_LOG_FILENAME,
    PRIOR_RESULT_FILENAME,
    REFERENCE_COPY_IGNORE,
    TASK_JSON_FILENAME,
    ignore_patterns_and_symlinks,
    rmtree_restrictive,
    write_text_atomic,
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

# MUST equal the `COPY` destination in docker/Dockerfile (drift-guarded by a test).
# Rationale: .claude/notes/isolation.md § The entrypoint and the image contract
CONTAINER_ENTRYPOINT = "/usr/local/bin/coder_eval_entrypoint.sh"

# Docker Desktop's stable host alias from a bridge-network container. Auto-resolves
# on macOS/Windows; on Linux it must be published via `--add-host`.
_DOCKER_HOST_ALIAS = "host.docker.internal"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


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


# DENYLIST of top-level entries the per-task RW copy of ~/.claude skips. Matched by
# basename at every level, so anything unlisted (settings.json, .credentials.json,
# plugins/) is copied through.
# Rationale: .claude/notes/isolation.md § The lean ~/.claude copy
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

# The live host dir is rewritten while we walk it, so a file can vanish mid-copy.
# Rationale: .claude/notes/isolation.md § The lean ~/.claude copy
CLAUDE_COPY_MAX_ATTEMPTS = 3

# The runner touches this file while alive; the in-container watchdog exits if it
# goes stale. Lives in the output dir, which is bind-mounted into the container.
# Rationale: .claude/notes/isolation.md § The heartbeat watchdog is armed only inside a container
HEARTBEAT_FILENAME = ".coder_eval_host_heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 2.0
HEARTBEAT_STALE_SECONDS = 20

# asyncio's StreamReader caps a line at 64 KiB by default, which a single stream
# event can exceed. The same KIND of guard as Orchestrator._POST_RUN_STREAM_LIMIT,
# but deliberately far larger (64 MiB vs 256 KiB) — do not unify them downward: a
# whole-file tool input on this stream tore the container down before task.json.
# Rationale: .claude/notes/isolation.md § The stdout line limit
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
        # Image absent locally or inspect failed: let `docker run` raise the canonical
        # error. Suppressed rather than raised because argv-logging paths reach here
        # even when the image is fine, and would then double-fail.
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


_CONTAINER_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_.-]")

# A leading Windows drive letter. Bare ``C:`` is deliberately not matched.
# Rationale: .claude/notes/isolation.md § Extra mounts and reserved destinations
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")


def _sanitize_container_name_component(s: str) -> str:
    """Strip characters Docker rejects in `--name` so dataset row IDs work.

    Suite/row tasks have ids like ``suite_id/row_id``; ``/`` is invalid in
    Docker names. Anything outside ``[a-zA-Z0-9_.-]`` collapses to ``_``.
    """
    return _CONTAINER_NAME_INVALID.sub("_", s)


# Single source of truth in models.container_paths; extra-mount destinations and
# WORKDIR both reject these.
_RESERVED_MOUNT_DESTS = RESERVED_CONTAINER_DIRS


def _validate_extra_mount(spec: str) -> str:
    """Sanity-check a ``-v`` mount spec and return a normalized form.

    Defends against typos that would silently expose the host fs to the
    container, and against mount specs that shadow framework-owned mounts.
    Normalizes BOTH sides by expanding ``~`` and ``$VAR`` so authors can
    write portable specs. Returns the (possibly rewritten) spec to feed
    back into argv.

    Notes:
      - Destinations are expanded too. A container path that has to match a
        host-valued var (``$SKILLS_REPO_PATH``) would otherwise have to be
        hardcoded per machine.
      - Mode is REQUIRED. Forgetting ``:ro`` is the single most common way
        to accidentally hand the container RW access to a host directory,
        so we make the author write it explicitly.
      - Destinations colliding with framework mounts (``/work``, ``/``,
        etc.) are rejected outright.
    """
    # The container side is always POSIX, so only the source can carry a drive letter.
    if _DRIVE_PREFIX.match(spec):
        head, body = spec[:2], spec[2:]
    else:
        head, body = "", spec
    parts = body.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: expected `src:dst[:ro|rw]`.")
    src, raw_dst = head + parts[0], parts[1]
    # Default read-only: mounting host paths RW by default is the wrong sandbox stance.
    mode = parts[2] if len(parts) == 3 else "ro"
    if not src:
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: empty source path.")
    if not raw_dst:
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: empty destination path.")
    # Expanded before the absolute-path check: that is the point.
    expanded_src = os.path.expandvars(os.path.expanduser(src))
    dst = os.path.expandvars(os.path.expanduser(raw_dst))
    # HAZARD: a variable expanding to a ':' would add fields to the rebuilt spec,
    # silently moving the destination or widening the mode.
    if ":" in dst or ":" in expanded_src[len(head) :]:
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: expansion introduced a ':' into a path.")
    if not dst.startswith("/"):
        # expandvars leaves an unset var verbatim, so typos land here.
        detail = f"{raw_dst!r}" if dst == raw_dst else f"{raw_dst!r} (expanded to {dst!r})"
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: destination must be an absolute path, got {detail}.")
    if mode not in ("ro", "rw"):
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: mode must be 'ro' or 'rw'.")
    if not Path(expanded_src).exists():
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: source path does not exist on host.")
    # Checked in EXPANDED form: a var could itself expand to a reserved path, and a
    # ``/work/...`` destination shadows the input/output tree.
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
                # AS symlinks, not followed: a self-referential marketplace link
                # makes a following walk recurse infinitely.
                # Rationale: .claude/notes/isolation.md § The lean ~/.claude copy
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


def grant_container_access(root: Path, *, writable: bool) -> list[tuple[Path, int]]:
    """Widen ``root`` (recursively) so the container can reach it without DAC caps.

    COUNTERPART to the ``--cap-drop DAC_OVERRIDE --cap-drop DAC_READ_SEARCH`` in
    :meth:`DockerRunner._build_argv`: the container is root but owns no
    framework-owned mount, so every access it makes is an "other" access. Semantics
    match ``chmod -R o+rwX`` (``o+rX`` when ``writable=False``). ``writable=False``
    is load-bearing, not cosmetic -- it keeps ``/work/references`` off the list of
    things the agent can overwrite.

    Returns ``(path, original_mode)`` for every entry it changed, so a caller that
    widened a tree it does not own can put it back (see :func:`restore_modes`).
    No-op on Windows.

    Rationale: .claude/notes/isolation.md § grant_container_access
    """
    widened_paths: list[tuple[Path, int]] = []
    if os.name == "nt":  # pragma: no cover - POSIX mode bits are meaningless here
        return widened_paths
    extra = 0o006 if writable else 0o004
    for path in (root, *root.rglob("*")):
        # HAZARD: chmod follows symlinks, so widening one would re-mode its target --
        # for the ~/.claude copy, an arbitrary path outside the staging tree.
        if path.is_symlink():
            continue
        try:
            mode = path.lstat().st_mode & 0o7777
        except OSError:  # pragma: no cover - raced away mid-walk; nothing to widen
            continue
        widened = mode | extra
        if path.is_dir() or mode & 0o100:
            widened |= 0o001
        if widened != mode:
            os.chmod(path, widened)
            widened_paths.append((path, mode))
    return widened_paths


def restore_modes(widened: list[tuple[Path, int]]) -> None:
    """Put back the modes :func:`grant_container_access` widened.

    Needed for exactly one mount, and the asymmetry is the point. ``input_dir``
    and ``output_dir`` are staging directories the harness created for this one
    dispatch and deletes afterwards, so widening them is scoped to their whole
    lifetime. The GRADED WORKSPACE is neither: with ``--workspace`` it is an
    arbitrary operator directory, and otherwise it is the run's preserved
    ``artifacts/`` tree that outlives the grade. Leaving those world-writable
    means any other local uid on a shared or CI host can afterwards rewrite the
    artifacts a criterion reads -- i.e. change the verdict -- or plant an
    executable in the tree.

    Best-effort and never raises: this runs in a ``finally`` beside the staging
    cleanup, and a failed restore must not mask the container's own outcome.
    """
    for path, mode in reversed(widened):
        try:
            if not path.is_symlink():
                os.chmod(path, mode)
        except OSError as exc:  # pragma: no cover - raced away or removed by the container
            logger.warning("Could not restore mode on %s: %s", path, exc)


def _quarantine_record(task_json: Path | None, suffix: str, label: str) -> None:
    """Move a refused container record aside, best-effort.

    Shared by both version-skew refusals (`_assert_grade_honored`,
    `_assert_regrade_honored`), which had the same seven lines twice and differed
    only in the suffix and the wording. Refusing in memory while leaving
    contradictory bytes in the bind-mounted run dir is not a refusal -- a later
    `aggregate` would publish exactly the row the guard declined -- so this must
    behave identically on both paths, which one copy per caller cannot promise.

    Never masks the caller's raise: a failed move is logged and swallowed.
    """
    if task_json is None:
        return
    sidecar = task_json.with_suffix(task_json.suffix + suffix)
    try:
        os.replace(task_json, sidecar)  # atomic; overwrites any stale prior sidecar
        logger.warning("Quarantined the refused %s record to %s", label, sidecar)
    except OSError as exc:
        logger.warning("Could not quarantine %s: %s", task_json, exc)


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
        grade: bool = True,
        prior_result: EvaluationResult | None = None,
        grade_workspace: Path | None = None,
    ) -> None:
        self.rt = rt
        self.preservation_mode = preservation_mode
        self.stream_callback = stream_callback
        self.verbose = verbose
        # DETACHED GRADE. Both set together or neither: `prior_result` is the
        # already-executed row the in-container Orchestrator seeds from, and
        # `grade_workspace` is the host directory that run left behind, mounted at
        # CONTAINER_GRADE_WORKSPACE and ADOPTED rather than recreated.
        # Rationale: .claude/notes/isolation.md § Grading a docker row inside a container
        self.prior_result = prior_result
        self.grade_workspace = grade_workspace
        if (prior_result is None) != (grade_workspace is None):
            raise ValueError("prior_result and grade_workspace must be passed together")
        # Forwarded via context.json: a run-level decision by the CLI, not
        # recoverable from the staged task.yaml on the other side.
        self.grade = grade
        # Set by _prepare_host_mounts: the lean RW copy of ~/.claude. None when
        # there is none to forward or CODER_EVAL_NO_CLAUDE_MOUNT is set.
        self._claude_mount_src: Path | None = None
        # A throwaway COPY of the reference, mounted read-WRITE. Both are
        # load-bearing -- see _prepare_reference_mount.
        self._reference_mount_src: Path | None = None
        # Host path the copy came from, cached by _prepare_reference_mount so the
        # argv builder doesn't re-stat it (and re-emit its warning).
        self._reference_source_dir: Path | None = None
        # A throwaway COPY of the task dir, mounted read-WRITE so the agent-turn
        # window can chmod it. None when the task has no task_file.
        self._task_dir_mount_src: Path | None = None
        # Resolved in run() (needs the built image for "auto"). Concrete WORKDIR the
        # agent runs at + copies out from; None = standard artifacts workspace.
        self._workspace_dir: str | None = None

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
        # Side-effecting, so it runs in a worker thread like the other docker calls.
        try:
            image = await asyncio.to_thread(self._build_image)
        except DockerBuildError as exc:
            # The build precedes run_dir/docker.log and task.json, so persist the
            # log and a BUILD_FAILED record before re-raising.
            # Rationale: .claude/notes/isolation.md § A container that produced no task.json
            await self._record_build_failure(exc)
            raise
        # The version-label preflight only makes sense for the framework image;
        # a task-supplied Dockerfile won't carry the org.coder-eval.version label.
        if not self._docker_config.dockerfile_path:
            await asyncio.to_thread(_preflight_image_version, image)
        await asyncio.to_thread(self.rt.run_dir.mkdir, parents=True, exist_ok=True)

        # Docker WORKDIR alignment: config value / "auto" -> inspect / fallback /root.
        # None keeps the standard artifacts workspace.
        self._workspace_dir = await asyncio.to_thread(_resolve_workspace_dir, self._docker_config.working_dir, image)

        # Stage only the inputs. The OUTPUT dir is the host's run_dir itself,
        # bind-mounted at the same path inside the container, so paths are symmetric.
        # Sanitize task_id: dataset ids are ``suite_id/row_id`` and ``/`` breaks mkdtemp.
        safe_staging_id = _sanitize_container_name_component(self.rt.task.task_id)
        staging = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=f"coder_eval_docker_{safe_staging_id}_"))
        input_dir = staging / "input"
        await asyncio.to_thread(input_dir.mkdir)
        output_dir = self.rt.run_dir.resolve()
        # Bound BEFORE the try: the `finally` restores it, and `_stage_inputs`
        # can raise before the widening happens.
        widened_workspace: list[tuple[Path, int]] = []

        try:
            await self._stage_inputs(input_dir)

            # Stable and UNIQUE so cancellation can target it: PID alone collides
            # under --max-parallel >1. Sanitized and truncated -- dataset row ids break
            # docker name validation, and a 30-char cap collided on shared prefixes.
            short_uuid = uuid.uuid4().hex[:8]
            safe_task_id = _sanitize_container_name_component(self.rt.task.task_id)[:80]
            container_name = f"coder-eval-{safe_task_id}-r{self.rt.replicate_index}-{os.getpid()}-{short_uuid}"
            # Side-effecting prep _build_argv must NOT do: argv rendering stays pure
            # so it is testable without a docker daemon. Cleaned up with `staging`.
            await asyncio.to_thread(self._prepare_host_mounts, staging)
            await asyncio.to_thread(self._prepare_reference_mount, staging)
            await asyncio.to_thread(self._prepare_task_dir_mount, staging)
            # AFTER staging, BEFORE the container starts: the DAC caps are dropped, so
            # every framework-owned mount must be reachable through its `other` bits.
            # Rationale: .claude/notes/isolation.md § grant_container_access
            await asyncio.to_thread(grant_container_access, input_dir, writable=False)
            await asyncio.to_thread(grant_container_access, output_dir, writable=True)
            if self.grade_workspace is not None:
                # The one mount whose files the harness did NOT create, so the owner
                # bits cannot be assumed -- and the one that SURVIVES the dispatch,
                # which is why it is recorded and restored in the `finally` below.
                widened_workspace = await asyncio.to_thread(grant_container_access, self.grade_workspace, writable=True)
            argv = self._build_argv(input_dir, output_dir, container_name=container_name, image=image)
            logger.info("Running task '%s' in docker: %s", self.rt.task.task_id, " ".join(argv))
            # Prime the heartbeat before the container starts so the watchdog never sees an initial stale state.
            heartbeat_path = output_dir / HEARTBEAT_FILENAME
            await asyncio.to_thread(heartbeat_path.touch)
            heartbeat_task = asyncio.create_task(_heartbeat_loop(heartbeat_path))
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=STDOUT_LINE_LIMIT_BYTES,
            )
            log_path = self.rt.run_dir / DOCKER_LOG_FILENAME
            log_fh = await asyncio.to_thread(log_path.open, "w", encoding="utf-8")
            # HAZARD: `docker run --rm` does NOT propagate a kill daemon-side, so
            # without this `finally` Ctrl-C leaves the container burning budget.
            # Rationale: .claude/notes/isolation.md § A container that produced no task.json
            try:
                returncode = await self._stream_container_output(proc, log_fh)
            finally:
                heartbeat_task.cancel()
                # Narrowed so a genuine KeyboardInterrupt / SystemExit from a parallel sibling still propagates.
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                await asyncio.to_thread(log_fh.close)
                # Cancelled mid-flight: kill the container AND the docker CLI subprocess, best-effort.
                if proc.returncode is None:
                    await self._kill_container(proc, container_name)

            return await self._parse_result_or_raise(output_dir, returncode, log_path)
        finally:
            # rmtree_restrictive, not ignore_errors: `staging` holds the references
            # copy, which a container killed mid-turn leaves at mode 000.
            # Rationale: .claude/notes/isolation.md § Why the framework mounts are writable copies
            await asyncio.to_thread(rmtree_restrictive, staging)
            # The graded workspace is the caller's tree, not ours; give it back
            # the modes it had. See `restore_modes`.
            await asyncio.to_thread(restore_modes, widened_workspace)

    async def _stage_inputs(self, input_dir: Path) -> None:
        """Serialise the post-override TaskDefinition + lineage/variant context into the
        staging ``input_dir`` (``task.yaml`` + ``context.json``). Pure I/O off the event
        loop; no control-flow change.
        """
        # POST-override, not rt.source_yaml: _apply_cli_overrides has since mutated
        # rt.task in-memory and the container must see those mutations.
        # Rationale: .claude/notes/isolation.md § The context payload is untrusted input
        task_yaml_in = input_dir / "task.yaml"

        def _dump_task_yaml() -> str:
            payload = self.rt.task.model_dump(mode="json")
            if self.rt.task.agent is not None:
                # Only the fields a layer wrote: the reloaded task must not claim a
                # model default (e.g. permission_mode) the harness contract rejects.
                payload["agent"] = self.rt.task.agent.model_dump(mode="json", exclude_unset=True)
                # The absolute host path the plugin is auto-mounted at: the container
                # cwd would otherwise resolve a relative path somewhere else. Kept as
                # authored when it does not resolve here: a detached grade on another
                # host never uses the plugin, and a run fails its own resolution check.
                for plugin in payload["agent"].get("plugins") or []:
                    with contextlib.suppress(PluginStagingError):
                        plugin["path"] = str(resolve_plugin_path(plugin["path"]))
            return yaml.safe_dump(payload, sort_keys=False)

        task_yaml_text = await asyncio.to_thread(_dump_task_yaml)
        await asyncio.to_thread(task_yaml_in.write_text, task_yaml_text, encoding="utf-8")
        # Lineage + variant metadata so the in-container Orchestrator reconstructs
        # the same context (variant_id is load-bearing for report grouping).
        context_payload = json.dumps(
            {
                "variant_id": self.rt.variant_id,
                "replicate_index": self.rt.replicate_index,
                "config_lineage": {k: v.model_dump(mode="json") for k, v in self.rt.config_lineage.items()},
                "preservation_mode": self.preservation_mode.value,
                # `coder-eval run` vs `coder-eval execute`. Not derivable from
                # task.yaml on the container side (deliberately not a task field).
                "grade": self.grade,
                # A detached grade: seed from prior.json and adopt
                # CONTAINER_GRADE_WORKSPACE instead of running an agent.
                "regrade": self.prior_result is not None,
                "source_yaml": self.rt.source_yaml,
                # The HOST's path, recorded verbatim into task.json's audit trail --
                # distinct from the container path TASK_DIR resolves against.
                # Rationale: .claude/notes/orchestration.md § Recording the task as authored
                "host_task_file": str(self.rt.task_file) if self.rt.task_file else None,
                # Docker WORKDIR alignment: concrete path the in-container
                # orchestrator runs at + captures out (None = standard workspace).
                "workspace_dir": self._workspace_dir,
            }
        )
        await asyncio.to_thread((input_dir / "context.json").write_text, context_payload, encoding="utf-8")
        if self.prior_result is not None:
            # Carried in whole, so the trajectory an `llm_judge` or
            # `command_executed` criterion reads is the ORIGINAL run's.
            await asyncio.to_thread(
                (input_dir / PRIOR_RESULT_FILENAME).write_text,
                self.prior_result.model_dump_json(indent=2),
                encoding="utf-8",
            )

    async def _stream_container_output(self, proc: asyncio.subprocess.Process, log_fh: TextIO) -> int:
        """Stream the container's stdout, returning its exit code.

        Wire-format lines emit to the host ``StreamCallback``; plain lines are written
        to ``docker.log``. A single over-limit line is dropped (degrade, not die) — the
        ``readline`` ``ValueError`` resyncs at the next newline. Runs as the inner-``try``
        body of ``run``; the caller owns the ``finally`` cleanup, so this helper never
        touches the heartbeat/log-fh/container teardown.
        """
        assert proc.stdout is not None
        # Explicit readline loop (not `async for`) so a single over-limit line
        # degrades to a dropped line instead of tearing the task down.
        # Rationale: .claude/notes/isolation.md § The stdout line limit
        while True:
            try:
                raw_line = await proc.stdout.readline()
            except ValueError:
                # readline() drains the offending bytes and resyncs at the next
                # newline. task.json crosses via the bind mount, not stdout.
                logger.warning(
                    "Dropped a stdout line over %d bytes from task %r's container; continuing to stream.",
                    STDOUT_LINE_LIMIT_BYTES,
                    self.rt.task.task_id,
                )
                continue
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            # Three-way split: wire-prefixed and parses -> the host StreamCallback
            # (canonical destination, not echoed to docker.log); prefixed but
            # unparseable -> wire bug, preserved raw so it is not lost; no prefix ->
            # plain log line.
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
                # Usually the container was already gone (race with --rm) or the
                # daemon refused. Surface stderr so the ambiguity is debuggable.
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
        # HAZARD: narrow to CancelledError -- a generic BaseException catch here
        # eats KeyboardInterrupt / SystemExit propagation from parallel tasks.
        with contextlib.suppress(asyncio.CancelledError):
            await proc.wait()

    async def _parse_result_or_raise(self, output_dir: Path, returncode: int, log_path: Path) -> EvaluationResult:
        """Read back ``task.json`` (the only artifact crossing the boundary) and parse it.

        If the container exited without producing it, persist a synthetic ERROR
        task.json and raise ``DockerRunError`` so the batch dispatcher records the
        failure as an ERROR-status result.
        """
        task_json = output_dir / TASK_JSON_FILENAME
        if not await asyncio.to_thread(task_json.exists):
            # Persist a synthetic ERROR task.json so the row stays visible instead of
            # vanishing -- the batch layer's skeleton never reaches the per-task dir.
            # Rationale: .claude/notes/isolation.md § A container that produced no task.json
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
            # Present but unparseable (schema skew from a stale image, a torn
            # write): degrade like the missing-file branch.
            raise await self._handle_malformed_task_json(task_json, log_path, exc) from exc
        self._warn_on_version_mismatch(result)
        self._assert_grade_honored(result, task_json)
        self._assert_regrade_honored(result, task_json)
        return result

    def _assert_regrade_honored(self, result: EvaluationResult, task_json: Path | None = None) -> None:
        """Fail loudly when a detached GRADE came back as a fresh agent run.

        ``regrade`` crosses the boundary only through ``context.json``; an image
        that predates container-side grading ignores it and falls through to the
        ordinary ``Orchestrator`` branch -- which **starts an agent**. Nothing else
        catches it: ``_assert_grade_honored`` early-returns because a grading
        container is dispatched with ``grade=True``.

        Keyed on EVIDENCE: a container that honored the request seeds from
        ``prior`` and never runs the agent, so a DIFFERENT ``started_at`` is the
        tell.

        Rationale: .claude/notes/isolation.md § The two honored-request guards
        """
        if self.prior_result is None:
            return
        if result.started_at == self.prior_result.started_at:
            return
        _quarantine_record(task_json, ".rerun", "re-run")
        raise DockerRunError(
            "Grading asked the container to score an already-executed run, but it returned a "
            + f"different trajectory (started_at {result.started_at} vs the recorded "
            + f"{self.prior_result.started_at}). The runtime image predates container-side "
            + "grading and re-ran the agent instead; rebuild or pull a matching agent image, "
            + "or grade on the host with --allow-host-grading."
        )

    def _assert_grade_honored(self, result: EvaluationResult, task_json: Path | None = None) -> None:
        """Fail loudly when `execute` came back with a graded verdict.

        ``grade`` crosses the boundary only through ``context.json``. An image that
        predates ``execute`` ignores the unknown key and grades anyway, and the
        image-version preflight only warns -- so version skew would change what a
        command MEANS.

        ``task_json`` is the on-disk record, quarantined before the raise: refusing
        in memory while leaving contradictory bytes on disk is not a refusal.

        Rationale: .claude/notes/isolation.md § The two honored-request guards
        """
        if self.grade:
            return
        # Keyed on EVIDENCE, not on the label: the question is not "what status is this" but "did it grade".
        graded_anyway = bool(result.success_criteria_results) or result.weighted_score is not None
        if not graded_anyway and (
            result.final_status.is_execution_fact or result.final_status is FinalStatus.NOT_GRADED
        ):
            return
        _quarantine_record(task_json, ".graded", "graded")
        raise DockerRunError(
            "`coder-eval execute` asked the container not to grade, but it returned "
            + f"{result.final_status.value} with {len(result.success_criteria_results)} criterion "
            + "result(s). The runtime image predates `execute` and ignored the request; "
            + "rebuild or pull a matching agent image."
        )

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
            log_path = self.rt.run_dir / DOCKER_LOG_FILENAME
            await asyncio.to_thread(log_path.write_text, exc.build_log or str(exc), encoding="utf-8")
            await self._write_synthetic_task_json(
                self.rt.run_dir / TASK_JSON_FILENAME, exc, status=FinalStatus.BUILD_FAILED
            )
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
            # HAZARD: through `write_text_atomic` like every other writer of this
            # file. A hand-rolled `Path.write_text` FOLLOWS symlinks, and a run
            # directory is bind-mounted writable into the agent's own container.
            # Rationale: .claude/notes/isolation.md § A container that produced no task.json
            write_text_atomic(target, result.model_dump_json(indent=2))

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
        # Writable: the CLI rewrites settings and state in place, and ~/.claude is
        # routinely 0700/0600 -- unreadable to the container without DAC_OVERRIDE.
        # Rationale: .claude/notes/isolation.md § grant_container_access
        grant_container_access(claude_copy, writable=True)
        self._claude_mount_src = claude_copy

    def _prepare_task_dir_mount(self, staging: Path) -> None:
        """Copy the task directory under ``staging`` for a read-WRITE mount.

        The in-container orchestrator holds this path at mode 000 for the duration
        of every agent turn, which neither a ``:ro`` mount (EROFS) nor an uncopied
        read-write mount (it would chmod the operator's real ``tasks/`` tree)
        survives. Lives under ``staging``, which ``run()`` removes in its
        ``finally``; one container per task means no cross-task interference.

        Rationale: .claude/notes/isolation.md § Why the framework mounts are writable copies
        """
        if not self.rt.task_file:
            return
        source = self.rt.task_file.parent.resolve()
        if not source.is_dir():
            return
        task_dir_copy = staging / "task_dir"
        shutil.copytree(source, task_dir_copy, ignore=ignore_patterns_and_symlinks(REFERENCE_COPY_IGNORE))
        # Read-only like the reference copy: criteria read fixtures here, nothing legitimately writes them.
        grant_container_access(task_dir_copy, writable=False)
        self._task_dir_mount_src = task_dir_copy

    def _prepare_reference_mount(self, staging: Path) -> None:
        """Copy the reference solution under ``staging`` for a read-WRITE mount.

        Both properties are load-bearing for the anti-cheat window:

        * **A copy**, so the container can chmod it without touching the user's
          checked-out ``tasks/`` tree.
        * **Writable**, because the in-container orchestrator holds this exact
          directory at mode 000 for the duration of every agent turn, and
          ``chmod`` on a ``:ro`` bind mount fails with EROFS. Mounting the real
          reference read-only instead leaves ``/work/references`` readable to the
          agent for the whole run -- which is precisely the leak
          ``tasks/anti_cheat_reference`` exists to catch.

        Lives under ``staging``, which ``run()`` removes in its ``finally``.
        """
        source = self._resolve_host_reference_dir()
        if source is None:
            return
        reference_copy = staging / "reference"
        shutil.copytree(source, reference_copy, ignore=ignore_patterns_and_symlinks(REFERENCE_COPY_IGNORE))
        # HAZARD: read-only on purpose -- withholding `o+w` keeps
        # _verify_reference_integrity from being the only thing between an agent
        # and a forged reference_comparison score.
        # Rationale: .claude/notes/isolation.md § grant_container_access
        grant_container_access(reference_copy, writable=False)
        self._reference_mount_src = reference_copy
        self._reference_source_dir = source

    def _build_image(self) -> str:
        """Resolve the image to run, building from a Dockerfile when configured.

        ``docker.dockerfile_path`` overrides ``docker.image``: the Dockerfile's
        parent directory is the build context (so relative ``COPY`` paths resolve)
        and the result is tagged deterministically per task so Docker's layer cache
        is reused. ``docker.build`` adds ``--build-arg`` / ``--secret`` / extra
        flags; BuildKit is enabled.

        Side-effecting (network + docker daemon state); call via
        ``asyncio.to_thread`` from :meth:`run`, never from :meth:`_build_argv`,
        which must stay pure.

        Rationale: .claude/notes/isolation.md § The entrypoint and the image contract

        Returns:
            The image reference to pass to ``docker run``.

        Raises:
            DockerRunError: If ``docker build`` exits non-zero, or the built image
                is not a coder-eval runtime image (missing the
                ``org.coder-eval.version`` label).
        """
        cfg = self._docker_config
        if not cfg.dockerfile_path:
            return cfg.image
        dockerfile = Path(cfg.dockerfile_path)
        context = dockerfile.parent
        # Lowercase: image repository names must be. Deterministic tag -> Docker
        # layer cache is reused across runs of the same task.
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

    def _resolve_host_reference_dir(self) -> Path | None:
        """Host path of ``task.reference.directory``, or None when unset/missing.

        Resolution goes through the shared
        ``orchestration.evaluation.resolve_host_reference_dir`` seam so the host
        mount and the orchestrator's own resolution cannot drift, but a missing
        directory is a WARNING here rather than an error: the host-side argv
        builder must not be the thing that fails the run. The in-container
        orchestrator hard-fails on the absent ``/work/references`` mount, and
        that error names this warning's cause so the operator is not sent
        chasing a stale image.
        """
        reference = self.rt.task.reference
        candidate = resolve_host_reference_dir(self.rt.task, self.rt.task_file)
        if reference is None or candidate is None:
            return None
        if not candidate.is_dir():
            logger.warning(
                "reference.directory %r does not resolve to a directory (%s); skipping the %s mount. "
                + "The task will fail in-container with a missing-mount error.",
                reference.directory,
                candidate,
                CONTAINER_REFERENCE_DIR,
            )
            return None
        return candidate

    def _reference_mount_args(self) -> list[str]:
        """Mount args that expose the reference to the harness but not to the agent.

        See the call site in ``_build_argv`` for the full rationale. Returns an
        empty list when the task declares no reference.

        No tmpfs mask any more. The mask existed because the task dir was mounted
        symmetrically and read-only, so a reference living inside it reached the
        agent as ``$TASK_DIR/<reference dir>`` and the only way to hide it was to
        layer an empty filesystem over that one subpath. The task dir is now a
        shielded copy (:meth:`_prepare_task_dir_mount`), so the embedded
        reference is already covered by that tree's own agent-turn window --
        along with the sibling-task reference directories the single-subpath mask
        never reached.
        """
        if self._reference_mount_src is None:
            return []
        # Read-WRITE, and of a COPY: the anti-cheat window chmods this path to 000
        # every turn, which a `:ro` mount rejects with EROFS.
        return ["-v", f"{self._reference_mount_src}:{CONTAINER_REFERENCE_DIR}"]

    def _plugin_mount_paths(self) -> list[str]:
        """Each plugin path as authored, plus each skill source outside every plugin root.

        Staging links a skill to its RESOLVED source, which can sit outside the root
        (a symlinked skill, a manifest ``skills: ../x``), so that source is mounted too.
        """
        plugins = (self.rt.task.agent.plugins if self.rt.task.agent else None) or []
        paths = [plugin["path"] for plugin in plugins]
        roots: list[Path] = []
        for raw in paths:
            with contextlib.suppress(PluginStagingError):
                roots.append(resolve_plugin_path(raw))
        with contextlib.suppress(PluginStagingError):
            skill_dirs = scan_plugin_skills(plugins).values() if plugins else ()
            paths += [str(d) for d in skill_dirs if not any(d.is_relative_to(root) for root in roots)]
        return paths

    def _build_argv(
        self, input_dir: Path, output_dir: Path, *, container_name: str, image: str | None = None
    ) -> list[str]:
        cfg = self._docker_config
        # _build_argv stays PURE -- no side effects -- so it remains testable without
        # a docker daemon. Fall back to the configured image when called directly.
        if image is None:
            image = cfg.image

        argv: list[str] = ["docker", "run", "--rm", "--name", container_name]

        # Pinned at run time, not trusted from the image: this survives a task
        # Dockerfile that sets or clears its own ENTRYPOINT/CMD.
        # Rationale: .claude/notes/isolation.md § The entrypoint and the image contract
        argv += ["--entrypoint", CONTAINER_ENTRYPOINT]

        # COUNTERPART, do not remove one without the other: dropping DAC_OVERRIDE
        # revokes root's bypass on every framework-owned mount, and
        # `grant_container_access` widens those host-side to compensate. Drop without
        # widening and every docker task dies on its first log write.
        # Rationale: .claude/notes/isolation.md § Capability drops and the anti-cheat window
        argv += [
            "--cap-drop",
            "DAC_OVERRIDE",
            "--cap-drop",
            "DAC_READ_SEARCH",
        ]

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

        # Explicit allowlist. `--env VAR` (name-only) tells docker to copy the value
        # from our env at run time, so secrets stay out of the argv we log.
        # Rationale: .claude/notes/isolation.md § Environment forwarding
        merged_allowlist = set(cfg.env_passthrough) | set(cfg.env_passthrough_extra)
        for env_var in merged_allowlist:
            # LITELLM_BASE_URL / LITELLM_COST_LOG are forwarded below with a value
            # rewrite (host alias / absolute mount path), not name-only.
            if env_var in ("LITELLM_BASE_URL", "LITELLM_COST_LOG"):
                continue
            if env_var in os.environ:
                argv += ["--env", env_var]

        # A bridge-network container cannot reach the host's loopback, so rewrite it
        # to the docker host alias and publish that alias. Only a URL, so an
        # explicit `--env VAR=value` is safe in the logged argv -- unlike the token.
        litellm_base_url = os.environ.get("LITELLM_BASE_URL")
        if litellm_base_url and "LITELLM_BASE_URL" in merged_allowlist and cfg.network != "none":
            rewritten = _rewrite_loopback_for_container(litellm_base_url)
            if rewritten is not None:
                argv += ["--env", f"LITELLM_BASE_URL={rewritten}", "--add-host", f"{_DOCKER_HOST_ALIAS}:host-gateway"]
            else:
                argv += ["--env", "LITELLM_BASE_URL"]

        # The proxy's per-call cost log: written on the HOST, READ by the
        # in-container cost join, so bind-mount its dir at the SAME host path
        # read-only and forward the resolved ABSOLUTE path.
        litellm_cost_log = os.environ.get("LITELLM_COST_LOG")
        if litellm_cost_log and "LITELLM_COST_LOG" in merged_allowlist and cfg.network != "none":
            abs_log = Path(litellm_cost_log).expanduser().resolve()
            if abs_log.parent.is_dir():
                argv += ["-v", f"{abs_log.parent}:{abs_log.parent}:ro", "--env", f"LITELLM_COST_LOG={abs_log}"]

        # Tells in-container agents the harness already provides OS-level isolation;
        # Codex reads it to fall back to its full-access sandbox.
        argv += ["--env", f"{IN_CONTAINER_ENV}=1"]

        # The invariant is "container silent, host emits once": the host re-emits
        # CoderEval.Task.End after parsing the result. Explicit value, not
        # name-only, so it overrides any inherited or baked-in value.
        # Rationale: .claude/notes/isolation.md § Environment forwarding
        argv += ["--env", "TELEMETRY_ENABLED=false"]

        argv += ["-v", f"{input_dir.resolve()}:{CONTAINER_INPUT_DIR}:ro"]
        # The host run_dir at the container's standard output location, so the
        # in-container Orchestrator writes straight to the host filesystem.
        argv += ["-v", f"{output_dir}:{CONTAINER_OUTPUT_DIR}"]
        # A COPY at a fixed container path, read-WRITE: see _prepare_task_dir_mount.
        if self._task_dir_mount_src is not None:
            argv += ["-v", f"{self._task_dir_mount_src}:{CONTAINER_TASK_DIR}"]

        # DETACHED GRADE: the already-executed workspace, read-WRITE and NOT a copy
        # -- criteria legitimately mutate what they grade, and _setup_template's
        # filtering would drop node_modules / dist / .venv from a copy.
        # Rationale: .claude/notes/isolation.md § Why the framework mounts are writable copies
        if self.grade_workspace is not None:
            argv += ["-v", f"{self.grade_workspace.resolve()}:{CONTAINER_GRADE_WORKSPACE}"]

        # ANTI-CHEAT: the reference normally lives INSIDE the task dir, and a
        # writable COPY is mounted at /work/references for the window to chmod.
        # There is NO tmpfs mask -- the task dir is itself a shielded copy now
        # (see _reference_mount_args), so the embedded original is covered by the
        # same window rather than hidden by a layered filesystem.
        # Rationale: .claude/notes/isolation.md § Why the framework mounts are writable copies
        argv += self._reference_mount_args()
        # A throwaway lean COPY of ~/.claude, read-WRITE at the host's own path
        # (HOME is forwarded, so the path is symmetric), so the container never
        # mutates the host's real one.
        # Rationale: .claude/notes/isolation.md § The lean ~/.claude copy
        if self._claude_mount_src is not None:
            host_claude_dir = Path.home() / ".claude"
            argv += ["-v", f"{self._claude_mount_src}:{host_claude_dir}"]

        # Host paths the task references (plugin dirs, resolved template dirs), at
        # the SAME path inside the container. The reference is deliberately NOT
        # here -- it has its own mount and is masked out of the task_dir mount.
        # ``mounted`` dedupes overlapping entries.
        mounted: set[Path] = set()
        # Warned, not refused: `plugin.path` / `reference.directory` /
        # `template_sources` are user-controlled strings, and legitimate uses exist.
        # Rationale: .claude/notes/isolation.md § Extra mounts and reserved destinations
        sensitive_sources = self._sensitive_source_paths()

        def _auto_mount(raw_path: str | None) -> None:
            if not raw_path:
                return
            target = Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()
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

        for plugin_path in self._plugin_mount_paths():
            _auto_mount(plugin_path)

        from coder_eval.models import TemplateDirSource

        sandbox_cfg = self.rt.task.sandbox
        for source in (sandbox_cfg.template_sources or []) if sandbox_cfg else []:
            if isinstance(source, TemplateDirSource):
                _auto_mount(source.path)

        # HAZARD: task.reference.directory is deliberately NOT auto-mounted at its
        # host path. That would bind the REAL tree in beside the shielded copy, so
        # the mode-000 window would leave it readable through $TASK_DIR.
        for mount in cfg.extra_mounts:
            normalized = _validate_extra_mount(mount)
            argv += ["-v", normalized]

        # Docker WORKDIR alignment: `-w` only. NO bind mount targets it -- capture
        # is a copy-out (see Orchestrator._cleanup), so baked inputs and HOME
        # survive.
        if self._workspace_dir is not None:
            _assert_workspace_not_reserved(self._workspace_dir)
            argv += ["-w", self._workspace_dir]

        argv += [image]
        # Pass the container-side output path (the input/output are bound at
        # container-side defaults, so we just use those).
        if self.verbose:
            argv += ["-v"]
        argv += ["--output", str(CONTAINER_OUTPUT_DIR)]
        if self._task_dir_mount_src is not None:
            # The container-side path: it only ever seeds TASK_DIR and is never
            # re-read, which is why the mount no longer has to be symmetric.
            argv += ["--task-dir", CONTAINER_TASK_DIR]
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
