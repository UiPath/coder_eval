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
import logging
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

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
    IN_CONTAINER_ENV,
    ContainerContext,
    EvaluationResult,
    TaskDefinition,
)
from coder_eval.orchestration.task_loader import load_task
from coder_eval.path_utils import PRIOR_RESULT_FILENAME


logger = logging.getLogger(__name__)


def heartbeat_is_alive(current: str, last_counter: str, current_mtime: float, last_mtime: float) -> bool:
    """True when the heartbeat shows a fresh signal of life.

    Counter advance OR mtime advance counts as alive. The mtime arm covers the empty-counter
    startup race (host touch + delayed first write → both reads ""; mtime advanced); the counter
    arm covers bind-mount mtime latency (macOS gRPC-FUSE/VirtioFS) where mtime lags the write.
    """
    return bool(current and current != last_counter) or current_mtime > last_mtime


def _arm_host_heartbeat_watchdog(output_dir: Path) -> None:
    """Start the orphan-container reaper, but only inside the container.

    A module-level function rather than an inline block so the only
    process-lethal code in this command sits behind one named, testable seam
    instead of being a side effect of the command body.
    """
    # Daemon thread so it doesn't block normal shutdown.
    import os as _os
    import threading
    import time

    # HAZARD: armed ONLY inside the container. The thread's whole authority is
    # `os._exit(137)` on the process it runs in, so anywhere else it can only harm
    # -- it once killed a pytest worker mid-test-file and took its coverage with it.
    # Gated on CODER_EVAL_IN_CONTAINER, NOT on `driver`, for the same reason
    # `Sandbox.enforces_permission_windows` is: the host stages the task this
    # command runs with `driver: tempdir`.
    # Rationale: .claude/notes/isolation.md § The heartbeat watchdog is armed only inside a container
    if _os.environ.get(IN_CONTAINER_ENV) == "1":

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
                    # os._exit skips atexit and IO flushing, so this line would
                    # routinely be lost. Best-effort; never block the exit.
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
    else:
        logger.debug("Not in a container; host-heartbeat watchdog not armed.")


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
    # Same logging path as the host CLI, so the forwarded LOG_LEVEL is honoured and
    # Orchestrator's DEBUG-level task_log_handler sees the agent's records.
    log_level = "DEBUG" if verbose else settings.log_level
    setup_logging(level=log_level)

    _arm_host_heartbeat_watchdog(output_dir)

    task_yaml = input_dir / "task.yaml"
    context_json = input_dir / "context.json"
    if not task_yaml.exists():
        typer.echo(f"FATAL: missing {task_yaml}", err=True)
        raise typer.Exit(2)
    if not context_json.exists():
        typer.echo(f"FATAL: missing {context_json}", err=True)
        raise typer.Exit(2)

    try:
        ctx = ContainerContext.model_validate_json(context_json.read_text(encoding="utf-8"))
    except ValidationError as e:
        # Exit 2, a setup failure: a host/image skew must not read as a task failure.
        # Rationale: .claude/notes/isolation.md § The container contract
        typer.echo(f"FATAL: {context_json} is not a valid container contract: {e}", err=True)
        raise typer.Exit(2) from e
    # What task.json RECORDS, as distinct from the path this process resolves TASK_DIR against.
    # Rationale: .claude/notes/orchestration.md § Recording the task as authored
    recorded_task_file = Path(ctx.host_task_file) if ctx.host_task_file else None
    workspace_dir = Path(ctx.workspace_dir) if ctx.workspace_dir else None

    # `task_file` is then pointed under the task_dir mount, so the `TASK_DIR` the
    # Orchestrator exposes to `run_command` criteria resolves there, not /work/input.
    task, _ = load_task(task_yaml)
    # The path below is never re-read; it only seeds Orchestrator's TASK_DIR.
    runtime_task_file = task_dir / "task.yaml" if task_dir.is_dir() else task_yaml

    # The staged task is the host's execution copy (driver: tempdir); task.json records the sandbox as authored.
    # Rationale: .claude/notes/orchestration.md § Recording the task as authored
    authored_task = task.model_copy(update={"sandbox": ctx.authored_sandbox})

    output_dir.mkdir(parents=True, exist_ok=True)

    if ctx.regrade:
        _grade_recorded_run(
            task=task,
            authored_task=authored_task,
            recorded_task_file=recorded_task_file,
            input_dir=input_dir,
            output_dir=output_dir,
            runtime_task_file=runtime_task_file,
            source_yaml=ctx.source_yaml,
            variant_id=ctx.variant_id,
            replicate_index=ctx.replicate_index,
            container_contract=ctx.model_dump(mode="json"),
        )
        return

    # Late import: orchestrator pulls in heavy deps (anthropic SDK etc.)
    # that we don't want to load just to print --help.
    from coder_eval.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        task=task,
        run_dir=output_dir,
        preservation_mode=ctx.preservation_mode,
        task_file=runtime_task_file,
        recorded_task_file=recorded_task_file,
        variant_id=ctx.variant_id,
        source_yaml=ctx.source_yaml,
        config_lineage=ctx.config_lineage,
        replicate_index=ctx.replicate_index,
        workspace_dir=workspace_dir,
        grade=ctx.grade,
        recorded_task=authored_task,
        container_contract=ctx.model_dump(mode="json"),
    )

    # Late import keeps the streaming module out of the default --help path.
    from coder_eval.streaming.wire import StdoutNDJsonCallback

    orchestrator.stream_callback = StdoutNDJsonCallback()

    asyncio.run(orchestrator.run())
    # Orchestrator.run() writes task.json to run_dir (== output_dir). Done.


