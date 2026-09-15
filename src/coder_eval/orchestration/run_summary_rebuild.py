"""Rebuild a run's ``run.json`` + ``run.md`` from the finalized ``task.json`` files on disk."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..models import RunSummary, SkippedTask, TaskResult
from .batch import build_run_summary, recover_task_results, write_run_summary


logger = logging.getLogger(__name__)


def rebuild_run_summary(run_dir: Path) -> RunSummary | None:
    """(Re)build and write ``run_dir``'s run-level ``run.json`` + ``run.md`` in place. Prints nothing.

    Aggregates the rows ``recover_task_results`` assigns to ``run_dir`` with the builder a
    live run uses. Static metadata (tags, source paths, skipped tasks, the run window,
    ``max_parallel``) is carried from an existing ``run.json``, which is untrusted: a
    malformed entry is dropped with a warning. Per-suite and experiment rollups are not
    rebuilt, because the grouping they need is not recoverable from ``task.json`` alone.

    Returns:
        The written summary, or ``None`` when ``run_dir`` holds no finalized ``task.json``.
    """
    results = recover_task_results(run_dir)
    if not results:
        return None
    task_tags, task_paths, prior = _read_prior_metadata(run_dir)
    start_time, end_time = _resolve_window(results, prior)
    summary = build_run_summary(
        run_dir.resolve().name,
        results,
        start_time,
        end_time,
        task_tags,
        task_paths=task_paths,
        max_parallel=int(prior.get("max_parallel", 1) or 1),
        skipped_tasks=_recover_skipped_tasks(prior),
    )
    write_run_summary(summary, run_dir)
    return summary


def find_run_root(path: Path) -> Path | None:
    """The nearest directory at or above ``path`` holding a ``run.json``, or ``None``.

    ``path`` is resolved first, so a relative path walks past the working directory. The
    inverse of ``recover_task_results``' rule that the nearest ``run.json`` owns a row.
    """
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "run.json").is_file():
            return candidate
    return None


def _read_prior_metadata(run_dir: Path) -> tuple[dict[str, list[str]], dict[str, str], dict[str, Any]]:
    """Per-task tags and source paths plus the run-level fields of an existing ``run.json``.

    Returns ``({}, {}, {})`` when there is no readable ``run.json``.
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
    """The prior ``run.json``'s skipped tasks; a malformed entry is dropped, never fatal."""
    recovered: list[SkippedTask] = []
    for entry in prior.get("skipped_tasks", []):
        if not isinstance(entry, dict):
            continue
        try:
            recovered.append(SkippedTask.model_validate(entry))
        except ValidationError:
            logger.warning("Dropping malformed skipped_tasks entry from prior run.json: %s", entry)
    return recovered


def _resolve_window(results: list[TaskResult], prior: dict[str, Any]) -> tuple[datetime, datetime]:
    """The run's ``(start, end)``: the prior ``run.json``'s timestamps, else derived from ``results``.

    ``results`` must be non-empty.
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
