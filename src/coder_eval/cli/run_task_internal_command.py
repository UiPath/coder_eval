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
from pathlib import Path

import typer

from coder_eval.config import settings
from coder_eval.isolation.docker_runner import (
    CONTAINER_INPUT_DIR,
    CONTAINER_OUTPUT_DIR,
    CONTAINER_TASK_DIR,
    HEARTBEAT_FILENAME,
    HEARTBEAT_STALE_SECONDS,
)
from coder_eval.logging_config import setup_logging
from coder_eval.models import ConfigLineageEntry
from coder_eval.orchestration.task_loader import load_task


logger = logging.getLogger(__name__)


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
            # Either signal of life resets the stale clock. mtime guards
            # against the empty-counter startup race (heartbeat_path.touch
            # on host + delayed first write_text → both reads return "" but
            # mtime did advance). Counter guards against bind-mount mtime
            # latency on macOS gRPC-FUSE / VirtioFS, where the inode-level
            # mtime can lag the actual write by seconds.
            if (current and current != last_counter) or current_mtime > last_mtime:
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
    preserve_sandbox: bool = context.get("preserve_sandbox", False)
    config_lineage = {k: ConfigLineageEntry.model_validate(v) for k, v in (context.get("config_lineage") or {}).items()}
    # Prefer the host's raw source_yaml so task.json's audit trail matches
    # the in-process driver. Fall back to the staged (post-override) YAML
    # for older host versions that didn't forward it.
    host_source_yaml: str | None = context.get("source_yaml")

    # Load the post-override spec from the staged YAML. We then point
    # `task_file` at a path *under the symmetric task_dir mount* so the
    # Orchestrator's `task_file.parent` reasoning -- specifically the
    # `TASK_DIR` env exposed to `run_command` criteria -- resolves to the
    # original host task directory rather than `/work/input/`.
    task, source_yaml = load_task(task_yaml)
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

    # Late import: orchestrator pulls in heavy deps (anthropic SDK etc.)
    # that we don't want to load just to print --help.
    from coder_eval.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        task=task,
        run_dir=output_dir,
        preserve_sandbox=preserve_sandbox,
        task_file=runtime_task_file,
        variant_id=variant_id,
        source_yaml=source_yaml,
        config_lineage=config_lineage,
        replicate_index=replicate_index,
    )

    # Install the stdout-NDJSON stream callback so per-tool-call events
    # reach the host. Late import keeps the streaming module out of the
    # default --help path.
    from coder_eval.streaming.wire import StdoutNDJsonCallback

    orchestrator.stream_callback = StdoutNDJsonCallback()

    asyncio.run(orchestrator.run())
    # Orchestrator.run() writes task.json to run_dir (== output_dir). Done.