def _grade_recorded_run(
    *,
    task: TaskDefinition,
    authored_task: TaskDefinition,
    input_dir: Path,
    output_dir: Path,
    recorded_task_file: Path | None,
    runtime_task_file: Path,
    source_yaml: str,
    variant_id: str,
    replicate_index: int,
    container_contract: dict[str, Any],
) -> None:
    """Grade an already-executed row INSIDE the container that produced it.

    The container half of `evaluate <run_dir>` / `run --resume` over a
    `driver: docker` task: the host stages `prior.json` next to `task.yaml` and
    bind-mounts the executed workspace at ``CONTAINER_GRADE_WORKSPACE``.

    ``task`` is the execution copy the host staged with ``driver: tempdir``, which is also what
    keeps ``regrade_in_place`` from dispatching a container from within one.
    ``authored_task`` is what gets RECORDED, and ``recorded_task_file`` is the path
    half of that same distinction and travels with it.

    Delegates to the same ``regrade_in_place`` the host uses rather than restating
    it.

    Rationale: .claude/notes/isolation.md § Grading a docker row inside a container
    """
    from coder_eval.models import CONTAINER_GRADE_WORKSPACE
    from coder_eval.orchestration.regrade import RegradeError, regrade_in_place

    prior_path = input_dir / PRIOR_RESULT_FILENAME
    if not prior_path.is_file():
        typer.echo(f"FATAL: context.json requested a regrade but {prior_path} is missing", err=True)
        raise typer.Exit(2)
    try:
        prior = EvaluationResult.model_validate_json(prior_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # A clean message, not a traceback: the host parses this container's
        # task.json, so a crash here surfaces as the opaque "no task.json".
        typer.echo(f"FATAL: {prior_path} is not a readable EvaluationResult: {e}", err=True)
        raise typer.Exit(2) from e

    workspace = Path(CONTAINER_GRADE_WORKSPACE)
    if not workspace.is_dir():
        typer.echo(f"FATAL: the graded workspace was not mounted at {workspace}", err=True)
        raise typer.Exit(2)

    try:
        asyncio.run(
            regrade_in_place(
                task=task,
                prior=prior,
                workspace=workspace,
                run_dir=output_dir,
                task_file=runtime_task_file,
                source_yaml=source_yaml,
                variant_id=variant_id,
                replicate_index=replicate_index,
                recorded_task=authored_task,
                recorded_task_file=recorded_task_file,
                container_contract=container_contract,
            )
        )
    except RegradeError as e:
        # Clean message, not a traceback. Exit 2 keeps it distinguishable from an
        # agent failure.
        typer.echo(f"FATAL: {e}", err=True)
        raise typer.Exit(2) from e
