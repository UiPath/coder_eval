"""Validation of LLM-generated task dicts against the TaskDefinition schema."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from coder_eval.models import TaskDefinition


logger = logging.getLogger(__name__)


def validate_tasks(raw_tasks: list[dict[str, Any]]) -> tuple[list[TaskDefinition], list[tuple[int, str]]]:
    """Parse and validate raw task dicts produced by the generator.

    Args:
        raw_tasks: List of raw dicts from generate_tasks()

    Returns:
        (valid_tasks, errors) where errors is a list of (index, error_message) tuples
    """
    valid: list[TaskDefinition] = []
    errors: list[tuple[int, str]] = []

    for i, raw in enumerate(raw_tasks):
        task_id = raw.get("task_id", f"<task {i + 1}>")
        try:
            task = TaskDefinition.model_validate(raw)
            valid.append(task)
            logger.debug("Task %r validated OK", task_id)
        except ValidationError as exc:
            msg = _format_validation_error(exc)
            logger.warning("Task %r failed validation: %s", task_id, msg)
            errors.append((i, f"{task_id}: {msg}"))

    return valid, errors


def _format_validation_error(exc: ValidationError) -> str:
    """Return a compact single-line summary of a Pydantic ValidationError."""
    lines = []
    for error in exc.errors():
        loc = " -> ".join(str(p) for p in error["loc"])
        lines.append(f"{loc}: {error['msg']}")
    return "; ".join(lines)
