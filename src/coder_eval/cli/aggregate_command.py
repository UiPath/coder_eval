"""Aggregate command — (re)build run.json + run.md from finalized task.json files.

The standalone aggregation step: where `coder-eval run` folds the live batch into
run.json/run.md at the end of execution, this re-aggregates the same artifacts
afterwards from the finalized task.json files already on disk — for a run dir
whose top-level run.json is missing or stale (e.g. after recovering or combining
run dirs). It reuses the exact builder a live run uses (`build_run_summary`), so
the counts and version chip are identical.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from ..models import SkippedTask, TaskResult
from .console import console


def aggregate_command(
    run_dir: Path = typer.Argument(  # noqa: B008
        ...,
        help="Run directory holding finalized task.json files (e.g. runs/2026-06-22_14-32-27).",
        exists=True,
        file_okay=False,
    ),
    output_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Directory to write run.json/run.md into instead of run_dir (e.g. a merged output dir).",
        file_okay=False,
    ),
) -> None:
    """(Re)build run.json + run.md by aggregating the finalized task.json files under a run dir.

    Rebuilds the run-level summary only. Per-suite rollups (suite.json/suite.md) and
    experiment reports (experiment.json/experiment.md) that a live ``run`` produces are
    NOT rebuilt — the per-row suite/variant grouping they need is not recoverable from
    task.json alone.

    Examples:
        # Rebuild a run's summary in place
        coder-eval aggregate runs/2026-06-22_14-32-27

        # Aggregate a combined dir's task results into a fresh summary
        coder-eval aggregate runs/combined -o runs/combined
    """
    from ..orchestration.batch import build_run_summary, recover_task_results, write_run_summary

    results = recover_task_results(run_dir)
    if not results:
        console.print(f"[red]Error: no finalized task.json files found under {run_dir}[/red]")
        console.print("\n[dim]Hint: this aggregates a finished run — use 'coder-eval run' to create one.[/dim]")
        raise typer.Exit(1)

    out_dir = output_dir or run_dir

    # tags / source-path / run-level fields are inputs (static task metadata), not
    # results, so carry them from an existing run.json when present — reading them
    # from a stale summary is safe. Absent → empty maps + a window derived from the
    # recovered results.
    task_tags, task_paths, prior = _read_prior_metadata(run_dir)
    start_time, end_time = _resolve_window(results, prior)
    skipped = _recover_skipped_tasks(prior)

    summary = build_run_summary(
        out_dir.name,
        results,
        start_time,
        end_time,
        task_tags,
        task_paths=task_paths,
        max_parallel=int(prior.get("max_parallel", 1) or 1),
        skipped_tasks=skipped,
    )
    write_run_summary(summary, out_dir)
    console.print(
        f"[green][OK][/green] Aggregated {summary.tasks_run} task(s) "
        f"({summary.tasks_succeeded} ok / {summary.tasks_failed} fail / {summary.tasks_error} err) "
        f"→ {out_dir / 'run.json'}"
    )
    console.print(
        "[dim]Note: run-level summary only — per-suite (suite.json/suite.md) and "
        "experiment (experiment.json/experiment.md) rollups are not rebuilt.[/dim]"
    )


def _read_prior_metadata(run_dir: Path) -> tuple[dict[str, list[str]], dict[str, str], dict[str, Any]]:
    """Pull per-task tags/source-paths + run-level fields from an existing run.json.

    Returns ``(task_tags, task_paths, run_meta)`` — empty maps and ``{}`` when there
    is no readable run.json (a fresh dir, or one assembled purely from task.json).
    """
    try:
        prior = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}, {}
    if not isinstance(prior, dict):
        return {}, {}, {}
    task_tags: dict[str, list[str]] = {}
    task_paths: dict[str, str] = {}
    for row in prior.get("task_results", []):
        if not isinstance(row, dict):
            continue
        task_id = row.get("task_id")
        if not task_id:
            continue
        if isinstance(row.get("tags"), list):
            task_tags[task_id] = row["tags"]
        if isinstance(row.get("task_path"), str):
            task_paths[task_id] = row["task_path"]
    return task_tags, task_paths, prior


def _recover_skipped_tasks(prior: dict[str, Any]) -> list[SkippedTask]:
    """Reconstruct the skipped-task carry-over from a prior run.json, per entry.

    The prior summary is untrusted (possibly stale / hand-edited / older-schema), so a
    malformed entry must drop rather than abort the rebuild — mirroring the rest of
    ``_read_prior_metadata``'s degrade-to-empty stance.
    """
    recovered: list[SkippedTask] = []
    for entry in prior.get("skipped_tasks", []):
        if not isinstance(entry, dict):
            continue
        try:
            recovered.append(SkippedTask.model_validate(entry))
        except ValidationError:
            console.print(f"[yellow]Dropping malformed skipped_tasks entry from prior run.json: {entry}[/yellow]")
    return recovered


def _resolve_window(results: list[TaskResult], prior: dict[str, Any]) -> tuple[datetime, datetime]:
    """Determine the run's (start, end) for total_duration_seconds.

    Prefer the prior run.json's timestamps (the real wall-clock); otherwise derive a
    best-effort window from the recovered results' start times + durations. ``results``
    is always non-empty here (the command errors out earlier on an empty recovery) and
    ``EvaluationResult.started_at`` is a required field, so a start time always exists.
    """
    start_raw, end_raw = prior.get("start_time"), prior.get("end_time")
    if isinstance(start_raw, str) and isinstance(end_raw, str):
        try:
            return datetime.fromisoformat(start_raw), datetime.fromisoformat(end_raw)
        except ValueError:
            pass
    start = min(r.result.started_at for r in results)
    end = max(r.result.started_at + timedelta(seconds=r.duration) for r in results)
    return start, max(end, start)
