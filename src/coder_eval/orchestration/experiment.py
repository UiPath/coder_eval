"""Experiment orchestration — loading, config resolution, and aggregation.

Provides standalone functions for the 3-phase experiment pipeline:
  1. RESOLVE: resolve_all_tasks() — task_files x experiment -> list[ResolvedTask]
  2. EXECUTE: run_batch() (in batch.py) — list[ResolvedTask] -> list[TaskResult]
  3. AGGREGATE: aggregate_results() — list[TaskResult] -> ExperimentResult
"""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path
from typing import Any, Literal

import yaml

from ..models import (
    AgentConfig,
    ConfigLineageEntry,
    EvaluationResult,
    ExperimentDefinition,
    ExperimentResult,
    ExperimentVariant,
    ResolvedTask,
    SnapshotMode,
    TaskDefinition,
    TaskExperimentSummary,
    TaskResult,
    TemplateDirSource,
    TemplateSource,
    VariantAggregate,
    VariantResult,
    validate_template_sources_list,
)
from .config import BatchRunConfig
from .task_loader import load_task


logger = logging.getLogger(__name__)


def _find_default_experiment() -> Path:
    """Locate the default experiment YAML, checking packaged resources first.

    Resolution order:
      1. Package resource (works in wheel installs)
      2. Repo-root fallback (works in source checkouts)
    """
    # 1. Package resource — always available in installed wheels
    pkg_resource = importlib.resources.files("coder_eval.resources").joinpath("default_experiment.yaml")
    pkg_path = Path(str(pkg_resource))
    if pkg_path.is_file():
        return pkg_path

    # 2. Repo-root fallback — for source checkouts / editable installs
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    repo_path = project_root / "experiments" / "default.yaml"
    if repo_path.is_file():
        return repo_path

    # Return the package path as default (will produce a clear FileNotFoundError if missing)
    return pkg_path


DEFAULT_EXPERIMENT_PATH = _find_default_experiment()


def _resolve_experiment_template_paths(experiment: ExperimentDefinition, base_dir: Path) -> None:
    """Resolve relative TemplateDirSource paths in experiment defaults and variants.

    Mutates paths in place, resolving relative paths against the experiment YAML directory.

    Args:
        experiment: The experiment definition to resolve paths in.
        base_dir: Directory containing the experiment YAML file.
    """
    sources_lists: list[list[TemplateSource]] = []
    if experiment.defaults and experiment.defaults.template_sources:
        sources_lists.append(experiment.defaults.template_sources)
    for variant in experiment.variants:
        if variant.template_sources:
            sources_lists.append(variant.template_sources)

    for sources in sources_lists:
        for i, source in enumerate(sources):
            if isinstance(source, TemplateDirSource):
                template_path = Path(source.path)
                if not template_path.is_absolute():
                    sources[i] = source.model_copy(update={"path": str((base_dir / template_path).resolve())})


def load_experiment(experiment_file: Path) -> ExperimentDefinition:
    """Load an experiment definition from a YAML file.

    Args:
        experiment_file: Path to the experiment YAML file.

    Returns:
        Parsed ExperimentDefinition.

    Raises:
        FileNotFoundError: If experiment file doesn't exist.
        ValueError: If experiment file is invalid.
    """
    if not experiment_file.exists():
        raise FileNotFoundError(f"Experiment file not found: {experiment_file}")

    with open(experiment_file) as f:
        data = yaml.safe_load(f)

    try:
        exp = ExperimentDefinition(**data)
        _resolve_experiment_template_paths(exp, experiment_file.parent)
        return exp
    except Exception as e:
        raise ValueError(f"Invalid experiment definition: {e}") from e


