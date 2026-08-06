"""Batch execution support for running resolved tasks in parallel.

This module provides the unified run_batch() function that accepts pre-resolved
tasks (list[ResolvedTask]) and executes them with concurrency control,
exception handling, and result aggregation.

Task loading, config resolution, and CLI override application are handled
upstream by resolve_all_tasks() in experiment.py.
"""

import asyncio
import json
import logging
import shutil
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import (
    AgentKind,
    EvaluationResult,
    FinalStatus,
    PreservationMode,
    ResolvedTask,
    RunSummary,
    SkippedTask,
    TaskDefinition,
    TaskResult,
)
from ..path_utils import format_task_log_id
from ..pricing import unpriced_models
from ..reports_experiment import eval_result_to_task_dict
from ..streaming.callbacks import StreamCallback
from ..utils import get_version_info, looks_like_version
from .config import BatchRunConfig, resolve_preservation_mode


logger = logging.getLogger(__name__)


def _run_models(resolved_tasks: list[ResolvedTask]) -> Iterator[str | None]:
    """Every model id this run pins up front: subject agents and judge criteria.

    A criterion's model is always priced from the rate card (no judge backend
    reports a cost), so it matters more here than the agent's, which only falls
    back to the card on a turn the backend never priced.
    """
    for rt in resolved_tasks:
        yield rt.task.agent.model if rt.task.agent else None
        for criterion in rt.task.success_criteria:
            yield getattr(criterion, "model", None)


def check_pricing_coverage(resolved_tasks: list[ResolvedTask]) -> list[str]:
    """Pre-flight the rate card against the models this run will use.

    The rate card is a static table baked into the installed version, so a model
    released after it has no rate and its tokens book no money. A warning rather
    than a refusal, so a brand-new model stays evaluable the day it ships: cost is
    what degrades, not the evaluation. What the run actually lost is counted after
    the fact by ``RunSummary.tasks_cost_incomplete``.

    Only models pinned in the task YAML are visible here; one deferred to the route
    resolves inside the agent and can't be pre-flighted.

    Returns:
        The sorted, de-duplicated unpriced model ids (empty when all are priced).
    """
    missing = unpriced_models(_run_models(resolved_tasks))
    if not missing:
        return []
    logger.warning(
        "No pricing rate for %s. Turns the agent's own backend prices are unaffected, but any "
        + "judge call, timed-out turn or killed partial will book its tokens with no cost, so "
        + "run-level totals will understate the bill (RunSummary.cost_complete reports false). "
        + "Add the rate to coder_eval.pricing or register it from a plugin via register_pricing().",
        ", ".join(repr(m) for m in missing),
    )
    return missing


