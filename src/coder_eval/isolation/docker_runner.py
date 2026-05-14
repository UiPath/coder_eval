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
from typing import TYPE_CHECKING

import yaml

from coder_eval.models import (
    AgentKind,
    DockerDriverConfig,
    EvaluationResult,
    FinalStatus,
    ResourceLimits,
    SandboxConfig,
)
from coder_eval.streaming.callbacks import safe_emit
from coder_eval.streaming.wire import deserialize_event, has_prefix


if TYPE_CHECKING:
    from coder_eval.models import ResolvedTask
    from coder_eval.streaming.callbacks import StreamCallback


logger = logging.getLogger(__name__)


def default_image_tag() -> str:
    """Return the default ``coder-eval-agent`` image tag for this package version."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"coder-eval-agent:{version('coder-eval')}"
    except PackageNotFoundError:
        # Package not installed in current env (e.g. running from source without -e .).
        # Fall back to :latest so a manually-tagged image still resolves.
        logger.debug("coder-eval package not installed; defaulting image tag to :latest")
        return "coder-eval-agent:latest"


DEFAULT_IMAGE_TAG = default_image_tag()

# Container-side paths. Kept in lockstep with docker/entrypoint.sh.
CONTAINER_WORK_DIR = "/work"
CONTAINER_INPUT_DIR = "/work/input"
CONTAINER_OUTPUT_DIR = "/work/output"
CONTAINER_TASK_DIR = "/work/task_dir"

# Host-side heartbeat: the runner touches this file every HEARTBEAT_INTERVAL
# seconds while alive. The in-container watchdog exits if the file is stale
# (older than HEARTBEAT_STALE_SECONDS) -- our only defence against the host
# being SIGKILL'd (e.g. Claude Code's Escape) before the asyncio cleanup
# runs. Lives in the output dir, which is bind-mounted into the container.
HEARTBEAT_FILENAME = ".coder_eval_host_heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 2.0
HEARTBEAT_STALE_SECONDS = 20


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
                await asyncio.to_thread(heartbeat_path.write_text, str(counter))
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


_CONTAINER_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_.-]")


def _sanitize_container_name_component(s: str) -> str:
    """Strip characters Docker rejects in `--name` so dataset row IDs work.

    Suite/row tasks have ids like ``suite_id/row_id``; ``/`` is invalid in
    Docker names. Anything outside ``[a-zA-Z0-9_.-]`` collapses to ``_``.
    """
    return _CONTAINER_NAME_INVALID.sub("_", s)


# Destinations that would shadow framework-owned mounts inside the container.
# Letting a user spec collide with these silently breaks input/output staging.
_RESERVED_MOUNT_DESTS = frozenset(
    {
        "/",
        CONTAINER_WORK_DIR,
        CONTAINER_INPUT_DIR,
        CONTAINER_OUTPUT_DIR,
        CONTAINER_TASK_DIR,
    }
)


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
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError(f"Invalid extra_mounts entry {spec!r}: expected `src:dst[:ro|rw]`.")
    src, dst = parts[0], parts[1]
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


class DockerRunner:
    """Spawns a per-task container and reconstructs the EvaluationResult.

    One instance per task. Stateless across tasks -- batch execution just
    instantiates N runners concurrently.
    """

    def __init__(
        self,
        rt: ResolvedTask,
        preserve_sandbox: bool = False,
        stream_callback: StreamCallback | None = None,
    ) -> None:
        self.rt = rt
        self.preserve_sandbox = preserve_sandbox
        self.stream_callback = stream_callback

    @property
    def _docker_config(self) -> DockerDriverConfig:
        sandbox = self.rt.task.sandbox or SandboxConfig()
        return sandbox.docker

    @property
    def _limits(self) -> ResourceLimits:
        sandbox = self.rt.task.sandbox or SandboxConfig()
        return sandbox.limits

    async def run(self) -> EvaluationResult:
        """Run the task in a container and return the parsed EvaluationResult.

        The container is responsible for producing ``task.json`` in
        ``CONTAINER_OUTPUT_DIR``. On any path where the container exits
        without producing it, this raises ``DockerRunError`` and the batch
        dispatcher converts that to an ERROR-status EvaluationResult.
        """
        _preflight()
        image = self._docker_config.image or DEFAULT_IMAGE_TAG
        await asyncio.to_thread(_preflight_image_version, image)
        await asyncio.to_thread(self.rt.run_dir.mkdir, parents=True, exist_ok=True)

        # Stage only the inputs (task YAML + context). The *output* dir is
        # the host's run_dir itself, bind-mounted at the same path inside
        # the container so the in-container Orchestrator writes
        # task.json/task.log/task.html/artifacts/ straight into the host
        # filesystem -- no copy step, paths are symmetric inside and out.
        staging = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=f"coder_eval_docker_{self.rt.task.task_id}_"))
        input_dir = staging / "input"
        await asyncio.to_thread(input_dir.mkdir)
        output_dir = self.rt.run_dir.resolve()

        try:
            # Always serialise the *post-override* TaskDefinition. We can't use
            # rt.source_yaml because that's the raw on-disk text -- _apply_cli_overrides
            # has since mutated rt.task in-memory (e.g. --model, --max-turns), and the
            # container needs to see those mutations.
            task_yaml_in = input_dir / "task.yaml"

            def _dump_task_yaml() -> str:
                return yaml.safe_dump(self.rt.task.model_dump(mode="json"), sort_keys=False)

            task_yaml_text = await asyncio.to_thread(_dump_task_yaml)
            await asyncio.to_thread(task_yaml_in.write_text, task_yaml_text)
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
                    "preserve_sandbox": self.preserve_sandbox,
                    "source_yaml": self.rt.source_yaml,
                }
            )
            await asyncio.to_thread((input_dir / "context.json").write_text, context_payload)

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
            # stays pure for testability). Creates ~/.claude/session-env on
            # the host so the RW child mount in _build_argv resolves cleanly.
            await asyncio.to_thread(self._prepare_host_mounts)
            argv = self._build_argv(input_dir, output_dir, container_name=container_name)
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
            )
            assert proc.stdout is not None
            log_path = self.rt.run_dir / "docker.log"
            log_fh = await asyncio.to_thread(log_path.open, "w", encoding="utf-8")
            # Cancellation guard: `docker run --rm` does NOT propagate kill
            # to the container daemon-side. Without this `finally`, Ctrl-C
            # on the host leaves the container running and burning LLM
            # budget. Covers CancelledError, KeyboardInterrupt, and any
            # other exit-by-exception path uniformly.
            try:
                async for raw_line in proc.stdout:
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
                    await asyncio.to_thread(log_fh.write, line + "\n")
                    await asyncio.to_thread(log_fh.flush)
                    logger.debug("[docker:%s] %s", self.rt.task.task_id, line)
                returncode = await proc.wait()
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

            task_json = output_dir / "task.json"
            if not await asyncio.to_thread(task_json.exists):
                raise DockerRunError(
                    f"Container exited with code {returncode} without producing task.json. "
                    + f"See {log_path} for container output."
                )

            # output_dir IS rt.run_dir -- no copy needed.
            task_json_text = await asyncio.to_thread(task_json.read_text)
            result = EvaluationResult.model_validate_json(task_json_text)
            self._warn_on_version_mismatch(result)
            return result
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, ignore_errors=True)

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

    def _prepare_host_mounts(self) -> None:
        """Side-effecting prep that ``_build_argv`` must not do.

        Currently: create ``~/.claude/session-env`` so the RW child mount has
        a directory to bind. Argv rendering needs to stay pure — calling it
        twice (e.g. for logging then exec) must not double-create dirs under
        ``$HOME``.
        """
        if os.environ.get("CODER_EVAL_NO_CLAUDE_MOUNT"):
            return
        host_claude_dir = Path.home() / ".claude"
        if host_claude_dir.is_dir():
            (host_claude_dir / "session-env").mkdir(parents=True, exist_ok=True)

    def _build_argv(self, input_dir: Path, output_dir: Path, *, container_name: str) -> list[str]:
        cfg = self._docker_config
        image = cfg.image or DEFAULT_IMAGE_TAG
        argv: list[str] = ["docker", "run", "--rm", "--name", container_name]

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

        # Strict allowlist: ONLY env vars listed in `cfg.env_passthrough`
        # cross the boundary. `--env VAR` (name-only) tells docker to copy
        # the value from our current env at run time, so secrets stay out
        # of the rendered argv list that we log.
        for env_var in cfg.env_passthrough:
            if env_var in os.environ:
                argv += ["--env", env_var]

        argv += ["-v", f"{input_dir.resolve()}:{CONTAINER_INPUT_DIR}:ro"]
        # Mount the host run_dir at the same path inside the container so
        # the in-container Orchestrator writes task.json/task.log/etc.
        # directly to the host filesystem (and absolute paths in logs
        # match what a user sees on the host).
        argv += ["-v", f"{output_dir}:{output_dir}"]
        # Mount the original task dir at the SAME host path so the
        # in-container Orchestrator can set TASK_DIR (used by run_command
        # criteria via `$TASK_DIR/foo.json`) to a path that resolves
        # identically inside and outside the container.
        host_task_dir: Path | None = None
        if self.rt.task_file:
            host_task_dir = self.rt.task_file.parent.resolve()
            argv += ["-v", f"{host_task_dir}:{host_task_dir}:ro"]
        # Forward the host's Claude Code OAuth state so the in-container
        # CLI inherits the same login as the host. Two-layer mount:
        #   1. ~/.claude itself: read-ONLY (settings, OAuth token, cache).
        #   2. ~/.claude/session-env: read-WRITE; the CLI creates a fresh
        #      `<uuid>/` subdir per Bash tool invocation. Without RW here
        #      every Bash call fails with EROFS. Child mount overrides the
        #      parent's :ro mode for this subtree only.
        # Net effect: container can write the per-session ephemera the
        # CLI needs, can't touch settings/OAuth/etc.
        # Opt out via CODER_EVAL_NO_CLAUDE_MOUNT=1.
        if not os.environ.get("CODER_EVAL_NO_CLAUDE_MOUNT"):
            host_claude_dir = Path.home() / ".claude"
            if host_claude_dir.is_dir():
                argv += ["-v", f"{host_claude_dir}:{host_claude_dir}:ro"]
                # session-env is created in _prepare_host_mounts before argv
                # rendering so this stays a pure argv builder.
                session_env = host_claude_dir / "session-env"
                argv += ["-v", f"{session_env}:{session_env}"]

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
        for source in (sandbox_cfg.template_sources or []) if sandbox_cfg else []:
            if isinstance(source, TemplateDirSource):
                _auto_mount(source.path)

        # Defensive: system_prompt_file is normally inlined into
        # system_prompt by load_task / experiment resolution, but a variant
        # could conceivably inject an absolute path that survives. Cover
        # that path so the in-container Orchestrator can read it.
        agent_cfg = self.rt.task.agent
        if agent_cfg and agent_cfg.system_prompt_file:
            _auto_mount(agent_cfg.system_prompt_file, dir_only=False)

        # reference.file / reference.directory: if a task ships absolute
        # paths (or relative paths that escape the task_dir mount via
        # ``..``), they must be mounted explicitly. Relative paths under
        # task_dir are already covered by the symmetric task_dir mount.
        reference = self.rt.task.reference
        if reference is not None:
            _auto_mount(reference.file, dir_only=False)
            _auto_mount(reference.directory)
        for mount in cfg.extra_mounts:
            normalized = _validate_extra_mount(mount)
            argv += ["-v", normalized]

        argv += [image]
        # Override the entrypoint's defaults so they point at the symmetric
        # host paths now bind-mounted into the container.
        argv += ["--output", str(output_dir)]
        if host_task_dir is not None:
            argv += ["--task-dir", str(host_task_dir)]
        return argv


def build_error_result(rt: ResolvedTask, exc: BaseException) -> EvaluationResult:
    """Synthesize an ERROR-status EvaluationResult for a Docker-runner failure.

    Mirrors the shape produced by ``_create_error_task_result`` in
    ``orchestration.batch`` so downstream reporting code doesn't have to
    special-case Docker failures.
    """
    return EvaluationResult(
        task_id=rt.task.task_id,
        task_description=f"Docker run failed: {type(exc).__name__}",
        variant_id=rt.variant_id,
        agent_type=AgentKind.UNKNOWN,
        started_at=datetime.now(),
        final_status=FinalStatus.ERROR,
        error_message=str(exc),
        iteration_count=0,
        environment_info={},
    )
