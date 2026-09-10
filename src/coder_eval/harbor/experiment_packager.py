"""Experiment export — ``coder-eval export --format harbor <task.yaml>... -e <experiment.yaml> -o <dir>``.

Extends the single-task Harbor packager (``packager.py``) to ``experiment.yaml``
variants. Harbor's ``task.toml`` has no concept of a "variant" — an experiment
comparing N variants over M task files becomes N*M (times replicates, times
dataset rows) independent Harbor task directories, one per resolved
(task, variant, replicate[, dataset row]) combination.

Resolution reuses ``orchestration/experiment.py::resolve_all_tasks`` — the exact
machinery ``coder-eval run -e`` uses — so the export sees identical resolved
configs to a real experiment run, with zero duplicated merge logic. Each
resolved task is then written out via ``packager.export_resolved_task``, the
same writer the single-task path uses.

See ``tmp/harborframework_conversion.md`` for the full table of what does and
does not survive this translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from coder_eval.harbor.packager import (
    CriteriaNotExportableError,
    ExportResult,
    TaskNotExportableError,
    export_resolved_task,
)
from coder_eval.models import ResolvedTask, SkippedTask
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment, resolve_all_tasks


# Sources a resolved field's ConfigLineageEntry can carry (see
# models/results.py::ConfigLineageEntry). Only these two mean "the experiment
# introduced this, the task's own YAML did not."
_EXPERIMENT_INTRODUCED_SOURCES = {"variant", "experiment-defaults"}


@dataclass(frozen=True)
class SkippedVariantExport:
    """One resolved (task, variant, replicate) combination that was not exported, and why."""

    variant_id: str
    task_id: str
    replicate_index: int
    reason: str


@dataclass(frozen=True)
class ExperimentExportResult:
    """What ``export_experiment`` produced, for the CLI to report."""

    exported: list[ExportResult] = field(default_factory=list)
    skipped: list[SkippedVariantExport] = field(default_factory=list)
    load_skipped: list[SkippedTask] = field(default_factory=list)


def _unhonorable_override_reason(resolved: ResolvedTask) -> str | None:
    """Why this resolved task's Harbor export can't honor an experiment-introduced override.

    Harbor's agent phase is Harbor's own (or the unbuilt C1.2
    coder-eval-as-agent embed adapter, see ``tmp/harborframework.md`` Part A
    step 8) — entirely independent of coder-eval's ``agent``/``simulation``
    config. A variant that changes neither is fine to export (the exported
    directory is then identical to the base task's own export, which is the
    existing, already-documented single-task limitation — coder-eval's
    ``agent`` field is never read for the exported verifier ``tests/task.yaml``
    either). But a variant that *introduces* an agent or simulation override
    is the entire point of an A/B experiment: silently dropping it would
    export two indistinguishable Harbor directories claiming to compare two
    different arms. Detected via ``ResolvedTask.config_lineage`` — a field
    counts only when its most-specific source is the variant or the
    experiment defaults, never the task's own YAML or the baseline defaults.
    """
    agent_sources = sorted(
        {
            entry.source
            for key, entry in resolved.config_lineage.items()
            if key.startswith("agent.") and entry.source in _EXPERIMENT_INTRODUCED_SOURCES
        }
    )
    if agent_sources:
        return (
            f"variant {resolved.variant_id!r} sets an agent override (source: {', '.join(agent_sources)}) that "
            "Harbor's verifier-only export cannot honor -- Harbor's agent phase is independent of coder-eval's "
            "agent config. Building the C1.2 coder-eval-as-agent embed adapter (tmp/harborframework.md, Part A "
            "step 8) is the fix; until then this variant is skipped rather than exported as an indistinguishable "
            "duplicate of another arm."
        )
    sim_entry = resolved.config_lineage.get("simulation")
    if sim_entry is not None and sim_entry.source in _EXPERIMENT_INTRODUCED_SOURCES:
        return (
            f"variant {resolved.variant_id!r} sets a simulation override that Harbor's single-shot verifier "
            "export cannot represent -- Harbor's Trial model has no multi-turn user-simulator concept. Skipped."
        )
    return None


def _out_subdir(out_dir: Path, resolved: ResolvedTask, *, needs_replicate_segment: bool) -> Path:
    """``<out_dir>/<variant_id>/<task_id>/[<row_id>/]rep<NN>/`` — row/replicate segments only when they fan out."""
    dest = out_dir / resolved.variant_id / resolved.task.task_id
    if resolved.task.row_id:
        dest = dest / resolved.task.row_id
    if needs_replicate_segment:
        dest = dest / f"rep{resolved.replicate_index:02d}"
    return dest


def export_experiment(
    task_files: list[Path],
    experiment_file: Path,
    out_dir: Path,
    *,
    allow_credentials: bool = False,
) -> ExperimentExportResult:
    """Export every (task x variant x replicate[ x dataset row]) combination to Harbor directories.

    Resolves ``task_files`` against ``experiment_file`` through the same
    ``resolve_all_tasks`` pipeline ``coder-eval run -e`` uses (including
    ``experiments/default.yaml`` as the baseline layer, when present), then
    writes one Harbor task directory per resolved task at
    ``<out_dir>/<variant_id>/<task_id>/[<row_id>/]rep<NN>/``. A resolved task
    is SKIPPED (not fatal to the rest of the export) rather than written when:
    it carries an experiment-introduced ``agent``/``simulation`` override
    Harbor's export cannot honor (see ``_unhonorable_override_reason``), its
    sandbox driver isn't ``docker``, or its criteria fail C1.4's portability
    audit -- each mirrors an existing per-task refusal in ``packager.py``,
    demoted from a raised exception to a collected skip so one bad variant
    does not abort the whole experiment's export.
    """
    experiment = load_experiment(experiment_file)
    if experiment_file == DEFAULT_EXPERIMENT_PATH:
        default_experiment = experiment
    elif DEFAULT_EXPERIMENT_PATH.exists():
        default_experiment = load_experiment(DEFAULT_EXPERIMENT_PATH)
    else:
        default_experiment = experiment

    config = BatchRunConfig(run_dir=out_dir)
    resolved_tasks, load_skipped = resolve_all_tasks(
        task_files=task_files,
        experiment=experiment,
        default_experiment=default_experiment,
        config=config,
        experiment_file=experiment_file,
    )

    # How many replicates each (variant, task) group actually fanned out to,
    # so a single-replicate group's directory omits the `rep00` segment.
    replicate_counts: dict[tuple[str, str], int] = {}
    for r in resolved_tasks:
        key = (r.variant_id, r.task.task_id)
        replicate_counts[key] = max(replicate_counts.get(key, 0), r.replicate_index + 1)

    exported: list[ExportResult] = []
    skipped: list[SkippedVariantExport] = []
    for resolved in resolved_tasks:
        reason = _unhonorable_override_reason(resolved)
        if reason is not None:
            skipped.append(
                SkippedVariantExport(
                    variant_id=resolved.variant_id,
                    task_id=resolved.task.task_id,
                    replicate_index=resolved.replicate_index,
                    reason=reason,
                )
            )
            continue

        needs_replicate_segment = replicate_counts[(resolved.variant_id, resolved.task.task_id)] > 1
        dest = _out_subdir(out_dir, resolved, needs_replicate_segment=needs_replicate_segment)
        try:
            result = export_resolved_task(
                resolved.task,
                resolved.task_file,
                dest,
                allow_credentials=allow_credentials,
            )
        except (TaskNotExportableError, CriteriaNotExportableError) as e:
            skipped.append(
                SkippedVariantExport(
                    variant_id=resolved.variant_id,
                    task_id=resolved.task.task_id,
                    replicate_index=resolved.replicate_index,
                    reason=str(e),
                )
            )
            continue
        exported.append(result)

    return ExperimentExportResult(exported=exported, skipped=skipped, load_skipped=load_skipped)


__all__ = [
    "ExperimentExportResult",
    "SkippedVariantExport",
    "export_experiment",
]
