"""Internal CLI subcommand executed inside the Docker container.

Not part of the public CLI surface -- the host's :class:`DockerRunner`
invokes it via ``docker run``. It loads the staged task + context from
``/work/input``, runs one full evaluation cycle in-process (driver=tempdir),
and writes ``task.json`` + ``task.html`` to ``/work/output``.

The container always exits 0 once ``task.json`` is written, even if the
task itself failed -- criterion failures are signaled via the final_status
field, not the container exit code. Setup failures (missing input,
malformed YAML) exit non-zero before producing task.json so the host can
distinguish them from task-level failures.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

import typer
import yaml

from coder_eval.config import settings
from coder_eval.isolation.docker_runner import (
    HEARTBEAT_FILENAME,
    HEARTBEAT_STALE_SECONDS,
)
from coder_eval.logging_config import setup_logging
from coder_eval.models import (
    CONTAINER_INPUT_DIR,
    CONTAINER_OUTPUT_DIR,
    CONTAINER_TASK_DIR,
    ConfigLineageEntry,
    PreservationMode,
    TaskDefinition,
)
from coder_eval.orchestration.task_loader import load_task, parse_task_dict


logger = logging.getLogger(__name__)


def heartbeat_is_alive(current: str, last_counter: str, current_mtime: float, last_mtime: float) -> bool:
    """True when the heartbeat shows a fresh signal of life.

    Counter advance OR mtime advance counts as alive. The mtime arm covers the empty-counter
    startup race (host touch + delayed first write → both reads ""; mtime advanced); the counter
    arm covers bind-mount mtime latency (macOS gRPC-FUSE/VirtioFS) where mtime lags the write.
    """
    return bool(current and current != last_counter) or current_mtime > last_mtime


def _merge_full_task(task_yaml: Path, input_dir: Path) -> tuple[TaskDefinition, str | None]:
    """Load the criteria-stripped ``task.yaml`` and restore the real
    criteria/reference from the root-only ``task_full.json`` sibling BEFORE parsing.

    Under the isolation barrier the agent-readable ``task.yaml`` is criteria-stripped
    (``success_criteria: []``), which cannot pass ``TaskDefinition`` validation on its
    own -- so the restore must happen at the raw-dict level, not on an already-parsed
    task (parsing the stripped dict would raise first). Returns the grading-ready task
    plus the raw ``source_yaml`` (for the audit trail).

    Falls back to parsing the raw dict as-is if ``task_full.json`` is absent
    (defensive; that parse then surfaces the missing-criteria error loudly rather
    than silently grading against empty criteria).
    """
    raw = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
    full_path = input_dir / "task_full.json"
    if not full_path.exists():
        logger.warning("task_full.json missing; grading with the stripped (agent-visible) criteria only")
        return parse_task_dict(raw, task_yaml.parent), None
    full = json.loads(full_path.read_text(encoding="utf-8"))
    raw["success_criteria"] = full.get("success_criteria", [])
    raw["reference"] = full.get("reference")
    merged = parse_task_dict(raw, task_yaml.parent)
    return merged, full.get("source_yaml")


def _apply_isolation_barrier(
    *,
    agent_run_uid: int,
    task: TaskDefinition,
    input_dir: Path,
    output_dir: Path,
    task_dir: Path,
    workspace_dir: Path | None,
    plugin_host_paths: list[str] | None = None,
    reference_host_paths: list[str] | None = None,
) -> None:
    """Lock grading material root-0700, grant the agent uid its own paths, and set
    ``agent_run_uid`` on the resolved agent config. Root-only; fails LOUD otherwise.

    Runs as the container's root PID before the agent turn. Grading
    (SuccessChecker / run_command / judges) stays in this root process and reads
    the locked harness via ``$TASK_DIR``/``$SKILLS_REPO_PATH`` (root ignores DAC),
    so only the agent's dropped CLI subprocess is denied.

    ``reference_host_paths`` are the resolved host mount targets for an
    absolute/escaping ``reference.file``/``reference.directory`` (the reference
    solution — grading material, never shown to the agent). They are bind-mounted rw
    for the in-container grader and locked root-0700 here so the dropped agent uid
    cannot read the answer off disk.

    ``plugin_host_paths`` are the ORIGINAL host plugin/skills-repo mount paths the
    host forwarded via ``context.json``. They are the raw grader-bearing mounts
    (``tests/``, ``check_*.py``, ``RESOLUTION.md``, ``reference_agents/``) that
    ``docker_runner`` bind-mounts at ``{path}:{path}``; the staged task's own
    ``agent.plugins[].path`` has been rewritten to ``/work/skills`` and can no
    longer point at them, so the lock loop below MUST use these forwarded paths.
    """
    from coder_eval.isolation import container_perms
    from coder_eval.models import AGENT_UID, CONTAINER_SKILL_DOCS_DIR, plugin_path

    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        raise typer.Exit(
            _fatal(
                "isolation barrier requested (agent_run_uid set) but the container is not root; "
                + "refusing to run the agent un-dropped as the container owner"
            )
        )

    # Pre-create the agent workspace so it exists before the lock+grant, and so the
    # orchestrator (root) later writes into an agent-owned dir.
    workspace = workspace_dir if workspace_dir is not None else output_dir / "artifacts" / task.task_id
    workspace.mkdir(parents=True, exist_ok=True)

    # 1. Lock harness material root-0700 (deny the agent uid). /work/input carries
    #    criteria/graders (incl. the root-only task_full.json); the per-task-dir mount
    #    + the raw plugin/skills-repo mounts are the auto-mounted grading trees.
    #
    #    /work/output is deliberately NOT locked as a whole: it is a bind mount SHARED
    #    with the host, which writes the liveness heartbeat there as a non-root uid — a
    #    root-0700 lock would make the heartbeat unwritable and self-reap the container.
    #    task.json (surface #4) is written only AFTER the agent turn ends, so it is not
    #    a live read surface during the turn; and its source_yaml is already nulled in
    #    the agent-visible context. The agent writes only under its granted artifacts
    #    subdir (below); /work/output siblings are not staged with criteria.
    harness: list[Path] = [input_dir, task_dir]
    # The raw skills-repo mounts the host forwarded (their in-container path == the
    # host path). Never the /work/skills sanitized copy (agent-legitimate).
    for raw in plugin_host_paths or []:
        if raw and not str(raw).startswith(CONTAINER_SKILL_DOCS_DIR):
            harness.append(Path(raw))
    # Reference solution mounts (grading material). Only present for an
    # absolute/escaping reference; a relative reference under task_dir is already
    # covered by the task_dir lock above.
    for raw in reference_host_paths or []:
        if raw:
            harness.append(Path(raw))
    # Defence-in-depth: if the staged task STILL carries a raw (non-/work/skills)
    # plugin path — e.g. a future staging change stopped rewriting it — lock it too.
    # A plugin entry we can't parse a path from, while the barrier is active, is a
    # hard error (a silently-skipped lock is exactly the C1 class of bug).
    for plugin in (task.agent.plugins if task.agent else None) or []:
        raw_path = plugin_path(plugin)
        if raw_path is None:
            raise typer.Exit(
                _fatal(
                    "isolation barrier active but a plugin entry has no parseable path; "
                    + "refusing to run with a potentially unlocked grader mount"
                )
            )
        if not raw_path.startswith(CONTAINER_SKILL_DOCS_DIR):
            harness.append(Path(raw_path))
    container_perms.lock_harness_root_0700(harness)

    # 2. Grant the agent uid ownership of the paths it reads/writes: its workspace
    #    (pre-created above so the orchestrator writes into an agent-owned dir), the
    #    skill-DOCS mount, and the ~/.claude copy. `/tmp` is deliberately NOT chowned:
    #    it is already 1777
    #    (world-writable, sticky), so the agent uid can create its own temp files
    #    there; a recursive chown of /tmp would be both unnecessary and hazardous (it
    #    would clobber any root-0700 mkdtemp grader dir under /tmp and defeat the
    #    sticky-bit isolation).
    agent_paths: list[Path] = [workspace, Path(CONTAINER_SKILL_DOCS_DIR)]
    claude_home = Path.home() / ".claude"
    if claude_home.exists():
        agent_paths.append(claude_home)
    container_perms.grant_agent_ownership(agent_paths)

    # 3. Set agent_run_uid on the resolved agent config so each agent (claude-code /
    #    codex / antigravity) wires its own spawn seam to this uid. Framework-set,
    #    not YAML — assigned directly on the typed field.
    if task.agent is not None:
        task.agent.agent_run_uid = AGENT_UID


def _fatal(message: str) -> int:
    """Log + echo a fatal setup error and return the exit code (2)."""
    logger.error(message)
    typer.echo(f"FATAL: {message}", err=True)
    return 2


def run_task_internal_command(
    input_dir: Path = typer.Option(  # noqa: B008
        Path(CONTAINER_INPUT_DIR),
        "--input",
        help="Directory containing task.yaml and context.json (bind-mounted by host).",
    ),
    output_dir: Path = typer.Option(  # noqa: B008
        Path(CONTAINER_OUTPUT_DIR),
        "--output",
        help="Directory to write task.json/task.html into (bind-mounted by host).",
    ),
    task_dir: Path = typer.Option(  # noqa: B008
        Path(CONTAINER_TASK_DIR),
        "--task-dir",
        help="Original task directory mount (used to resolve relative template paths).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose (DEBUG level) logging",
    ),
) -> None:
    """Run a single staged task inside the container."""
    # Use the same logging path as the host CLI so LOG_LEVEL from the
    # forwarded env is honoured. Without this, root stays at INFO and the
    # DEBUG-level task_log_handler attached by Orchestrator never sees the
    # agent's per-tool-call DEBUG records.
    log_level = "DEBUG" if verbose else settings.log_level
    setup_logging(level=log_level)

    # Start the host-heartbeat watchdog: if the host process dies
    # ungracefully (SIGKILL, Claude-Code Escape, crash) before it can
    # `docker kill` us, the heartbeat file in output_dir goes stale and
    # we self-exit -- otherwise the container would keep burning LLM
    # budget orphaned. Daemon thread so it doesn't block normal shutdown.
    import os as _os
    import threading
    import time

    def _watch_host_heartbeat() -> None:
        heartbeat = output_dir / HEARTBEAT_FILENAME
        # Grace period for the host to write the first counter value.
        time.sleep(HEARTBEAT_STALE_SECONDS)
        last_counter = ""
        last_mtime = 0.0
        last_change = time.monotonic()
        while True:
            try:
                current = heartbeat.read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                current = ""
            try:
                current_mtime = heartbeat.stat().st_mtime
            except (FileNotFoundError, OSError):
                current_mtime = 0.0
            now = time.monotonic()
            if heartbeat_is_alive(current, last_counter, current_mtime, last_mtime):
                last_counter = current
                last_mtime = current_mtime
                last_change = now
            if now - last_change > HEARTBEAT_STALE_SECONDS:
                logger.error(
                    "Host heartbeat stale (>%ss); exiting to reap orphan container.",
                    HEARTBEAT_STALE_SECONDS,
                )
                # os._exit skips atexit and IO flushing, so the error line
                # above would routinely be lost -- making a genuine
                # stale-heartbeat suicide indistinguishable from an external
                # SIGKILL in the archived logs. Flush best-effort first;
                # never let a flush failure stop the exit.
                import sys as _sys

                for _handler in logging.getLogger().handlers:
                    with contextlib.suppress(Exception):
                        _handler.flush()
                with contextlib.suppress(Exception):
                    _sys.stdout.flush()
                with contextlib.suppress(Exception):
                    _sys.stderr.flush()
                _os._exit(137)
            time.sleep(HEARTBEAT_STALE_SECONDS / 4)

    threading.Thread(target=_watch_host_heartbeat, daemon=True).start()

    task_yaml = input_dir / "task.yaml"
    context_json = input_dir / "context.json"
    if not task_yaml.exists():
        typer.echo(f"FATAL: missing {task_yaml}", err=True)
        raise typer.Exit(2)
    if not context_json.exists():
        typer.echo(f"FATAL: missing {context_json}", err=True)
        raise typer.Exit(2)

    context = json.loads(context_json.read_text(encoding="utf-8"))
    variant_id: str = context["variant_id"]
    replicate_index: int = context.get("replicate_index", 0)
    # The host resolves the driver-derived default before dispatch; the container
    # obeys it verbatim. This command only ever runs inside the docker driver, so
    # a missing key falls back to the docker default (DIRECT_WRITE) — a deliberate
    # default, not version back-compat.
    preservation_mode = PreservationMode(context.get("preservation_mode", PreservationMode.DIRECT_WRITE.value))
    # Docker WORKDIR alignment: the host resolves the concrete WORKDIR
    # (config value / "auto" -> `docker inspect` / fallback) and forwards it here.
    # Absent -> None -> standard run_dir/artifacts workspace.
    workspace_dir_raw = context.get("workspace_dir")
    workspace_dir = Path(workspace_dir_raw) if workspace_dir_raw else None
    config_lineage = {k: ConfigLineageEntry.model_validate(v) for k, v in (context.get("config_lineage") or {}).items()}
    # The unprivileged uid the agent's CLI subprocess is dropped to under the
    # docker user/permission isolation barrier. None (older host / barrier off) =>
    # no drop, legacy behaviour (task.yaml carried full criteria, source_yaml on
    # context.json). When set, the agent-readable task.yaml was criteria-stripped
    # and the real criteria/reference/source_yaml ride on the root-only
    # task_full.json sibling below.
    agent_run_uid: int | None = context.get("agent_run_uid")
    # Original host plugin/skills-repo mount paths (resolved). The staged task.yaml
    # rewrote plugin paths to /work/skills, so the barrier locks THESE raw
    # grader-bearing in-container mounts instead (see _apply_isolation_barrier).
    plugin_host_paths: list[str] = context.get("plugin_host_paths") or []
    # Resolved host mount targets for an absolute/escaping reference (grading material,
    # "NEVER shown to the agent"). Bind-mounted rw for the grader; locked root-0700 here
    # so the dropped agent uid can't read the reference solution off disk.
    reference_host_paths: list[str] = context.get("reference_host_paths") or []

    # Load the post-override spec from the staged YAML. We then point
    # `task_file` at a path *under the symmetric task_dir mount* so the
    # Orchestrator's `task_file.parent` reasoning -- specifically the
    # `TASK_DIR` env exposed to `run_command` criteria -- resolves to the
    # original host task directory rather than `/work/input/`.
    host_source_yaml: str | None = context.get("source_yaml")

    if agent_run_uid is not None:
        # Barrier path: this process is root, /work/input is about to be locked
        # root-0700. The agent-readable task.yaml was criteria-stripped
        # (success_criteria=[]), which cannot parse standalone -- restore the FULL
        # criteria/reference from the root-only task_full.json at the raw-dict level
        # BEFORE parsing.
        task, full_source_yaml = _merge_full_task(task_yaml, input_dir)
        if host_source_yaml is None:
            host_source_yaml = full_source_yaml
        source_yaml = host_source_yaml if host_source_yaml is not None else task_yaml.read_text(encoding="utf-8")
    else:
        task, source_yaml = load_task(task_yaml)
        # Prefer the raw source_yaml so task.json's audit trail matches the in-process
        # driver. Absent it (older host), keep the staged post-override YAML.
        if host_source_yaml is not None:
            source_yaml = host_source_yaml
    # The path below is never re-read; it only seeds Orchestrator's TASK_DIR.
    runtime_task_file = task_dir / "task.yaml" if task_dir.is_dir() else task_yaml

    # Force driver back to tempdir for the actual in-container run.
    # We're already inside the container; another nested docker would be
    # both wrong and impossible (no docker CLI in image).
    if task.sandbox.driver == "docker":
        task = task.model_copy(update={"sandbox": task.sandbox.model_copy(update={"driver": "tempdir"})})

    output_dir.mkdir(parents=True, exist_ok=True)

    if agent_run_uid is not None:
        # As root: lock all grading material root-0700 (deny the agent uid), grant
        # the agent uid ownership of the paths it legitimately reads/writes, set
        # agent_run_uid on the resolved agent config (each agent wires its own spawn
        # seam), and fail LOUD if we are not actually root (never silently run the
        # agent as root). Runs BEFORE the agent turn (orchestrator.run() below).
        _apply_isolation_barrier(
            agent_run_uid=agent_run_uid,
            task=task,
            input_dir=input_dir,
            output_dir=output_dir,
            task_dir=task_dir,
            workspace_dir=workspace_dir,
            plugin_host_paths=plugin_host_paths,
            reference_host_paths=reference_host_paths,
        )

    # Late import: orchestrator pulls in heavy deps (anthropic SDK etc.)
    # that we don't want to load just to print --help.
    from coder_eval.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        task=task,
        run_dir=output_dir,
        preservation_mode=preservation_mode,
        task_file=runtime_task_file,
        variant_id=variant_id,
        source_yaml=source_yaml,
        config_lineage=config_lineage,
        replicate_index=replicate_index,
        workspace_dir=workspace_dir,
    )

    # Install the stdout-NDJSON stream callback so per-tool-call events
    # reach the host. Late import keeps the streaming module out of the
    # default --help path.
    from coder_eval.streaming.wire import StdoutNDJsonCallback

    orchestrator.stream_callback = StdoutNDJsonCallback()

    asyncio.run(orchestrator.run())
    # Orchestrator.run() writes task.json to run_dir (== output_dir). Done.
