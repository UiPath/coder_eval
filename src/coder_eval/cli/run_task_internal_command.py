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
    HEARTBEAT_FILENAME,
    HEARTBEAT_STALE_SECONDS,
)
from coder_eval.logging_config import setup_logging
from coder_eval.models import (
    CONTAINER_INPUT_DIR,
    CONTAINER_OUTPUT_DIR,
    CONTAINER_TASK_DIR,
    IN_CONTAINER_ENV,
    ConfigLineageEntry,
    EvaluationResult,
    PreservationMode,
    SandboxConfig,
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
    # Start the host-heartbeat watchdog: if the host process dies
    # ungracefully (SIGKILL, Claude-Code Escape, crash) before it can
    # `docker kill` us, the heartbeat file in output_dir goes stale and
    # we self-exit -- otherwise the container would keep burning LLM
    # budget orphaned. Daemon thread so it doesn't block normal shutdown.
    import os as _os
    import threading
    import time

    # ARMED ONLY INSIDE THE CONTAINER, and not even defined outside one. The
    # watchdog's whole authority is `os._exit(137)` on the process it runs in,
    # and the only process that may be reaped that way is the container's own
    # disposable main -- there is no container to orphan anywhere else, so
    # outside one the thread can do nothing but harm. It did: a test invoked
    # this command in-process (legitimately -- the command must refuse a
    # malformed context.json, and proving that means calling it) and the pytest
    # worker inherited the thread, which found no heartbeat and 40s later exited
    # the worker mid-way through an unrelated test file. It named a different
    # test on each run and on each platform, carried no traceback, and took that
    # worker's coverage data with it -- so the gate reported "65.13 < 80.00",
    # naming neither the test nor the cause.
    #
    # Gated on CODER_EVAL_IN_CONTAINER (set by docker_runner on the container's
    # argv), NOT on `driver`, for the same reason the reference-permission
    # window is: this command rewrites `driver: docker` -> `tempdir` before
    # building the in-container Orchestrator, so a driver-based gate would
    # disarm itself on exactly the path that needs it. See
    # `Sandbox.enforces_permission_windows`.
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
    # Use the same logging path as the host CLI so LOG_LEVEL from the
    # forwarded env is honoured. Without this, root stays at INFO and the
    # DEBUG-level task_log_handler attached by Orchestrator never sees the
    # agent's per-tool-call DEBUG records.
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

    context = json.loads(context_json.read_text(encoding="utf-8"))
    # Checked, not just annotated. `json.loads` returns `Any`, so pyright accepts
    # `variant_id: str = context["variant_id"]` for a value that may be anything
    # at all — the annotation reads like a guarantee and enforces nothing. A
    # `"replicate_index": "00"` then reached `build_task_run_dir` typed as `int`.
    # This is the host→container boundary; the comment below (about `grade`
    # being the one raw value) was only true because these two looked checked.
    variant_id = context["variant_id"]
    if not isinstance(variant_id, str):
        typer.echo(f"FATAL: context.json 'variant_id' must be a string, got {variant_id!r}", err=True)
        raise typer.Exit(2)
    replicate_index = context.get("replicate_index", 0)
    if not isinstance(replicate_index, int) or isinstance(replicate_index, bool):
        typer.echo(f"FATAL: context.json 'replicate_index' must be an integer, got {replicate_index!r}", err=True)
        raise typer.Exit(2)
    # The host resolves the driver-derived default before dispatch; the container
    # obeys it verbatim. This command only ever runs inside the docker driver, so
    # a missing key falls back to the docker default (DIRECT_WRITE) — a deliberate
    # default, not version back-compat.
    preservation_mode = PreservationMode(context.get("preservation_mode", PreservationMode.DIRECT_WRITE.value))
    # `coder-eval run` vs `coder-eval execute`, decided host-side. Defaults to
    # True (grade) so a host that predates `execute` — which never writes the
    # key — keeps its exact behavior.
    # Coerced, not annotated, like every other value crossing this boundary —
    # `grade` was once the only raw one, so a hand-edited or older-format
    # `"grade": "false"` arrived as a truthy str typed as bool and silently
    # graded a run that asked not to be graded.
    grade_raw = context.get("grade", True)
    if not isinstance(grade_raw, bool):
        typer.echo(f"FATAL: context.json 'grade' must be a boolean, got {grade_raw!r}", err=True)
        raise typer.Exit(2)
    grade: bool = grade_raw
    # A DETACHED GRADE, not a run: seed from the staged prior.json and adopt the
    # already-executed workspace instead of starting an agent. Coerced for the
    # same reason `grade` is — a hand-edited `"regrade": "false"` is a truthy
    # str, and getting this one wrong would re-RUN the agent against a workspace
    # the operator asked only to grade, destroying the trajectory being graded.
    regrade_raw = context.get("regrade", False)
    if not isinstance(regrade_raw, bool):
        typer.echo(f"FATAL: context.json 'regrade' must be a boolean, got {regrade_raw!r}", err=True)
        raise typer.Exit(2)
    regrade: bool = regrade_raw
    # What task.json RECORDS as the task's source path, as distinct from the
    # path this process resolves TASK_DIR against (see Orchestrator's
    # `recorded_task_file`). Absent on an older host -> None -> the container
    # path is recorded, which is the pre-existing behaviour.
    host_task_file_raw = context.get("host_task_file")
    recorded_task_file = Path(host_task_file_raw) if host_task_file_raw else None
    # Docker WORKDIR alignment: the host resolves the concrete WORKDIR
    # (config value / "auto" -> `docker inspect` / fallback) and forwards it here.
    # Absent -> None -> standard run_dir/artifacts workspace.
    workspace_dir_raw = context.get("workspace_dir")
    workspace_dir = Path(workspace_dir_raw) if workspace_dir_raw else None
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

    # Captured BEFORE the rewrite below: this is what `task.json` records.
    # Recording the rewritten copy made a docker run's own record claim
    # `driver: tempdir`, so `evaluate <run_dir>` skipped the host-grading
    # refusal and the `graded_on_host` stamp entirely. See Orchestrator's
    # `recorded_task`.
    authored_task = task

    # Force driver back to tempdir for the actual in-container run.
    # We're already inside the container; another nested docker would be
    # both wrong and impossible (no docker CLI in image).
    if task.sandbox.driver == "docker":
        # noqa: CE051 — the ONE legitimate rewrite. We are already inside the
        # container the docker driver asked for, so the isolation the driver
        # names is present, not bypassed; a nested docker would be both wrong
        # and impossible (no docker CLI in the image).
        # Re-validated rather than `model_copy(update=...)`, matching its sibling
        # `regrade.grading_sandbox_config`: `update` skips BOTH pydantic and
        # pyright, so a typo would produce a SandboxConfig violating its own
        # `Literal` and only surface far downstream. Two driver-rewrite sites
        # landing in one change with two different levels of type safety is how
        # the weaker one becomes the pattern people copy.
        rewritten = SandboxConfig.model_validate({**task.sandbox.model_dump(), "driver": "tempdir"})  # noqa: CE051
        task = task.model_copy(update={"sandbox": rewritten})

    output_dir.mkdir(parents=True, exist_ok=True)

    if regrade:
        _grade_recorded_run(
            task=task,
            authored_task=authored_task,
            recorded_task_file=recorded_task_file,
            input_dir=input_dir,
            output_dir=output_dir,
            runtime_task_file=runtime_task_file,
            source_yaml=source_yaml,
            variant_id=variant_id,
            replicate_index=replicate_index,
        )
        return

    # Late import: orchestrator pulls in heavy deps (anthropic SDK etc.)
    # that we don't want to load just to print --help.
    from coder_eval.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        task=task,
        run_dir=output_dir,
        preservation_mode=preservation_mode,
        task_file=runtime_task_file,
        recorded_task_file=recorded_task_file,
        variant_id=variant_id,
        source_yaml=source_yaml,
        config_lineage=config_lineage,
        replicate_index=replicate_index,
        workspace_dir=workspace_dir,
        grade=grade,
        recorded_task=authored_task,
    )

    # Install the stdout-NDJSON stream callback so per-tool-call events
    # reach the host. Late import keeps the streaming module out of the
    # default --help path.
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
) -> None:
    """Grade an already-executed row INSIDE the container that produced it.

    This is the container half of `evaluate <run_dir>` / `run --resume` over a
    `driver: docker` task. The host stages `prior.json` next to `task.yaml` and
    bind-mounts the executed workspace at ``CONTAINER_GRADE_WORKSPACE``; here we
    seed from that row and run its criteria against that workspace.

    Why it must happen here at all: a container task's criteria address the
    image's paths and toolchain, so grading them on the host scores a FAILURE for
    a run that passed. The host path therefore REFUSES by default and demands
    `--allow-host-grading`. Running them back inside the same image is the only
    place the verdict means what it meant during the run — so a container-graded
    detached row carries no `graded_on_host` stamp, exactly like a `run` row.

    ``task`` is the driver-rewritten copy (docker -> tempdir, done above because
    we are already inside the container the driver asked for), which is also what
    keeps ``regrade_in_place`` from trying to dispatch a container from within
    one. ``authored_task`` is what gets RECORDED, so the row keeps saying
    `driver: docker`. ``recorded_task_file`` is the path half of that same
    distinction and travels with it: without it the row re-records
    ``/work/task_dir/task.yaml`` as its ``source_file``, a path on no host, and a
    later ``evaluate <run_dir>`` over the row refuses or mounts the wrong tree.
    The ordinary run branch above has always forwarded it; this one is the
    second consumer and must not be the one that forgets.

    Delegates to the same ``regrade_in_place`` the host uses rather than
    restating it. The two implementations that already drifted apart once —
    `evaluate`'s run-dir mode hardcoding `replicate_index=0` and relabelling
    every replicate but the first — are the reason that function exists.
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
        # Degrade to a clean message rather than a traceback: the host parses
        # this container's task.json, so a crash here surfaces as the opaque
        # "container exited without producing task.json" rather than naming the
        # staged file that could not be read.
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
            )
        )
    except RegradeError as e:
        # Surfaced as a clean message, not a traceback: the host parses this
        # container's task.json, and a RegradeError means none was written. Exit
        # 2 keeps it distinguishable from an agent failure.
        typer.echo(f"FATAL: {e}", err=True)
        raise typer.Exit(2) from e
