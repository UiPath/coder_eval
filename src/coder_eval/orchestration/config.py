"""Configuration models for orchestration."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from coder_eval.models import PreservationMode
from coder_eval.path_utils import (
    DEFAULT_ARTIFACTS_DIR_TEMPLATE,
    DEFAULT_LOGGING_DIR_TEMPLATE,
    resolve_dir_template,
)


def resolve_preservation_mode(explicit: PreservationMode | None, driver: str) -> PreservationMode:
    """Resolve the effective preservation mode for a task.

    An explicit ``--preservation-mode`` always wins. Otherwise the default is
    driver-derived: ``docker`` → ``DIRECT_WRITE`` (isolated container; writing
    straight to the bind-mounted artifacts dir avoids a cross-mount copy),
    every other driver → ``MOVE_ON_WRITE`` (keeps the run off ``run_dir`` so
    parent-dir ``node_modules`` can't contaminate Node tool resolution).

    This must be called where the *original* driver is known (the dispatch seam
    in ``batch.run_single``); the in-container orchestrator sees the driver
    forced to ``tempdir`` and would otherwise mis-resolve docker → MOVE.
    """
    if explicit is not None:
        return explicit
    return PreservationMode.DIRECT_WRITE if driver == "docker" else PreservationMode.MOVE_ON_WRITE


class BatchRunConfig(BaseModel):
    """Configuration for batch task execution.

    This configuration object encapsulates all parameters needed to run
    multiple tasks in batch mode with optional parallelism.
    """

    model_config = ConfigDict(extra="forbid")

    run_dir: Path = Field(description="Directory for this batch run")
    max_parallel: int = Field(default=1, ge=1, description="Max concurrent tasks")
    preservation_mode: PreservationMode | None = Field(
        default=None,
        description=(
            "How to persist each task's sandbox. None = driver-derived default "
            "(docker → DIRECT_WRITE, else MOVE_ON_WRITE), resolved per-task at dispatch."
        ),
    )
    include_tags: set[str] | None = Field(default=None, description="Only run tasks matching any of these tags")
    exclude_tags: set[str] | None = Field(default=None, description="Skip tasks matching any of these tags")
    include_skipped: bool = Field(
        default=False,
        description=(
            "Run tasks marked `skip: true` in their YAML instead of quarantining them. "
            "Off by default so the nightly/CI keep excluding skipped tasks; pass "
            "--include-skipped for on-demand / local runs of quarantined or opt-in tasks."
        ),
    )

    # A dedicated field because it requires re-parsing the discriminated union,
    # not a simple field-merge; apply_overrides injects it into the agent patch.
    agent_type: str | None = Field(default=None, description="Override agent type for all tasks (e.g., 'claude-code')")

    # Layer-5 overrides, built from -D/--set plus the surviving flag aliases.
    # Rationale: .claude/notes/orchestration.md § Config merging and CLI overrides
    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Generic layer-5 task-config overrides (dotted path -> typed value) "
            "from -D/--set and the surviving flag aliases (--model, --driver)."
        ),
    )

    max_rows: int | None = Field(
        default=None,
        ge=1,
        description="Cap rows per dataset-backed task to first N. Non-dataset tasks unaffected.",
    )
    sample_per_stratum: int | None = Field(
        default=None,
        ge=1,
        description=(
            "CLI override (--sample-per-stratum) for dataset.sample_per_stratum: keep up to N "
            "rows per stratum (stratify_field, default expected_skill). Lets a runner cap a "
            "stratified dataset without editing the task YAML. Ignored when max_rows is set."
        ),
    )

    repeats: int | None = Field(
        default=None,
        ge=1,
        description="CLI override for replicates per (task, variant). None = defer to experiment layers.",
    )

    # HERE and nowhere else on purpose: deliberately NOT part of the 5-layer task
    # merge, so there is no `-D grade=...` path. A task YAML must never be able to
    # declare itself ungraded; only the invoking command decides.
    # Rationale: .claude/notes/orchestration.md § Execute vs. run: the grading switch
    grade: bool = Field(
        default=True,
        description=(
            "Evaluate success criteria after execution. False = `coder-eval execute`: "
            "run and capture the trajectory, score nothing, finalize as NOT_GRADED."
        ),
    )

    verbose: bool = Field(default=False, description="Enable verbose (DEBUG level) logging for Docker output")

    # Docker WORKDIR alignment for the NON-docker-driver dispatch path — a host
    # process, or a container someone else built. The same mechanism the docker
    # driver already uses, exposed for when coder-eval's own driver is not the one
    # building the container. Only meaningful for a single resolved task.
    workspace_dir: Path | None = Field(
        default=None,
        description=(
            "Run the agent in-place at this absolute path instead of the standard "
            "run_dir/artifacts workspace, copying it out to run_dir/artifacts/<task> at "
            "cleanup. For a single task only. Not for sandbox.driver: docker tasks — "
            "the docker driver already aligns automatically via sandbox.docker.working_dir."
        ),
    )

    # Sibling to workspace_dir: where finished artifacts land instead of the
    # The run's on-disk layout, as two independent templates resolved LATE (per
    # task, where ${variant}/${task}/${repeat} first exist). Defaults reproduce
    # today's layout byte-for-byte; a static override needs no special-casing
    # because substituting a string with no placeholders is the identity function.
    logging_dir_template: str = Field(
        default=DEFAULT_LOGGING_DIR_TEMPLATE,
        description=(
            "Where task.json/task.log go. Placeholders: ${run_dir}, ${variant}, ${task}, "
            "${repeat}. A static path (e.g. /logs/agent) resolves to itself."
        ),
    )
    artifacts_dir_template: str = Field(
        default=DEFAULT_ARTIFACTS_DIR_TEMPLATE,
        description=(
            "Where the agent's artifacts go -- the FINAL directory, not a parent. Same "
            "placeholders as logging_dir_template, and independent of it: the two may live in "
            "unrelated parts of the filesystem (Harbor puts logs at /logs/agent and artifacts "
            "at the container's WORKDIR). When it already holds the workspace there is nothing "
            "to copy."
        ),
    )

    def resolve_logging_dir(self, variant_id: str, task_id: str, replicate_index: int = 0) -> Path:
        """This task's logging directory, per ``logging_dir_template``."""
        return resolve_dir_template(
            self.logging_dir_template,
            run_dir=self.run_dir,
            variant_id=variant_id,
            task_id=task_id,
            replicate_index=replicate_index,
        )

    def resolve_artifacts_dir(self, variant_id: str, task_id: str, replicate_index: int = 0) -> Path:
        """This task's FINAL artifacts directory, per ``artifacts_dir_template``.

        One chokepoint for every consumer -- the orchestrator's capture/direct-write
        target, ``--resume``'s stale-artifact clearing, and the regrade workspace
        lookup -- so they cannot disagree about where a task's artifacts live.
        """
        return resolve_dir_template(
            self.artifacts_dir_template,
            run_dir=self.run_dir,
            variant_id=variant_id,
            task_id=task_id,
            replicate_index=replicate_index,
        )

    # TODO(container-death-diagnostics): containers run uncapped today, so at a
    # high --max-parallel one runaway task can pressure the host. An opt-in
    # default is already expressible through the layered sandbox config; a
    # dedicated CLI knob would go in as the LOWEST-priority layer, never
    # defaulted to a value (that would change existing configs).
