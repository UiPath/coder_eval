"""Configuration models for orchestration."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from coder_eval.models import PreservationMode


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

    # Agent type override stays a dedicated field: it requires re-parsing the
    # discriminated union (not a simple field-merge), so it is injected into the
    # generic agent patch by apply_overrides rather than living in `overrides`.
    agent_type: str | None = Field(default=None, description="Override agent type for all tasks (e.g., 'claude-code')")

    # Generic layer-5 task-config overrides. Built from -D/--set and the surviving
    # flag aliases (--model, --driver) in run_command, then applied to the resolved
    # TaskDefinition by orchestration.overrides.
    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Generic layer-5 task-config overrides (dotted path -> typed value) "
            "from -D/--set and the surviving flag aliases (--model, --driver)."
        ),
    )

    # Dataset sampling (for cheap smoke runs on dataset-backed tasks)
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

    # Replicate count override
    repeats: int | None = Field(
        default=None,
        ge=1,
        description="CLI override for replicates per (task, variant). None = defer to experiment layers.",
    )

    # Grading switch: `coder-eval run` (True) vs `coder-eval execute` (False).
    # It lives HERE and nowhere else on purpose — it is deliberately NOT part of
    # the 5-layer task merge, so there is no `-D grade=...` path and no
    # MergeField (CE014 does not apply to a scalar bool outside the merged
    # roots). A task YAML must never be able to declare itself ungraded; only
    # the invoking command decides.
    grade: bool = Field(
        default=True,
        description=(
            "Evaluate success criteria after execution. False = `coder-eval execute`: "
            "run and capture the trajectory, score nothing, finalize as NOT_GRADED."
        ),
    )

    # Logging
    verbose: bool = Field(default=False, description="Enable verbose (DEBUG level) logging for Docker output")

    # TODO(container-death-diagnostics): consider a run-level default resource
    # cap. Containers run uncapped today (sandbox.limits.{max_memory_mb,
    # max_cpus,max_pids} default to None -> _build_argv emits no --memory/
    # --cpus/--pids-limit), so at --max-parallel=20 a single runaway task can
    # pressure the whole host. An opt-in default cap is already expressible
    # via the EXISTING layered sandbox config -- defaults.sandbox.limits.
    # max_memory_mb in the experiment YAML, or `-D sandbox.limits.
    # max_memory_mb=N` on `coder-eval run` -- both flow through
    # resolve_all_tasks and are overridden by per-task limits. If a dedicated
    # CLI knob is ever wanted, add it as the FIRST (lowest-priority) layer in
    # _build_sandbox_layers so per-task limits win, and do NOT default it to
    # a non-None value (would change behavior for existing configs).
