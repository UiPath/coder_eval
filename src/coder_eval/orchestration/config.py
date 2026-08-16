"""Configuration models for orchestration."""

import sys
import warnings
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coder_eval.models import PreservationMode, RowSelection


# The flat field names `row_selection` replaced. Derived from `RowSelection` rather than typed
# again, minus `split`.
#
# `split` is excluded because no RELEASED version ever exposed it: it was added as a flat field in
# f1b6abb and folded into `row_selection` in d5301a6, both on this unreleased branch, so v0.9.6 —
# what every out-of-tree caller actually has — carries only `max_rows` and `sample_per_stratum`.
# Deprecating a spelling nobody could be using would advertise an alias for one release and then
# remove it, which is pure churn. (Stated precisely because it is checkable and easy to get
# backwards: a future reader who finds f1b6abb and concludes the exclusion was an oversight would
# "fix" it and ship an alias for a field that never left the branch.)
_DEPRECATED_ROW_SELECTORS = tuple(n for n in RowSelection.model_fields if n != "split")


def _stacklevel_past_pydantic() -> int:
    """Frames from this module out to the first caller that is not pydantic.

    A `mode="before"` validator runs underneath a VARIABLE number of pydantic frames — two for
    keyword construction, a different count for `model_validate` — so no fixed `stacklevel` can
    point at the caller. Measured with `stacklevel=2`: the warning was attributed to
    `pydantic/main.py`, and because Python's default filter only surfaces a `DeprecationWarning`
    raised from `__main__`, an ordinary script constructing `BatchRunConfig(max_rows=5)` printed
    NOTHING. The alias worked and the migration signal it exists to deliver never arrived — which
    would make 0.11.0 a hard break nobody was warned about.
    """
    depth = 1
    while True:
        try:
            frame = sys._getframe(depth)
        except ValueError:
            return 2  # ran off the stack; fall back to the ordinary "my caller" level
        module = frame.f_globals.get("__name__", "")
        if module != __name__ and not module.startswith("pydantic"):
            return depth
        depth += 1


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

    # Dataset row selection (--split / --sample / --sample-per-stratum), one model rather
    # than three flat fields so the same declaration is what run.json records. Note the
    # nested model deliberately does NOT declare extra="forbid" (run.json must stay
    # forward-compatible — see RowSelection's docstring), so this container's own
    # extra="forbid" does not reach inside it: a mistyped RowSelection(...) keyword is
    # caught by pyright at the single construction site, not by validation here.
    row_selection: RowSelection = Field(
        default_factory=RowSelection,
        description=(
            "Which dataset rows this run selects (--split / --sample / --sample-per-stratum). "
            "Applied in expand_dataset, BEFORE variant resolution, so it takes part in no merge "
            "layer. Defaults to an empty selection (every row). Non-dataset tasks are unaffected."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_deprecated_row_selectors(cls, data: Any) -> Any:
        """Accept the pre-`row_selection` flat spellings for one release.

        ``max_rows`` and ``sample_per_stratum`` used to be fields on this model. Collapsing them
        into one ``RowSelection`` is right — it is the same declaration ``run.json`` records — but
        this container declares ``extra="forbid"``, so the old spelling went from *working* to a
        hard ``ValidationError`` with no deprecation step. ``run_batch`` is a public API and the
        nightly/dashboard consumer lives in a separate repo, so that break lands out of tree.

        A ``mode="before"`` validator is REQUIRED, not stylistic: ``extra="forbid"`` rejects an
        unknown key during validation, so a field validator never runs and an
        ``@model_validator(mode="after")`` never sees it either. This has to fold the keys away
        before validation looks at them.

        The fold happens pre-validation, so ``model_dump()`` shows only ``row_selection`` — which
        keeps ``compute_run_fingerprint`` (it dumps the whole config) byte-identical either way. A
        changed fingerprint would invalidate every existing ``--resume``.

        Passing a flat alias TOGETHER with an explicit ``row_selection`` raises rather than
        picking one: a caller mid-migration doing both has a bug, and choosing for them hides it.

        Removal: **0.11.0**. Version is 0.9.6 and ``[tool.semantic_release]`` bumps the minor on a
        ``feat:``, so these ship in 0.10.0 and out-of-tree callers get one full minor of overlap.
        ``RowSelection`` is the SSOT and deliberately does NOT carry these aliases — one deprecated
        spelling, in one place, on the container that broke.
        """
        if not isinstance(data, dict):
            return data
        aliased = {name: data[name] for name in _DEPRECATED_ROW_SELECTORS if name in data}
        if not aliased:
            return data
        # COPY, never pop in place. On `model_validate(payload)` pydantic hands in the caller's own
        # dict, and rewriting it there is a side effect they never asked for: measured, a caller
        # who built kwargs from JSON got their `max_rows` replaced by a `RowSelection` object and a
        # later `json.dumps(payload)` raised TypeError. The raise path below mutated too, so a
        # caller catching the error and retrying saw a silently altered payload.
        data = {k: v for k, v in data.items() if k not in aliased}
        if "row_selection" in data:
            raise ValueError(
                f"BatchRunConfig received both row_selection and the deprecated flat {sorted(aliased)}"
                + " — pass one or the other. The flat spellings fold into row_selection and are"
                + " removed in 0.11.0."
            )
        spelled = ", ".join(f"{name}=..." for name in sorted(aliased))
        warnings.warn(
            f"BatchRunConfig({spelled}) is deprecated and will be removed in 0.11.0;"
            + f" pass row_selection=RowSelection({spelled}) instead.",
            DeprecationWarning,
            stacklevel=_stacklevel_past_pydantic(),
        )
        # `model_validate`, not a splat (CE041): a dict genuinely IS the input here. The keys are
        # derived from `RowSelection.model_fields`, so they cannot be wrong — but `RowSelection`
        # deliberately declares no extra="forbid" (run.json must stay forward-readable), so a
        # splat would have no runtime guard behind it either.
        data["row_selection"] = RowSelection.model_validate(aliased)
        return data

    # Replicate count override
    repeats: int | None = Field(
        default=None,
        ge=1,
        description="CLI override for replicates per (task, variant). None = defer to experiment layers.",
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