async def run_batch(
    resolved_tasks: list[ResolvedTask],
    config: BatchRunConfig,
    on_task_complete: Callable[[TaskResult], None] | None = None,
    on_batch_start: Callable[[int], None] | None = None,
    stream_callback_factory: Callable[[str], StreamCallback] | None = None,
    skipped_tasks: list[SkippedTask] | None = None,
    prior_results: list[TaskResult] | None = None,
    prior_resolved: list[ResolvedTask] | None = None,
) -> tuple[RunSummary, list[TaskResult]]:
    """Run resolved tasks in batch with optional parallelism.

    Tasks must be fully resolved (all config layers applied, tag filtering done).
    This function is a pure executor — no configuration or loading logic.

    Args:
        resolved_tasks: List of fully-resolved tasks from resolve_all_tasks.
        config: Batch configuration (max_parallel, preservation_mode, run_dir).
        on_task_complete: Optional callback invoked after each task finishes.
        on_batch_start: Optional callback invoked with the final task count.
        stream_callback_factory: Optional factory for streaming callbacks.
        skipped_tasks: Task YAMLs that failed to load upstream and should be
            recorded in the run summary (informational; they don't run).
        prior_results: Already-complete TaskResults (loaded from disk on
            --resume) to fold into run.json so the summary covers the whole
            run, not just this batch. These are not re-executed.
        prior_resolved: ResolvedTasks matching prior_results — used only to
            populate tags/source-path in the summary's per-task entries.

    Returns:
        Tuple of (RunSummary, list[TaskResult]) — results cover this batch
        plus any prior_results.
    """
    from ..orchestrator import Orchestrator

    start_time = datetime.now()

    check_pricing_coverage(resolved_tasks)

    if on_batch_start is not None:
        on_batch_start(len(resolved_tasks))

    semaphore = asyncio.Semaphore(config.max_parallel)
    # Tags/paths cover both freshly-run and resumed-prior tasks so every entry
    # in run.json carries its metadata.
    metadata_tasks = [*resolved_tasks, *(prior_resolved or [])]
    task_tags: dict[str, list[str]] = {rt.task.task_id: rt.task.tags for rt in metadata_tasks}
    task_paths: dict[str, str] = {rt.task.task_id: str(rt.task_file) for rt in metadata_tasks}

    async def run_single(rt: ResolvedTask) -> TaskResult:
        """Run a single resolved task with semaphore for concurrency control."""
        stream_label = format_task_log_id(rt.variant_id, rt.task.task_id, rt.replicate_index)
        task_callback = stream_callback_factory(stream_label) if stream_callback_factory else None
        async with semaphore:
            try:
                rt.run_dir.mkdir(parents=True, exist_ok=True)  # noqa: CE002 — mkdir on local FS is nanoseconds
                sandbox_cfg = rt.task.sandbox
                # Resolve the driver-derived preservation default HERE, where the
                # original driver is still visible (the in-container orchestrator
                # sees it forced to tempdir). Explicit --preservation-mode wins.
                driver = sandbox_cfg.driver if sandbox_cfg is not None else "tempdir"
                preservation_mode = resolve_preservation_mode(config.preservation_mode, driver)
                # Explicit DIRECT_WRITE on a non-docker host runs the sandbox under
                # run_dir, re-opening the parent-dir node_modules contamination the
                # host default (MOVE_ON_WRITE) exists to prevent (MST-9795).
                if config.preservation_mode == PreservationMode.DIRECT_WRITE and driver != "docker":
                    logger.warning(
                        "DIRECT_WRITE on driver=%s runs the sandbox under run_dir; parent-dir "
                        + "node_modules can contaminate Node tool resolution (MST-9795).",
                        driver,
                    )
                if sandbox_cfg is not None and sandbox_cfg.driver == "docker":
                    # Docker isolation: spawn one container per task, parse
                    # its task.json on completion. The in-container CLI
                    # serializes stream events as NDJSON on stdout; we
                    # forward them to the host callback so --stream
                    # renders identically to the in-process path.
                    from ..isolation.docker_runner import DockerRunner, regrade_on_host

                    result = await DockerRunner(
                        rt,
                        preservation_mode=preservation_mode,
                        stream_callback=task_callback,
                        verbose=config.verbose,
                    ).run()
                    # GRADE-OUTSIDE: the container ran the AGENT ONLY (it never
                    # received the criteria/reference/graders). Grade the
                    # copied-out artifacts on the host, where the full unstripped
                    # rt.task lives, via the evaluate-only re-grade path. No-ops
                    # for ungraded tasks or terminal agent-side failures.
                    result = await regrade_on_host(result, rt)
                    # The in-container _finalize_result can't emit task telemetry
                    # (connection-string env vars aren't forwarded into the
                    # container), so emit the Task.End event host-side here
                    # — keeping docker runs at parity with the in-process path.
                    # Emit AFTER the host re-grade so the telemetry status/score
                    # reflect the authoritative host grade, not the container's.
                    from ..orchestrator import build_task_event
                    from ..telemetry import track_event

                    name, props = build_task_event(result, driver="docker", variant_id=rt.variant_id)
                    track_event(name, props)
                else:
                    orchestrator = Orchestrator(
                        task=rt.task,
                        run_dir=rt.run_dir,
                        preservation_mode=preservation_mode,
                        task_file=rt.task_file,
                        stream_callback=task_callback,
                        variant_id=rt.variant_id,
                        source_yaml=rt.source_yaml,
                        config_lineage=rt.config_lineage,
                        replicate_index=rt.replicate_index,
                    )
                    result = await orchestrator.run()
                tr = TaskResult(
                    task_id=rt.task.task_id,
                    variant_id=rt.variant_id,
                    result=result,
                    duration=result.duration_seconds,
                    suite_id=rt.task.suite_id,
                    row_id=rt.task.row_id,
                    replicate_index=rt.replicate_index,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                tr = _create_error_task_result(
                    rt.task_file,
                    exc,
                    task_id=rt.task.task_id,
                    variant_id=rt.variant_id,
                    suite_id=rt.task.suite_id,
                    row_id=rt.task.row_id,
                    replicate_index=rt.replicate_index,
                )
            _safe_notify(on_task_complete, tr)
            return tr

    coroutines = [run_single(rt) for rt in resolved_tasks]
    results: list[TaskResult | BaseException] = await asyncio.gather(*coroutines, return_exceptions=True)

    # Re-raise fatal exceptions before processing results
    for result in results:
        if isinstance(result, (KeyboardInterrupt, SystemExit)):
            raise result

    processed: list[TaskResult] = []
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            rt = resolved_tasks[i]
            processed.append(
                _create_error_task_result(
                    rt.task_file,
                    result,
                    task_id=rt.task.task_id,
                    variant_id=rt.variant_id,
                    suite_id=rt.task.suite_id,
                    row_id=rt.task.row_id,
                    replicate_index=rt.replicate_index,
                )
            )
        else:
            processed.append(result)

    end_time = datetime.now()
    # Fold in any already-complete results (--resume) so run.json and all
    # downstream reports describe the whole run, not just this batch.
    all_results = [*(prior_results or []), *processed]
    summary = _generate_run_summary(
        config.run_dir,
        all_results,
        start_time,
        end_time,
        task_tags,
        task_paths=task_paths,
        max_parallel=config.max_parallel,
        skipped_tasks=skipped_tasks or [],
    )
    return summary, all_results


def _safe_notify(callback: Callable[[TaskResult], None] | None, result: TaskResult) -> None:
    """Invoke a progress callback, swallowing any exceptions so UI failures never affect task outcomes."""
    if callback is None:
        return
    try:
        callback(result)
    except Exception:
        logger.warning("Progress callback failed (ignored)", exc_info=True)


def _create_error_task_result(
    task_file: Path,
    error: BaseException,
    *,
    task_id: str | None = None,
    variant_id: str,
    suite_id: str | None = None,
    row_id: str | None = None,
    replicate_index: int = 0,
) -> TaskResult:
    """Create a TaskResult for a failed task.

    Args:
        task_file: Path to task file that failed.
        error: Exception that was raised.
        task_id: Explicit task ID; falls back to task_file.stem when unavailable.
        variant_id: Experiment variant ID.
        suite_id: Parent suite id (dataset expansion).
        row_id: Row id within the suite.
        replicate_index: Replicate index from ResolvedTask.

    Returns:
        TaskResult with error information.
    """
    error_type = type(error).__name__
    # A failed image build is an environment/setup failure — record it as
    # BUILD_FAILED (not generic ERROR) so reports/run.json distinguish it. The
    # import is lazy + scoped to this except-path use so batch stays import-light
    # and the non-docker path never imports the docker runner.
    from ..isolation.docker_runner import DockerBuildError

    is_build_failure = isinstance(error, DockerBuildError)
    status = FinalStatus.BUILD_FAILED if is_build_failure else FinalStatus.ERROR
    description = (
        "Docker image build failed" if is_build_failure else f"Failed to load task from {task_file}: {error_type}"
    )
    error_result = EvaluationResult(
        task_id=task_id if task_id is not None else task_file.stem,
        task_description=description,
        variant_id=variant_id,
        agent_type=AgentKind.UNKNOWN,
        started_at=datetime.now(),
        final_status=status,
        error_message=str(error),
        iteration_count=0,
        environment_info={},
    )
    return TaskResult(
        task_id=error_result.task_id,
        variant_id=error_result.variant_id,
        result=error_result,
        duration=0.0,
        suite_id=suite_id,
        row_id=row_id,
        replicate_index=replicate_index,
    )


def partition_for_resume(
    resolved_tasks: list[ResolvedTask],
) -> tuple[list[ResolvedTask], list[TaskResult], list[ResolvedTask]]:
    """Split resolved tasks into (to_run, prior_results, prior_resolved) for --resume.

    A task is already-complete when its task.json exists, parses, and carries a
    final_status. task.json is written atomically at end-of-run, so any parseable
    file with a status is a finished task (no partial-write ambiguity). Complete
    tasks are reloaded into TaskResults (to fold into run.json) and excluded from
    to_run; everything else — including failed-to-parse — re-runs.

    Args:
        resolved_tasks: Fully-resolved tasks for the whole run.

    Returns:
        (to_run, prior_results, prior_resolved):
          - to_run: tasks still needing execution
          - prior_results: reloaded results for already-complete tasks
          - prior_resolved: the ResolvedTask for each prior_result (same order)
    """
    to_run: list[ResolvedTask] = []
    prior_results: list[TaskResult] = []
    prior_resolved: list[ResolvedTask] = []
    for rt in resolved_tasks:
        tr = _load_completed_result(rt)
        if tr is None:
            to_run.append(rt)
        else:
            prior_results.append(tr)
            prior_resolved.append(rt)
    return to_run, prior_results, prior_resolved


def clear_rerun_artifacts(to_run: list[ResolvedTask]) -> int:
    """Remove stale ``artifacts/<task_id>`` dirs for tasks about to re-run under --resume.

    A task in ``to_run`` is non-finalized (``partition_for_resume`` excluded every
    finalized task), so it re-executes from scratch and any leftover artifacts are
    unwanted. Only DIRECT_WRITE writes into ``artifacts/<task_id>`` live (a container
    killed mid-run leaves partial files there); MOVE_ON_WRITE/NONE only create it at
    finalize, which a non-finalized task never reached — so a pre-existing dir here is
    always a stale DIRECT_WRITE partial. Clearing it prevents stale files from
    satisfying file-based criteria and skewing the resumed task's score. Host-side, so
    it covers both the docker bind-mount and the tempdir path. Returns the count cleared.
    """
    cleared = 0
    for rt in to_run:
        artifacts = rt.run_dir / "artifacts" / rt.task.task_id
        if artifacts.exists():
            shutil.rmtree(artifacts, ignore_errors=True)
            cleared += 1
    return cleared


def _load_completed_result(rt: ResolvedTask) -> TaskResult | None:
    """Reconstruct a TaskResult from a finalized task.json, or None if absent/incomplete."""
    report_path = rt.run_dir / "task.json"
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        result = EvaluationResult.model_validate_json(text)
    except ValueError:
        # Malformed JSON or schema mismatch (pydantic ValidationError subclasses
        # ValueError) → treat as not-yet-complete so the task re-runs.
        return None
    if not result.final_status:
        return None
    return TaskResult(
        task_id=rt.task.task_id,
        variant_id=rt.variant_id,
        result=result,
        duration=result.duration_seconds or 0.0,
        suite_id=rt.task.suite_id,
        row_id=rt.task.row_id,
        replicate_index=rt.replicate_index,
    )


def recover_task_results(run_dir: Path) -> list[TaskResult]:
    """Reconstruct ``TaskResult``s from every finalized ``task.json`` under ``run_dir``.

    The disk half of the run-summary seam: pairs with ``build_run_summary`` to
    (re)aggregate a finished run without re-executing it. Each ``task.json`` is the
    atomically-written ``EvaluationResult`` for one task, so a file that is missing,
    unparseable, or carries no ``final_status`` is skipped as not-yet-finalized
    (mirrors ``_load_completed_result``). ``task_id`` and ``variant_id`` come from
    the result itself; ``replicate_index`` is recovered from the
    ``<variant_id>/<task_id>/<NN>/`` layout (``build_task_run_dir``). ``suite_id`` /
    ``row_id`` are not stored in ``task.json`` and are left ``None`` — they feed
    suite rollups, not ``run.json``.

    Results are sorted by ``(variant_id, task_id, replicate_index)`` so the rebuilt
    ``run.json`` ordering is deterministic, independent of filesystem walk order.

    A ``task.json`` that lives under a *nested* run dir — a subdirectory carrying its
    own ``run.json`` — belongs to that sub-run, not this one, and is excluded so a
    parent summary never absorbs a nested suite's tasks. Base runs have no nesting;
    this only matters for composed layouts that stack sub-runs under one tree.
    """
    nested_roots = [p.parent for p in run_dir.rglob("run.json") if p.parent != run_dir]
    recovered: list[TaskResult] = []
    for task_json in run_dir.rglob("task.json"):
        if any(root in task_json.parents for root in nested_roots):
            continue  # belongs to a nested sub-run (its own run.json), not this one
        try:
            result = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Unreadable / malformed / schema-mismatched. Skip so one corrupt file can't
            # abort the rebuild, but warn — silently dropping it would shrink tasks_run
            # (numerator and denominator) with no signal that a task was lost.
            logger.warning("skipping unreadable/malformed task.json %s: %s", task_json, exc)
            continue
        if not result.final_status:
            # Written but incomplete (no terminal status) → skip, same as --resume.
            continue
        # The leaf dir is the zero-padded replicate index (build_task_run_dir);
        # tolerate non-standard layouts by falling back to replicate 0.
        leaf = task_json.parent.name
        replicate_index = int(leaf) if leaf.isdigit() else 0
        recovered.append(
            TaskResult(
                task_id=result.task_id,
                variant_id=result.variant_id,
                result=result,
                duration=result.duration_seconds or 0.0,
                replicate_index=replicate_index,
            )
        )
    recovered.sort(key=lambda r: (r.variant_id, r.task_id, r.replicate_index))
    return recovered


# --- resume config fingerprint ------------------------------------------------
# The per-task path key (variant_id/task_id/NN) does NOT encode result-affecting
# run config like the model or backend. So --resume, which matches finalized tasks
# purely by that path, would otherwise fold results produced under a *different*
# config into the new run (e.g. resuming a Sonnet run with --model opus keeps the
# Sonnet results for already-finalized tasks). We stamp the config on every run and
# warn (don't refuse) when a resume's config differs (in _run_with_experiment) so
# the resulting mixed-config run.json is surfaced rather than silent.
RESUME_FINGERPRINT_FILE = "resume_fingerprint.json"


def compute_run_fingerprint(
    config: BatchRunConfig,
    experiment_id: str,
    backend: str,
    bedrock_model: str | None,
) -> dict[str, object]:
    """Snapshot the run config for the --resume drift warning.

    Dumps the whole config plus the model-selection context that lives outside it.
    Best-effort and informational only — any difference on resume produces a
    warning (resume always proceeds), so this is intentionally not exhaustive about
    which keys are "result-affecting". A benign diff (e.g. --max-parallel) just
    warns harmlessly. mode="json" yields JSON-comparable scalars that match what is
    written to / read back from disk.
    """
    return config.model_dump(mode="json") | {
        "experiment_id": experiment_id,
        "backend": backend,
        "bedrock_model": bedrock_model,
    }


def write_run_fingerprint(run_dir: Path, fingerprint: dict[str, object]) -> None:
    """Stamp the run config at the run root for a future --resume to validate against."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / RESUME_FINGERPRINT_FILE).write_text(json.dumps(fingerprint, indent=2, sort_keys=True), encoding="utf-8")


def read_run_fingerprint(run_dir: Path) -> dict[str, object] | None:
    """Load a prior run's config stamp, or None if absent/unreadable (e.g. a pre-feature run).

    A stamp that parses to a non-object (a bare number/string/list from external
    corruption) is folded into the tolerated missing-stamp path rather than reaching
    fingerprint_diff, where a non-dict would raise TypeError or silently no-op the guard.
    """
    try:
        data = json.loads((run_dir / RESUME_FINGERPRINT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def fingerprint_diff(prior: dict[str, object], current: dict[str, object]) -> dict[str, tuple[object, object]]:
    """Keys present in BOTH stamps that disagree, as ``{key: (prior, current)}``.

    Only keys present in ``prior`` are compared, so adding fingerprint fields in a
    later version never false-flags a resume of an older run.
    """
    return {k: (prior[k], current[k]) for k in current if k in prior and prior[k] != current[k]}


def _override_uip_versions_from_tasks(version_info: dict[str, Any], task_results: list[TaskResult]) -> None:
    """Replace host-captured uip versions with the per-task (container) truth.

    ``cli_version`` and ``tool_plugins`` in ``version_info`` were resolved on
    the machine running this process; the tasks ran inside containers that
    auto-install the latest alpha tool plugins on first use, so only the
    per-task ``environment_info`` (captured in-container, post-task) describes
    what actually executed. Mutates ``version_info`` in place: each key is
    overridden by the consensus across tasks; when tasks disagree (e.g. an
    alpha published mid-run) the sorted distinct versions are joined with
    ``" | "`` and a warning is logged. Host values are kept only when no task
    reported one — for ``cli_version`` that means no value besides ``""`` /
    ``"unknown"``, and for ``tool_plugins`` no non-empty plugin entry at all
    (legacy results, or every task errored before env capture).
    """
    # Defence-in-depth alongside the chokepoint fix in _uip_version: filter to
    # version-shaped strings so junk already on disk (older runs captured a
    # CLI that printed a JSON envelope instead of a version) can't leak into
    # the aggregated chip when those results are re-summarised on --resume.
    cli_versions = sorted(
        {
            v
            for r in task_results
            if (env := r.result.environment_info) and looks_like_version(v := env.get("cli_version"))
        }
    )
    if cli_versions:
        if len(cli_versions) > 1:
            logger.warning("cli_version drifted across task containers: %s", cli_versions)
        version_info["cli_version"] = " | ".join(cli_versions)

    plugin_versions: dict[str, set[str]] = {}
    for r in task_results:
        plugins = (r.result.environment_info or {}).get("tool_plugins")
        if not isinstance(plugins, dict):
            continue
        for name, plugin_version in plugins.items():
            if isinstance(plugin_version, str) and plugin_version:
                plugin_versions.setdefault(name, set()).add(plugin_version)
    # Gate on collected entries, not on "some task had a tool_plugins dict":
    # an all-empty consensus ({} from every task) must not stomp the host
    # fallback — symmetric with the ""/"unknown" filter on cli_version above.
    if plugin_versions:
        drifted = {name: sorted(versions) for name, versions in plugin_versions.items() if len(versions) > 1}
        if drifted:
            logger.warning("tool_plugins drifted across task containers: %s", drifted)
        version_info["tool_plugins"] = {
            name: " | ".join(sorted(versions)) for name, versions in sorted(plugin_versions.items())
        }


def _generate_run_summary(
    run_dir: Path,
    task_results: list[TaskResult],
    start_time: datetime,
    end_time: datetime,
    task_tags: dict[str, list[str]] | None = None,
    *,
    task_paths: dict[str, str] | None = None,
    max_parallel: int = 1,
    skipped_tasks: list[SkippedTask] | None = None,
) -> RunSummary:
    """Generate run-level summary from batch results.

    Args:
        run_dir: Run directory path.
        task_results: List of typed task results.
        start_time: Batch start time.
        end_time: Batch end time.
        task_tags: Optional mapping of task_id -> tags.
        task_paths: Optional mapping of task_id -> source YAML path (string).
        skipped_tasks: Task YAMLs that failed to load upstream.

    Returns:
        RunSummary with aggregated statistics.
    """
    summary = build_run_summary(
        run_dir.name,
        task_results,
        start_time,
        end_time,
        task_tags,
        task_paths=task_paths,
        max_parallel=max_parallel,
        skipped_tasks=skipped_tasks,
    )
    write_run_summary(summary, run_dir)
    return summary


def build_run_summary(
    run_id: str,
    task_results: list[TaskResult],
    start_time: datetime,
    end_time: datetime,
    task_tags: dict[str, list[str]] | None = None,
    *,
    task_paths: dict[str, str] | None = None,
    max_parallel: int = 1,
    skipped_tasks: list[SkippedTask] | None = None,
) -> RunSummary:
    """Aggregate task results into a ``RunSummary`` — pure, no disk I/O.

    The run-summary builder, decoupled from execution so a run can be summarised
    from any source of ``TaskResult``s: a live batch (``run_batch``), results
    recovered from finalized ``task.json`` files on disk (``recover_task_results``),
    or a combination of the two (e.g. splicing the slices of a split run). Pairs
    with ``write_run_summary`` for the persist half.

    Status buckets use the canonical ``FinalStatus.category`` mapping and the version
    chip is reconciled from the per-task (in-container) captures, so callers never
    re-implement either — the single source of truth for run-level aggregation.
    """
    statuses = [r.result.final_status for r in task_results]

    version_info = get_version_info()
    # The run-level cli/tool versions must describe what the tasks executed,
    # not this process's host installs: under --driver docker each task runs
    # in its own container, which auto-installs the latest alpha tool plugins
    # at first use — the host's `uip` tree can differ arbitrarily (#366
    # recorded host values by mistake). Aggregate the per-task (in-container)
    # captures; host values survive only as a fallback when no task reported.
    _override_uip_versions_from_tasks(version_info, task_results)
    host_coder_eval = version_info.get("coder_eval", "unknown")
    # Surface host↔container version drift: under --driver docker the agent
    # ran against the image's version, not the host's. Without this warning
    # framework_version silently mis-attributes the runtime.
    container_versions = {
        (r.result.environment_info or {}).get("coder_eval") for r in task_results if r.result.environment_info
    }
    container_versions.discard(None)
    container_versions.discard(host_coder_eval)
    if container_versions:
        logger.warning(
            "Container coder_eval %s != host %s; framework_version is host. Per-task versions in task.json.",
            sorted(v for v in container_versions if v),
            host_coder_eval,
        )
    return RunSummary(
        run_id=run_id,
        start_time=start_time,
        end_time=end_time,
        total_duration_seconds=(end_time - start_time).total_seconds(),
        tasks_run=len(task_results),
        tasks_succeeded=sum(1 for s in statuses if s.category == "succeeded"),
        tasks_failed=sum(1 for s in statuses if s.category == "failed"),
        tasks_error=sum(1 for s in statuses if s.category == "error"),
        tasks_token_budget_exceeded=sum(1 for s in statuses if s == FinalStatus.TOKEN_BUDGET_EXCEEDED),
        tasks_cost_budget_exceeded=sum(1 for s in statuses if s == FinalStatus.COST_BUDGET_EXCEEDED),
        skipped_tasks=skipped_tasks or [],
        max_parallel=max_parallel,
        task_results=[
            eval_result_to_task_dict(
                r.result,
                variant_id=r.variant_id,
                tags=(task_tags or {}).get(r.task_id, []),
                task_path=(task_paths or {}).get(r.task_id),
                duration_override=r.duration,
                replicate_index=r.replicate_index,
            )
            for r in task_results
        ],
        framework_version=version_info.get("coder_eval", "unknown"),
        environment_info=version_info,
    )


def write_run_summary(summary: RunSummary, run_dir: Path) -> None:
    """Persist a ``RunSummary`` to ``run_dir``: ``run.json`` + the ``run.md`` report.

    The persist half of the run-summary seam (see ``build_run_summary``). ``run.md``
    carries command statistics rendered from the per-task data under ``run_dir``.
    """
    from ..reports import ReportGenerator

    # Create run directory first to eliminate race condition
    run_dir.mkdir(parents=True, exist_ok=True)

    # run.json — run-level summary (distinct from experiment.json from ExperimentReportGenerator)
    (run_dir / "run.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")

    # run.md — command statistics
    report_md = ReportGenerator.generate_markdown(summary, run_dir=run_dir)
    (run_dir / "run.md").write_text(report_md, encoding="utf-8")


def filter_tasks_by_tags(
    tasks: list[tuple[Path, TaskDefinition]],
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
) -> list[tuple[Path, TaskDefinition]]:
    """Filter tasks by tag inclusion/exclusion (OR logic).

    Args:
        tasks: List of (task_file, task_definition) tuples.
        include_tags: If set, only keep tasks matching ANY of these tags.
        exclude_tags: If set, remove tasks matching ANY of these tags.

    Returns:
        Filtered list of (task_file, task_definition) tuples.
    """
    result = tasks
    if include_tags:
        result = [(p, t) for p, t in result if include_tags & set(t.tags)]
        skipped = len(tasks) - len(result)
        if skipped:
            logger.info("Tag filter: included %d/%d tasks (tags: %s)", len(result), len(tasks), ", ".join(include_tags))
    if exclude_tags:
        before = len(result)
        result = [(p, t) for p, t in result if not (exclude_tags & set(t.tags))]
        skipped = before - len(result)
        if skipped:
            logger.info("Tag filter: excluded %d tasks (tags: %s)", skipped, ", ".join(exclude_tags))
    return result
