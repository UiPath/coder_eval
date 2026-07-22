"""Pytest configuration and shared fixtures."""

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def disable_telemetry_by_default():
    """Hard-disable usage telemetry for every test (prevents prod pollution).

    A developer's local ``.env`` carries a real ``APPLICATIONINSIGHTS_CONNECTION_STRING``,
    and several CLI tests invoke the real Typer app, whose callback calls
    ``init_telemetry()``. Without this guard, ``make test`` would emit real
    ``CoderEval.*`` customEvents to the production App Insights resource. We flip
    the canonical disable gate AND clear the connection string so
    ``init_telemetry()`` no-ops on the ``not enabled or not connection_string``
    early return.

    Tests that specifically exercise telemetry re-enable it in-body via their own
    ``monkeypatch`` (e.g. ``test_telemetry.enabled_settings``), which is applied
    after this autouse fixture and therefore wins for the duration of that test.
    """
    from coder_eval.config import settings

    original_enabled = settings.telemetry_enabled
    original_conn = settings.telemetry_connection_string
    settings.telemetry_enabled = False
    settings.telemetry_connection_string = None
    try:
        yield
    finally:
        settings.telemetry_enabled = original_enabled
        settings.telemetry_connection_string = original_conn


@pytest.fixture(autouse=True)
def reset_logging_after_test():
    """Reset logging configuration after each test to prevent test pollution.

    This ensures that tests calling setup_logging() don't affect subsequent tests
    by restoring the logger's propagate flag and clearing handlers.
    """
    yield

    # Cleanup after test
    app_logger = logging.getLogger("coder_eval")

    # Restore propagate flag for pytest's caplog compatibility
    app_logger.propagate = True

    # Clear all handlers
    app_logger.handlers.clear()

    # Reset to default level
    app_logger.setLevel(logging.NOTSET)


@pytest.fixture
def write_run_json() -> Callable[..., Path]:
    """Factory that writes a valid ``run.json`` (a real ``RunSummary``) into a run dir.

    Shared by the JUnit writer tests (Phase 1) and the CLI report/run tests
    (Phase 2) so both track the ``RunSummary`` model instead of hand-typed JSON.
    Task-status counts (``tasks_succeeded``/``failed``/``error``) are computed
    from each row's ``status`` via ``FinalStatus.category`` so the model's
    task-count invariant always holds. Callers pass row dicts shaped like
    ``eval_result_to_task_dict`` output; only ``task_id`` and ``status`` are
    required per row.
    """
    from coder_eval.models import FinalStatus, RunSummary, SkippedTask

    def _build(
        run_dir: Path,
        rows: list[dict[str, Any]],
        *,
        skipped: list[tuple[str, str]] | None = None,
        run_id: str = "2026-07-21_12-00-00",
    ) -> Path:
        counts = {"succeeded": 0, "failed": 0, "error": 0}
        for row in rows:
            try:
                category = FinalStatus(row.get("status")).category
            except ValueError:
                category = "error"  # unknown status → error bucket (mirrors the writer)
            counts[category] += 1
        summary = RunSummary(
            run_id=run_id,
            start_time=datetime(2026, 7, 21, 12, 0, 0),
            end_time=datetime(2026, 7, 21, 12, 5, 0),
            total_duration_seconds=300.0,
            tasks_run=len(rows),
            tasks_succeeded=counts["succeeded"],
            tasks_failed=counts["failed"],
            tasks_error=counts["error"],
            skipped_tasks=[SkippedTask(path=p, reason=r) for p, r in (skipped or [])],
            task_results=rows,
            framework_version="test",
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return run_dir / "run.json"

    return _build
