"""Reference-loading helpers for the orchestrator.

Loads the reference solution (code / file / directory form) consumed by the
``reference_comparison``, ``llm_judge``, and ``agent_judge`` criteria.
"""

import logging
from pathlib import Path

from ..models import TaskDefinition


logger = logging.getLogger(__name__)


def load_reference(
    task: TaskDefinition,
    task_file: Path | None,
    cached_reference: str | None,
) -> tuple[str | None, Path | None, str | None]:
    """Load reference solution from task definition.

    Returns the reference in whichever form the task declared:
    ``code`` / ``file`` produce a string ``reference_code``; ``directory``
    produces a resolved ``reference_dir`` ``Path``. At most one is non-None
    on any given call.

    Args:
        task: Task definition with reference configuration
        task_file: Path to task YAML file (for resolving relative paths)
        cached_reference: Previously loaded ``reference_code`` (for caching).
            Directory paths are resolved fresh each call — path resolution
            is cheap and the directory contents are read by the consumer.

    Returns:
        Tuple of (reference_code, reference_dir, cached_reference).
        ``cached_reference`` should be stored for future calls (string forms only).

    Raises:
        FileNotFoundError: if the reference file or directory doesn't exist.
        ValueError: if ``task_file`` is not provided when needed for path resolution.

    Security: the reference is NEVER shown to the agent. It is only consumed
    by ``llm_judge`` / ``agent_judge`` and ``reference_comparison`` criteria.
    """
    # String-form cache short-circuit. Directory form is resolved fresh below
    # because Path resolution is nanoseconds and directory content varies.
    if cached_reference is not None:
        return cached_reference, None, cached_reference

    if not task.reference:
        return None, None, None

    reference_code: str | None = None
    reference_dir: Path | None = None

    if task.reference.code:
        reference_code = task.reference.code
    elif task.reference.file:
        if not task_file:
            raise ValueError("task_file not set, cannot resolve reference file path")
        ref_path = task_file.parent / task.reference.file
        if not ref_path.exists():
            raise FileNotFoundError(f"Reference file not found: {ref_path} (specified in {task_file})")
        reference_code = ref_path.read_text(encoding="utf-8")
    elif task.reference.directory:
        if not task_file:
            raise ValueError("task_file not set, cannot resolve reference directory path")
        ref_dir = (task_file.parent / task.reference.directory).resolve()
        if not ref_dir.is_dir():
            raise FileNotFoundError(f"Reference directory not found: {ref_dir} (specified in {task_file})")
        reference_dir = ref_dir

    # Log that reference was loaded (but NOT the content for security)
    logger.info("Reference solution loaded (content hidden for security)")
    return reference_code, reference_dir, reference_code


# Backward-compat alias — orchestrator.py still imports this name in places we
# didn't yet update. New code should use ``load_reference``.
def load_reference_code(
    task: TaskDefinition,
    task_file: Path | None,
    cached_reference: str | None,
) -> tuple[str | None, str | None]:
    """Compatibility wrapper around ``load_reference`` returning the legacy two-tuple.

    Use ``load_reference`` directly when you also need the directory path
    (i.e. anywhere agent_judge can run). Kept so existing call sites that
    only consume the string form (``reference_comparison``,
    pre-directory-feature paths) don't have to change.
    """
    code, _dir, cache = load_reference(task=task, task_file=task_file, cached_reference=cached_reference)
    return code, cache
