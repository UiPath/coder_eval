"""Reference-loading helpers for the orchestrator.

Resolves and stages the reference solution directory consumed by the
``reference_comparison``, ``llm_judge``, and ``agent_judge`` criteria.

The reference is always a *directory* (``task.reference.directory``, relative
to the task YAML). The orchestrator stages a per-run private copy of it rather
than pointing criteria at the checked-out path, for two reasons:

1. **Concurrency.** A batch run fans many tasks out over the same
   ``tasks/<name>/`` tree. The anti-cheat window chmods the reference to 000
   for the duration of each agent turn; doing that to the shared checkout would
   let one task's turn block a sibling task's judge mid-read.
2. **Blast radius.** A private copy means a crashed run can only leave a
   throwaway directory at mode 000, never the user's working tree.
"""

import logging
import os
import shutil
from pathlib import Path

from ..models import CONTAINER_REFERENCE_DIR, TaskDefinition
from ..path_utils import ignore_patterns_and_symlinks


logger = logging.getLogger(__name__)


def resolve_reference_dir(task: TaskDefinition, task_file: Path | None) -> Path | None:
    """Resolve ``task.reference.directory`` against the task YAML's directory.

    Args:
        task: Task definition with reference configuration.
        task_file: Path to the task YAML file (for resolving the relative path).

    Returns:
        The resolved source directory, or ``None`` when the task declares no
        reference.

    Raises:
        FileNotFoundError: if the reference directory doesn't exist.
        ValueError: if ``task_file`` is not provided when needed for resolution.
    """
    if not task.reference:
        return None

    # Under driver: docker the host bind-mounts the reference at a fixed container
    # path and layers an empty tmpfs over its original location inside the
    # task-dir mount, so the agent cannot reach it via $TASK_DIR. Resolving
    # relative to task_file would therefore find that empty mask, not the
    # solution — so the container mount wins whenever it is present.
    #
    # Gated on the env var AS WELL AS the path, and for the same reason
    # Sandbox.enforces_permission_windows is: a bare `/work/references` probe
    # silently hijacks every task's reference on any host that happens to have
    # that directory (a Linux box using /work as a workspace root is entirely
    # plausible, and this package is going open-source). The failure would be
    # invisible — wrong reference content, wrong reference_comparison scores,
    # wrong judge prompts, no error.
    container_mount = Path(CONTAINER_REFERENCE_DIR)
    if os.environ.get("CODER_EVAL_IN_CONTAINER") == "1":
        if container_mount.is_dir():
            logger.debug("Reference resolved from the container mount at %s", container_mount)
            return container_mount
        # Hard fail rather than falling back to task_file.parent. In-container
        # that fallback resolves to the UN-masked reference under the `:ro`
        # task-dir bind — which the mode-000 window then cannot chmod (EROFS), so
        # the run would complete with the solution readable by the agent for the
        # whole turn, reporting a normal pass/fail. A missing mount means the
        # host-side wiring is broken; that must be loud, not silently unprotected.
        raise FileNotFoundError(
            f"Task declares a reference but {CONTAINER_REFERENCE_DIR} is not mounted in this container. "
            + "The host-side DockerRunner should have mounted it; refusing to run unprotected. "
            + "If the coder-eval-agent image predates the reference mount, rebuild it (`make docker-image`)."
        )

    if not task_file:
        raise ValueError("task_file not set, cannot resolve reference directory path")
    ref_dir = (task_file.parent / task.reference.directory).resolve()
    if not ref_dir.is_dir():
        raise FileNotFoundError(
            f"Reference directory not found: {ref_dir} (specified in {task_file}). "
            + "reference.directory must name a directory relative to the task YAML."
        )
    return ref_dir


def stage_reference_dir(source: Path, destination: Path) -> Path:
    """Copy the reference solution into a per-run private ``destination``.

    Symlinks are NOT followed: a reference bundle that ships
    ``creds -> ~/.aws/credentials`` must not pull host files into a location a
    judge sub-agent can read. An existing ``destination`` is cleared first so a
    reused ``--run-dir`` cannot blend a previous run's reference into this one.

    Returns the staged destination path.
    """
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=ignore_patterns_and_symlinks([".git"]))
    # Log that the reference was staged, but never its contents.
    logger.info("Reference solution staged (content hidden for security)")
    return destination