def _merge_agent_dicts(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """Merge multiple partial agent config dicts (left to right, later wins).

    Shallow merge only — each layer's keys overwrite the previous entirely.
    Lists and nested dicts are replaced, not recursively merged.
    None layers are skipped.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer is not None:
            merged.update(layer)
    return merged


type ConfigSource = Literal["default", "task", "experiment-defaults", "variant", "cli"]


def _build_agent_lineage(
    layers: list[tuple[ConfigSource, dict[str, Any] | None]],
) -> dict[str, ConfigLineageEntry]:
    """Replay the agent merge and record which layer set each key.

    Args:
        layers: List of (source_name, agent_dict) tuples in precedence order (later wins).

    Returns:
        Dict mapping dotted keys like "agent.model" to ConfigLineageEntry.
    """
    lineage: dict[str, ConfigLineageEntry] = {}
    for source_name, layer in layers:
        if layer is None:
            continue
        for key, value in layer.items():
            lineage[f"agent.{key}"] = ConfigLineageEntry(value=value, source=source_name)
    return lineage


def resolve_task_for_variant(
    default_experiment: ExperimentDefinition,
    task: TaskDefinition,
    experiment: ExperimentDefinition,
    variant: ExperimentVariant,
) -> tuple[TaskDefinition, dict[str, ConfigLineageEntry]]:
    """Resolve a fully-configured TaskDefinition by merging the 4-layer precedence chain.

    Precedence (lowest to highest):
        1. default_experiment.defaults.agent
        2. experiment.defaults.agent
        3. task.agent
        4. variant.agent

    Args:
        default_experiment: The default experiment (experiments/default.yaml).
        task: The original task definition (may have agent=None).
        experiment: The active experiment definition.
        variant: The specific variant to resolve for.

    Returns:
        Tuple of (resolved TaskDefinition, config lineage dict).
    """
    # Layer 1: default experiment defaults agent
    default_agent = default_experiment.defaults.agent if default_experiment.defaults else None

    # Layer 2: experiment defaults agent
    exp_defaults_agent = experiment.defaults.agent if experiment.defaults else None

    # Layer 3: task agent (only explicitly-set fields, not Pydantic defaults)
    task_agent = task.agent.model_dump(exclude_unset=True) if task.agent else None

    # Layer 4: variant agent
    variant_agent = variant.agent

    # Merge agent dicts
    merged_agent_dict = _merge_agent_dicts(default_agent, exp_defaults_agent, task_agent, variant_agent)
    resolved_agent = AgentConfig(**merged_agent_dict)

    # Build agent lineage
    agent_lineage = _build_agent_lineage(
        [
            ("default", default_agent),
            ("experiment-defaults", exp_defaults_agent),
            ("task", task_agent),
            ("variant", variant_agent),
        ]
    )

    # Resolve scalar overrides and track lineage simultaneously (4-layer precedence)
    # Order: default → experiment-defaults → task → variant (task wins over experiment defaults)
    resolved_max_iterations = task.max_iterations
    resolved_task_timeout = task.task_timeout
    resolved_turn_timeout = task.agent.turn_timeout if task.agent else None

    scalar_lineage: dict[str, ConfigLineageEntry] = {}
    task_explicit = task.model_fields_set
    task_agent_explicit = task.agent.model_fields_set if task.agent else set()

    # Layer 1: default experiment defaults scalars
    if default_experiment.defaults:
        if default_experiment.defaults.max_iterations is not None and "max_iterations" not in task_explicit:
            resolved_max_iterations = default_experiment.defaults.max_iterations
            scalar_lineage["max_iterations"] = ConfigLineageEntry(value=resolved_max_iterations, source="default")
        if default_experiment.defaults.task_timeout is not None and "task_timeout" not in task_explicit:
            resolved_task_timeout = default_experiment.defaults.task_timeout
            scalar_lineage["task_timeout"] = ConfigLineageEntry(value=resolved_task_timeout, source="default")
        if default_experiment.defaults.turn_timeout is not None and "turn_timeout" not in task_agent_explicit:
            resolved_turn_timeout = default_experiment.defaults.turn_timeout
            scalar_lineage["turn_timeout"] = ConfigLineageEntry(value=resolved_turn_timeout, source="default")

    # Layer 2: experiment defaults scalars (overrides default, but task can still override these)
    if experiment.defaults:
        if experiment.defaults.max_iterations is not None and "max_iterations" not in task_explicit:
            resolved_max_iterations = experiment.defaults.max_iterations
            scalar_lineage["max_iterations"] = ConfigLineageEntry(
                value=resolved_max_iterations, source="experiment-defaults"
            )
        if experiment.defaults.task_timeout is not None and "task_timeout" not in task_explicit:
            resolved_task_timeout = experiment.defaults.task_timeout
            scalar_lineage["task_timeout"] = ConfigLineageEntry(
                value=resolved_task_timeout, source="experiment-defaults"
            )
        if experiment.defaults.turn_timeout is not None and "turn_timeout" not in task_agent_explicit:
            resolved_turn_timeout = experiment.defaults.turn_timeout
            scalar_lineage["turn_timeout"] = ConfigLineageEntry(
                value=resolved_turn_timeout, source="experiment-defaults"
            )

    # Layer 3: task-explicit scalars (override experiment defaults)
    if "max_iterations" in task_explicit:
        scalar_lineage["max_iterations"] = ConfigLineageEntry(value=task.max_iterations, source="task")
    if "task_timeout" in task_explicit:
        scalar_lineage["task_timeout"] = ConfigLineageEntry(value=task.task_timeout, source="task")
    if task.agent and "turn_timeout" in task_agent_explicit:
        scalar_lineage["turn_timeout"] = ConfigLineageEntry(value=task.agent.turn_timeout, source="task")

    # Layer 4: variant scalars (highest precedence before CLI)
    if variant.max_iterations is not None:
        resolved_max_iterations = variant.max_iterations
        scalar_lineage["max_iterations"] = ConfigLineageEntry(value=resolved_max_iterations, source="variant")
    if variant.task_timeout is not None:
        resolved_task_timeout = variant.task_timeout
        scalar_lineage["task_timeout"] = ConfigLineageEntry(value=resolved_task_timeout, source="variant")
    if variant.turn_timeout is not None:
        resolved_turn_timeout = variant.turn_timeout
        scalar_lineage["turn_timeout"] = ConfigLineageEntry(value=resolved_turn_timeout, source="variant")

    # Apply turn_timeout to agent config
    resolved_agent.turn_timeout = resolved_turn_timeout

    # Resolve template_sources: task base + experiment defaults overlays + variant overlays (append semantics)
    base_sources: list[TemplateSource] = list(task.sandbox.template_sources or [])
    exp_defaults_sources: list[TemplateSource] = (
        list(experiment.defaults.template_sources)
        if experiment.defaults and experiment.defaults.template_sources
        else []
    )
    variant_sources: list[TemplateSource] = list(variant.template_sources) if variant.template_sources else []

    combined_sources = base_sources + exp_defaults_sources + variant_sources
    resolved_sandbox = task.sandbox
    if exp_defaults_sources or variant_sources:
        validate_template_sources_list(combined_sources)
        resolved_sandbox = task.sandbox.model_copy(update={"template_sources": combined_sources})

    # Combine lineage
    lineage = {**agent_lineage, **scalar_lineage}

    # Build resolved task (copy with overrides)
    resolved_task = task.model_copy(
        update={
            "agent": resolved_agent,
            "max_iterations": resolved_max_iterations,
            "task_timeout": resolved_task_timeout,
            "sandbox": resolved_sandbox,
        }
    )
    return resolved_task, lineage


def _apply_cli_overrides(
    task: TaskDefinition,
    config: BatchRunConfig,
    lineage: dict[str, ConfigLineageEntry] | None = None,
) -> None:
    """Apply CLI and .env overrides (layer 5) to a task definition in-place.

    Override precedence: CLI > .env > experiment layers 1-4.

    Args:
        task: The task definition to mutate.
        config: Batch run configuration containing CLI overrides.
        lineage: Optional lineage dict to update with CLI override entries.
    """
    from ..config import settings as app_settings
    from ..models import SnapshotConfig

    def _record(key: str, value: Any, detail: str) -> None:
        if lineage is not None:
            lineage[key] = ConfigLineageEntry(value=value, source="cli", source_detail=detail)

    # Agent overrides (CLI > .env > task)
    assert task.agent is not None, f"Task '{task.task_id}' has no agent config"

    effective_model = config.agent_model if config.agent_model is not None else app_settings.default_agent_model
    if effective_model is not None:
        task.agent.model = effective_model
        detail = "--model" if config.agent_model is not None else ".env DEFAULT_AGENT_MODEL"
        _record("agent.model", effective_model, detail)

    effective_perm = (
        config.permission_mode if config.permission_mode is not None else app_settings.default_permission_mode
    )
    if effective_perm is not None:
        task.agent.permission_mode = effective_perm  # type: ignore[assignment]  # validated by Pydantic via validate_assignment
        detail = "--permission-mode" if config.permission_mode is not None else ".env DEFAULT_PERMISSION_MODE"
        _record("agent.permission_mode", effective_perm, detail)

    effective_max_turns = config.max_turns if config.max_turns is not None else app_settings.default_max_turns
    if effective_max_turns is not None:
        task.agent.max_turns = effective_max_turns
        detail = "--max-turns" if config.max_turns is not None else ".env DEFAULT_MAX_TURNS"
        _record("agent.max_turns", effective_max_turns, detail)

    # Timeout overrides (CLI > task YAML)
    if config.task_timeout is not None:
        task.task_timeout = config.task_timeout
        _record("task_timeout", config.task_timeout, "--task-timeout")
    if config.turn_timeout is not None:
        task.agent.turn_timeout = config.turn_timeout
        _record("turn_timeout", config.turn_timeout, "--turn-timeout")

    # Tool/plugin overrides
    if config.allowed_tools is not None:
        task.agent.allowed_tools = config.allowed_tools
        _record("agent.allowed_tools", config.allowed_tools, "--allowed-tools")
    if config.plugins is not None:
        task.agent.plugins = config.plugins
        _record("agent.plugins", config.plugins, "--plugins")
    if config.ignore_patterns is not None:
        task.agent.ignore_patterns = config.ignore_patterns
        _record("agent.ignore_patterns", config.ignore_patterns, "--ignore-patterns")

    # Max iterations override
    if config.max_iterations is not None:
        task.max_iterations = config.max_iterations
        _record("max_iterations", config.max_iterations, "--max-iterations")

    # Snapshot overrides
    if config.snapshot_mode or config.snapshot_checkpoint_freq:
        mode = SnapshotMode(config.snapshot_mode.lower()) if config.snapshot_mode else task.sandbox.snapshots.mode
        checkpoint_freq = (
            config.snapshot_checkpoint_freq
            if config.snapshot_checkpoint_freq is not None
            else task.sandbox.snapshots.checkpoint_frequency
        )
        task.sandbox.snapshots = SnapshotConfig(
            mode=mode,
            checkpoint_frequency=checkpoint_freq,
            ignore_patterns=task.sandbox.snapshots.ignore_patterns,
        )
        if config.snapshot_mode:
            _record("sandbox.snapshots.mode", mode, "--snapshot-mode")
        if config.snapshot_checkpoint_freq is not None:
            _record("sandbox.snapshots.checkpoint_frequency", checkpoint_freq, "--snapshot-checkpoint-freq")


def resolve_all_tasks(
    task_files: list[Path],
    experiment: ExperimentDefinition,
    default_experiment: ExperimentDefinition,
    config: BatchRunConfig,
) -> list[ResolvedTask]:
    """Resolve all (task x variant) combinations into typed, run-ready entries.

    Applies all 5 config layers in one place:
        1. default experiment base
        2. task YAML
        3. experiment base
        4. variant overrides
        5. CLI / .env overrides

    Also handles tag filtering and unique task ID validation.

    Args:
        task_files: Paths to task YAML files.
        experiment: The active experiment definition.
        default_experiment: The default experiment (experiments/default.yaml).
        config: Batch run configuration (provides CLI overrides, tags, run_dir).

    Returns:
        List of ResolvedTask entries ready for run_batch.

    Raises:
        ValueError: If duplicate task IDs are found after resolution.
    """
    resolved: list[ResolvedTask] = []

    for task_file in task_files:
        task, source_yaml = load_task(task_file)

        for variant in experiment.variants:
            # Apply layers 1-4 (default → experiment-defaults → task → variant)
            resolved_task, lineage = resolve_task_for_variant(default_experiment, task, experiment, variant)

            # Apply layer 5 (CLI / .env overrides)
            _apply_cli_overrides(resolved_task, config, lineage)

            resolved.append(
                ResolvedTask(
                    task=resolved_task,
                    task_file=task_file,
                    run_dir=config.run_dir / variant.variant_id / resolved_task.task_id,
                    variant_id=variant.variant_id,
                    source_yaml=source_yaml,
                    config_lineage=lineage,
                )
            )

    # Filter by tags
    if config.include_tags or config.exclude_tags:
        from .batch import filter_tasks_by_tags

        tagged = [(rt.task_file, rt.task) for rt in resolved]
        filtered = filter_tasks_by_tags(tagged, include_tags=config.include_tags, exclude_tags=config.exclude_tags)
        filtered_ids = {t.task_id for _, t in filtered}
        resolved = [rt for rt in resolved if rt.task.task_id in filtered_ids]

    # Validate no duplicate (task_id, variant_id) combinations
    seen: dict[tuple[str, str], list[Path]] = {}
    for rt in resolved:
        key = (rt.task.task_id, rt.variant_id)
        seen.setdefault(key, []).append(rt.task_file)
    duplicates = {k: files for k, files in seen.items() if len(files) > 1}
    if duplicates:
        lines = [
            f"  - '{tid}' (variant '{vid}'): {', '.join(str(f) for f in files)}"
            for (tid, vid), files in duplicates.items()
        ]
        raise ValueError("Duplicate task IDs found:\n" + "\n".join(lines))

    return resolved


def aggregate_results(
    experiment_id: str,
    description: str,
    variant_ids: list[str],
    task_results: list[TaskResult],
    total_duration: float,
) -> ExperimentResult:
    """Aggregate typed task results into an ExperimentResult with cross-variant comparisons.

    Args:
        experiment_id: Identifier for the experiment.
        description: Human-readable description.
        variant_ids: List of variant IDs in the experiment.
        task_results: Typed results from run_batch execution.
        total_duration: Total wall-clock duration in seconds.

    Returns:
        ExperimentResult with task summaries and variant aggregates.
    """
    # Group results by task_id using variant_id from TaskResult directly
    task_variants: dict[str, list[VariantResult]] = {}
    for tr in task_results:
        result: EvaluationResult = tr.result
        task_id = tr.task_id
        variant_id = tr.variant_id

        # Extract reference_comparison score if present
        ref_similarity: float | None = None
        for cr in result.success_criteria_results:
            if cr.criterion_type == "reference_comparison":
                ref_similarity = cr.score
                break

        variant_result = VariantResult(
            variant_id=variant_id,
            task_id=task_id,
            weighted_score=result.weighted_score or 0.0,
            final_status=result.final_status,
            duration_seconds=result.duration_seconds,
            total_tokens=result.total_token_usage.total_tokens if result.total_token_usage else None,
            iteration_count=result.iteration_count,
            total_assistant_turns=result.total_assistant_turns,
            reference_similarity=ref_similarity,
        )
        task_variants.setdefault(task_id, []).append(variant_result)

    # Build task summaries
    task_summaries: list[TaskExperimentSummary] = []
    for task_id, variants in task_variants.items():
        best = max(variants, key=lambda v: (v.weighted_score, v.variant_id))
        scores = [v.weighted_score for v in variants]
        top_count = sum(1 for v in variants if v.weighted_score == best.weighted_score)
        task_summaries.append(
            TaskExperimentSummary(
                task_id=task_id,
                variant_results=variants,
                best_variant=best.variant_id,
                is_tie=top_count > 1,
                score_spread=max(scores) - min(scores),
            )
        )

    # Build variant aggregates
    variant_aggregates: dict[str, VariantAggregate] = {}
    for vid in variant_ids:
        vr_list = [vr for ts in task_summaries for vr in ts.variant_results if vr.variant_id == vid]
        if not vr_list:
            variant_aggregates[vid] = VariantAggregate(
                variant_id=vid,
                tasks_run=0,
                tasks_succeeded=0,
                tasks_failed=0,
                average_score=0.0,
                average_duration=0.0,
            )
            continue

        token_values = [v.total_tokens for v in vr_list if v.total_tokens is not None]
        total_tokens = sum(token_values) if token_values else None
        variant_aggregates[vid] = VariantAggregate(
            variant_id=vid,
            tasks_run=len(vr_list),
            tasks_succeeded=sum(1 for v in vr_list if v.final_status == "SUCCESS"),
            tasks_failed=sum(
                1 for v in vr_list if v.final_status in ("FAILURE", "ERROR", "TIMEOUT", "MAX_TURNS_EXHAUSTED")
            ),
            average_score=sum(v.weighted_score for v in vr_list) / len(vr_list),
            average_duration=sum(v.duration_seconds for v in vr_list) / len(vr_list),
            total_tokens=total_tokens,
        )

    return ExperimentResult(
        experiment_id=experiment_id,
        description=description,
        variant_ids=variant_ids,
        task_summaries=task_summaries,
        variant_aggregates=variant_aggregates,
        total_duration_seconds=total_duration,
    )
