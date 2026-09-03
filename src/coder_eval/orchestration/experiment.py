"""Experiment orchestration — loading, config resolution, and aggregation.

Provides standalone functions for the 3-phase experiment pipeline:
  1. RESOLVE: resolve_all_tasks() — task_files x experiment -> list[ResolvedTask]
  2. EXECUTE: run_batch() (in batch.py) — list[ResolvedTask] -> list[TaskResult]
  3. AGGREGATE: aggregate_results() — list[TaskResult] -> ExperimentResult
"""

from __future__ import annotations

import importlib.resources
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from ..models import (
    AgentConfig,
    BaseAgentConfig,
    CheckerContext,
    ConfigLineageEntry,
    ExperimentDefinition,
    ExperimentResult,
    ExperimentVariant,
    FinalStatus,
    ResolvedTask,
    RunLimits,
    SimulationConfig,
    SkippedTask,
    TaskDefinition,
    TaskExperimentSummary,
    TaskResult,
    TemplateSource,
    VariantAggregate,
    VariantResult,
    apply_prompt_mutations,
)
from ..path_utils import build_task_run_dir
from .config import BatchRunConfig
from .config_merge import ConfigSource, Layer, merge_layers, resolve_root
from .task_loader import (
    expand_dataset,
    load_task,
    resolve_agent_system_prompt,
    resolve_template_source_paths,
    resolve_variant_initial_prompt_file,
)


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
        resolve_template_source_paths(sources, base_dir)


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

    with open(experiment_file, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    try:
        exp = ExperimentDefinition(**data)
        _resolve_experiment_template_paths(exp, experiment_file.parent)
        return exp
    except Exception as e:
        raise ValueError(f"Invalid experiment definition: {e}") from e


def _resolve_simulation(
    default_experiment: ExperimentDefinition,
    experiment: ExperimentDefinition,
    task: TaskDefinition,
    variant: ExperimentVariant,
    lineage: dict[str, ConfigLineageEntry],
) -> SimulationConfig | None:
    """Merge simulation config across the 4-layer precedence chain.

    Precedence (lowest to highest):
      1. default_experiment.defaults.simulation
      2. experiment.defaults.simulation
      3. task.simulation
      4. variant.simulation

    Any layer can be None (absent). When every layer is absent, the
    resolved value is None — single-shot mode is preserved.

    The merge runs through the generic resolver (``merge_layers`` over
    ``SimulationConfig``): every field uses the type-aware default — scalars and
    the ``constraints`` list all ``replace`` — so each layer's keys overwrite
    earlier ones, matching the historical shallow merge. After merging, the dict
    is validated by constructing a ``SimulationConfig`` (which raises a helpful
    error if a required ``persona``/``goal`` was never supplied). Lineage stays
    coarse: a single ``simulation`` entry crediting the most-specific source.
    """
    sim_specs: list[tuple[ConfigSource, dict[str, Any] | None]] = [
        ("default", default_experiment.defaults.simulation if default_experiment.defaults else None),
        ("experiment-defaults", experiment.defaults.simulation if experiment.defaults else None),
        ("task", task.simulation.model_dump(exclude_unset=True) if task.simulation else None),
        ("variant", variant.simulation),
    ]
    sim_layers = [Layer(source=src, patch=patch) for src, patch in sim_specs if patch is not None]
    if not sim_layers:
        return None

    # No lineage passed → merge_layers emits no per-field entries; we record the
    # single coarse `simulation` entry below instead.
    merged = merge_layers((SimulationConfig,), sim_layers, lineage_root="simulation")
    resolved = SimulationConfig(**merged)

    most_specific: ConfigSource | None = next((src for src, patch in reversed(sim_specs) if patch is not None), None)
    if most_specific is not None:
        lineage["simulation"] = ConfigLineageEntry(value=merged, source=most_specific)

    return resolved


def _resolve_checker_context(
    default_experiment: ExperimentDefinition,
    experiment: ExperimentDefinition,
    task: TaskDefinition,
    variant: ExperimentVariant,
    lineage: dict[str, ConfigLineageEntry],
) -> CheckerContext:
    """Merge ``checker_context`` across the 4-layer precedence chain.

    Precedence (lowest to highest):
      1. default_experiment.defaults.checker_context
      2. experiment.defaults.checker_context
      3. task.checker_context
      4. variant.checker_context

    Mirrors ``_resolve_simulation``: runs through the generic resolver
    (``merge_layers`` over ``CheckerContext``) so a later layer's fields
    overwrite earlier ones (nested ``api_route`` merges field-by-field, the
    type-aware default for a nested ``BaseModel``), then the merged dict is
    validated by constructing a ``CheckerContext`` — which raises a helpful,
    typed error (unknown key, wrong value type, `params`/`env_params` without
    `route: litellm`) rather than a hand-rolled shape check. Lineage stays
    coarse: a single ``checker_context`` entry crediting the most-specific source.
    """
    specs: list[tuple[ConfigSource, dict[str, Any] | None]] = [
        ("default", default_experiment.defaults.checker_context if default_experiment.defaults else None),
        ("experiment-defaults", experiment.defaults.checker_context if experiment.defaults else None),
        ("task", task.checker_context.model_dump(exclude_unset=True) if task.checker_context else None),
        ("variant", variant.checker_context),
    ]
    layers = [Layer(source=src, patch=patch) for src, patch in specs if patch is not None]
    if not layers:
        return CheckerContext()

    merged = merge_layers((CheckerContext,), layers, lineage_root="checker_context")
    resolved = CheckerContext(**merged)

    most_specific: ConfigSource | None = next((src for src, patch in reversed(specs) if patch is not None), None)
    if most_specific is not None:
        lineage["checker_context"] = ConfigLineageEntry(value=merged, source=most_specific)

    return resolved


def _resolve_repeats(
    default_experiment: ExperimentDefinition,
    experiment: ExperimentDefinition,
    variant: ExperimentVariant,
    config: BatchRunConfig,
    lineage: dict[str, ConfigLineageEntry],
) -> int:
    """Resolve effective ``repeats`` for a (task, variant) via the 4-layer merge.

    Skips task YAML (layer 3) — TaskDefinition has no repeats field.
    Writes a ``"repeats"`` entry into ``lineage`` recording the winning source.
    Raises ValueError when the resolved value exceeds 99 (2-digit subdir padding limit).
    """
    effective = 1
    source: str = "default"

    if default_experiment.defaults and default_experiment.defaults.repeats is not None:
        effective = default_experiment.defaults.repeats
        source = "default"
    if experiment.defaults and experiment.defaults.repeats is not None:
        effective = experiment.defaults.repeats
        source = "experiment-defaults"
    if variant.repeats is not None:
        effective = variant.repeats
        source = "variant"
    if config.repeats is not None:
        effective = config.repeats
        source = "cli"

    if effective > 99:
        raise ValueError(
            f"repeats must be <= 99 (got {effective}); widen replicate_subdir_name padding to support more"
        )

    lineage["repeats"] = ConfigLineageEntry(value=effective, source=source)
    return effective


def _apply_prompt_overrides(
    task: TaskDefinition,
    experiment: ExperimentDefinition,
    variant: ExperimentVariant,
    lineage: dict[str, ConfigLineageEntry],
) -> None:
    """Apply variant-level prompt overrides or mutations to a resolved task. Mutates in place.

    Resolution order:
      1. If variant.initial_prompt is set → full replacement (skip all mutations)
      2. Else → apply experiment.defaults.prompt_mutations, then variant.prompt_mutations

    Args:
        task: The resolved task definition (initial_prompt already inlined from task YAML).
        experiment: The active experiment definition (for defaults.prompt_mutations).
        variant: The specific variant being resolved.
        lineage: Config lineage dict to update.
    """
    # Full replacement — skip all mutations
    if variant.initial_prompt is not None:
        task.initial_prompt = variant.initial_prompt
        lineage["initial_prompt"] = ConfigLineageEntry(
            value="(overridden)", source="variant", source_detail="initial_prompt override"
        )
        return

    # Collect mutations: defaults first, then variant
    defaults_mutations = (
        list(experiment.defaults.prompt_mutations)
        if experiment.defaults and experiment.defaults.prompt_mutations
        else []
    )
    variant_mutations = list(variant.prompt_mutations) if variant.prompt_mutations else []
    combined = defaults_mutations + variant_mutations

    if not combined:
        return

    if task.initial_prompt is None:
        raise ValueError(f"initial_prompt must be resolved before applying mutations (task '{task.task_id}')")

    try:
        task.initial_prompt = apply_prompt_mutations(task.initial_prompt, combined)
    except re.error as e:
        raise ValueError(
            f"Invalid regex in prompt_mutations for variant '{variant.variant_id}' on task '{task.task_id}': {e}"
        ) from e

    # Build descriptive detail
    type_names = [m.type for m in combined]
    sources = []
    if defaults_mutations:
        sources.append("experiment-defaults")
    if variant_mutations:
        sources.append(f"variant '{variant.variant_id}'")
    detail = f"{len(combined)} ops ({', '.join(type_names)}) from {' + '.join(sources)}"

    lineage["initial_prompt"] = ConfigLineageEntry(value="(mutated)", source="mutation", source_detail=detail)


def _build_sandbox_layers(
    default_experiment: ExperimentDefinition,
    experiment: ExperimentDefinition,
    task: TaskDefinition,
    variant: ExperimentVariant,
) -> list[Layer]:
    """Build the ordered ``Layer`` list for the sandbox root (layers 1-4).

    Constructs the sandbox layer list from default/exp-defaults/task SandboxConfig
    dumps, plus synthetic layers for the experiment/variant TOP-LEVEL driver
    (replace) and template_sources (append). `template_sources` is popped from
    EVERY sandbox dump and re-added as dedicated synthetic layers, because its
    append order is NOT layer order: per the field docs ("appended after task's
    base templates") the task's own templates are the BASE and experiment-defaults
    + variant are overlays appended after — i.e. task -> exp-defaults -> variant.
    `driver` is kept in the dumps: the top-level `driver` field is layered AFTER,
    so it overrides a nested `sandbox.driver` while a nested-only driver still
    contributes. env_passthrough_extra appends in layer order via its strategy.

    Returns the layer list; the caller passes it to ``resolve_root("sandbox", …)``.
    """
    sandbox_layers: list[Layer] = []
    if default_experiment.defaults and default_experiment.defaults.sandbox:
        p = default_experiment.defaults.sandbox.model_dump(exclude_unset=True)
        p.pop("template_sources", None)
        if p:
            sandbox_layers.append(Layer(source="default", patch=p))
    if experiment.defaults and experiment.defaults.sandbox:
        p = experiment.defaults.sandbox.model_dump(exclude_unset=True)
        p.pop("template_sources", None)
        if p:
            sandbox_layers.append(Layer(source="experiment-defaults", patch=p))
    if experiment.defaults and experiment.defaults.driver is not None:
        sandbox_layers.append(Layer(source="experiment-defaults", patch={"driver": experiment.defaults.driver}))
    task_sandbox_patch = task.sandbox.model_dump(exclude_unset=True)
    task_template_sources = task_sandbox_patch.pop("template_sources", None)
    if task_sandbox_patch:
        sandbox_layers.append(Layer(source="task", patch=task_sandbox_patch))
    if variant.driver is not None:
        sandbox_layers.append(Layer(source="variant", patch={"driver": variant.driver}, detail=variant.variant_id))
    # template_sources synthetic layers, in documented task-first append order:
    # task base, then experiment-defaults overlay, then variant overlay.
    if task_template_sources:
        sandbox_layers.append(Layer(source="task", patch={"template_sources": task_template_sources}))
    if experiment.defaults and experiment.defaults.template_sources:
        sandbox_layers.append(
            Layer(
                source="experiment-defaults",
                patch={"template_sources": [s.model_dump() for s in experiment.defaults.template_sources]},
            )
        )
    if variant.template_sources:
        sandbox_layers.append(
            Layer(source="variant", patch={"template_sources": [s.model_dump() for s in variant.template_sources]})
        )
    return sandbox_layers


def resolve_task_for_variant(
    default_experiment: ExperimentDefinition,
    task: TaskDefinition,
    experiment: ExperimentDefinition,
    variant: ExperimentVariant,
    config: BatchRunConfig | None = None,
) -> tuple[TaskDefinition, dict[str, ConfigLineageEntry], int]:
    """Resolve a fully-configured TaskDefinition by merging the 4-layer precedence chain.

    Precedence (lowest to highest):
        1. default_experiment.defaults.agent   (global baseline defaults)
        2. experiment.defaults.agent           (experiment-wide defaults, below task)
        3. task.agent                          (task-explicit fields only via exclude_unset)
        4. variant.agent                       (per-variant overrides, highest)

    After resolution, CLI overrides (layer 5) are applied separately
    by _apply_cli_overrides().

    Args:
        default_experiment: The default experiment (experiments/default.yaml).
        task: The original task definition (may have agent=None).
        experiment: The active experiment definition.
        variant: The specific variant to resolve for.
        config: Optional batch run config; used to resolve ``repeats``.

    Returns:
        Tuple of (resolved TaskDefinition, config lineage dict, effective_repeats).
    """
    # Layer 1-4 raw agent dicts. Experiment-side dicts are dict[str, Any] (passed
    # through verbatim); the task agent is dumped with exclude_unset so Pydantic
    # defaults don't leak into the merge. Timing (max_turns/turn_timeout) belongs
    # under run_limits — a legacy max_turns under agent: now fails loudly via the
    # agent model's extra="forbid" rather than being silently hoisted.
    default_agent = default_experiment.defaults.agent if default_experiment.defaults else None
    exp_defaults_agent = experiment.defaults.agent if experiment.defaults else None
    variant_agent_clean = variant.agent
    task_agent = task.agent.model_dump(exclude_unset=True) if task.agent else None

    # Resolve all three `-D`-reachable roots through the SAME generic resolver
    # the CLI layer uses (config_merge.resolve_root) — one merge implementation,
    # one set of per-field strategies, lineage emitted as a side effect into the
    # shared `lineage` dict. Type is enforced after CLI overrides (layer 5) so
    # `--type` can satisfy the contract for tasks that omit `agent.type`.
    lineage: dict[str, ConfigLineageEntry] = {}

    # --- agent: layers default -> exp-defaults -> task -> variant ---
    # No-op (agent: {type: none}) tasks need no special-casing here: `type` is a
    # replace-scalar, so a task-level `type: none` wins over a baseline coding
    # agent injected by the default experiment, and the merged config validates
    # as NoneAgentConfig. The orchestrator then dispatches to NoOpAgent.
    resolved_agent: AgentConfig | BaseAgentConfig | None
    agent_layers: list[Layer] = []
    agent_specs: list[tuple[ConfigSource, dict[str, Any] | None]] = [
        ("default", default_agent),
        ("experiment-defaults", exp_defaults_agent),
        ("task", task_agent),
        ("variant", variant_agent_clean),
    ]
    for source, patch in agent_specs:
        if patch:
            agent_layers.append(Layer(source=source, patch=patch))
    resolved_agent = resolve_root("agent", agent_layers, lineage=lineage)
    assert resolved_agent is not None  # parse_agent_config always returns a model

    # --- run_limits: the canonical RunLimits blocks across the 4 layers. ---
    rl_layers: list[Layer] = []

    def _add_rl(rl: RunLimits | None, source: ConfigSource) -> None:
        if rl is None:
            return
        # exclude_unset (not exclude_none): count_cached_input defaults to False;
        # exclude_none would write it back, clobbering a True from a lower layer.
        patch = rl.model_dump(exclude_unset=True)
        if patch:
            rl_layers.append(Layer(source=source, patch=patch))

    if default_experiment.defaults:
        _add_rl(default_experiment.defaults.run_limits, "default")
    if experiment.defaults:
        _add_rl(experiment.defaults.run_limits, "experiment-defaults")
    _add_rl(task.run_limits, "task")
    _add_rl(variant.run_limits, "variant")
    resolved_run_limits = resolve_root("run_limits", rl_layers, lineage=lineage)

    # --- sandbox: synthetic-layer construction (driver precedence + task-first
    # template_sources ordering) lives in _build_sandbox_layers. ---
    sandbox_layers = _build_sandbox_layers(default_experiment, experiment, task, variant)
    # resolve_root returns None only when no layer set anything; fall back to the
    # task's own (default) sandbox so resolved_task.sandbox is never None.
    resolved_sandbox = resolve_root("sandbox", sandbox_layers, lineage=lineage) or task.sandbox

    # Resolve pre_run / post_run through the same engine via their declared append
    # strategies: pre_run appends in layer order (exp-defaults setup first, then the
    # task's); post_run uses append_order="reverse" (the task's commands first, then
    # experiment-defaults cleanup-last). Only exp-defaults + task contribute.
    prepost_layers: list[Layer] = []
    exp_patch: dict[str, Any] = {}
    if experiment.defaults and experiment.defaults.pre_run:
        exp_patch["pre_run"] = list(experiment.defaults.pre_run)
    if experiment.defaults and experiment.defaults.post_run:
        exp_patch["post_run"] = list(experiment.defaults.post_run)
    if exp_patch:
        prepost_layers.append(Layer(source="experiment-defaults", patch=exp_patch))
    task_patch: dict[str, Any] = {}
    if task.pre_run:
        task_patch["pre_run"] = list(task.pre_run)
    if task.post_run:
        task_patch["post_run"] = list(task.post_run)
    if task_patch:
        prepost_layers.append(Layer(source="task", patch=task_patch))
    prepost = merge_layers((TaskDefinition,), prepost_layers, lineage_root="task") if prepost_layers else {}
    resolved_pre_run = prepost.get("pre_run", [])
    resolved_post_run = prepost.get("post_run", [])

    # Resolve simulation: shallow-merge across default → experiment-defaults → task → variant.
    # Mirrors agent merge semantics — a later layer's keys overwrite earlier ones, and
    # the final dict is validated by building a SimulationConfig from it.
    resolved_simulation = _resolve_simulation(default_experiment, experiment, task, variant, lineage)
    resolved_checker_context = _resolve_checker_context(default_experiment, experiment, task, variant, lineage)

    # Build resolved task (copy with overrides)
    resolved_task = task.model_copy(
        update={
            "agent": resolved_agent,
            "run_limits": resolved_run_limits,
            "sandbox": resolved_sandbox,
            "post_run": resolved_post_run,
            "pre_run": resolved_pre_run,
            "simulation": resolved_simulation,
            "checker_context": resolved_checker_context,
        }
    )

    # Resolve repeats (4-layer: default → experiment-defaults → variant → cli; skips task layer)
    _config = config if config is not None else BatchRunConfig(run_dir=Path("."))
    effective_repeats = _resolve_repeats(default_experiment, experiment, variant, _config, lineage)

    # When no config was supplied (direct callers / tests), enforce the agent.type
    # contract here — _apply_cli_overrides won't run to do it later. A no-op task's
    # `type: none` satisfies it (type is set), so only a truly type-less agent trips.
    if config is None and resolved_agent is not None and resolved_agent.type is None:
        raise ValueError(
            "Agent 'type' is required but was not set by any layer (default experiment, "
            + f"experiment defaults, task, or variant) for task {task.task_id!r}. "
            + "Set it in the task YAML or the experiment."
        )

    return resolved_task, lineage, effective_repeats


def _apply_cli_overrides(
    task: TaskDefinition,
    config: BatchRunConfig,
    lineage: dict[str, ConfigLineageEntry] | None = None,
) -> None:
    """Apply CLI overrides (layer 5) to a task definition in-place.

    Layer 5 is CLI-only: ``-D``/``--set`` entries plus the bespoke flag
    aliases. The actual merge + re-validation is delegated to
    ``orchestration.overrides.apply_overrides``.

    Args:
        task: The task definition to mutate.
        config: Batch run configuration containing CLI overrides.
        lineage: Optional lineage dict to update with CLI override entries.
    """
    from .overrides import apply_overrides

    # No-op (agent: {type: none}) tasks need no special-casing: the resolved agent
    # is a NoneAgentConfig, so a suite-wide `--model` / `-D agent.*` lands on it
    # harmlessly (NoOpAgent ignores them) and the `type: none` already satisfies the
    # agent.type contract. An explicit `--type <x>` is highest precedence and, as for
    # any task, replaces the type — turning the no-op task into that agent.
    assert task.agent is not None, f"Task '{task.task_id}' has no agent config"

    apply_overrides(task, config.overrides, agent_type=config.agent_type, lineage=lineage)

    # Final guard: agent.type must be set after all 5 layers have merged.
    if task.agent.type is None:
        raise ValueError(
            "Agent 'type' is required but was not set by any layer (default experiment, "
            + f"experiment defaults, task, variant, or CLI) for task {task.task_id!r}. "
            + "Set it in the task YAML, the experiment, or via --type."
        )


def resolve_task_files(
    task: TaskDefinition,
    task_file: Path,
    experiment_file: Path | None = None,
) -> None:
    """Resolve relative file paths injected by experiment variants.

    Paths already resolved to absolute by load_task() are skipped.
    New relative paths (from variant/base) resolve from experiment_file.parent.
    """
    exp_dir = experiment_file.parent if experiment_file is not None else task_file.parent

    # Resolve system_prompt_file (may be injected by variant as relative or absolute path).
    # Rebind: the resolver returns a new config so the prompt/file swap is atomic
    # (see resolve_agent_system_prompt).
    task.agent = resolve_agent_system_prompt(task.agent, exp_dir)

    # Resolve relative template_sources paths
    if task.sandbox.template_sources:
        resolve_template_source_paths(task.sandbox.template_sources, exp_dir)


def resolve_all_tasks(
    task_files: list[Path],
    experiment: ExperimentDefinition,
    default_experiment: ExperimentDefinition,
    config: BatchRunConfig,
    experiment_file: Path | None = None,
) -> tuple[list[ResolvedTask], list[SkippedTask]]:
    """Resolve all (task x variant) combinations into typed, run-ready entries.

    Applies all 5 config layers in one place:
        1. default experiment base
        2. task YAML
        3. experiment base
        4. variant overrides
        5. CLI overrides

    Also handles tag filtering and unique task ID validation.

    Task YAMLs that fail to load (YAML parse error, Pydantic validation,
    dataset expansion error) are recorded in the returned ``skipped`` list and
    excluded from the resolved set rather than aborting the suite.

    Per-task config-resolution failures (a task whose own YAML is incompatible
    with the resolved run — e.g. Claude-only ``sdk_options`` surviving a
    ``--type codex`` override, which ``CodexAgentConfig`` forbids) are likewise
    demoted to ``skipped`` — but only when other tasks resolve. If EVERY task
    that reaches resolution fails, the cause is a global invocation error (a bad
    ``--type`` / ``-D`` value, repeats over the cap) rather than a per-task
    incompatibility, so it is re-raised and aborts the run. Early-stop arming
    errors always propagate. The caller surfaces ``skipped`` in the run summary
    so per-task failures are loud but recoverable.

    Args:
        task_files: Paths to task YAML files.
        experiment: The active experiment definition.
        default_experiment: The default experiment (experiments/default.yaml).
        config: Batch run configuration (provides CLI overrides, tags, run_dir).
        experiment_file: Path to the experiment YAML file. Used to resolve
            relative paths injected by experiment variants. Falls back to task
            file directory when None.

    Returns:
        Tuple of (resolved tasks ready for run_batch, skipped task records).

    Raises:
        ValueError: If duplicate task IDs are found after resolution.
    """
    from .early_stop import EarlyStopConfigError, validate_early_stop

    resolved: list[ResolvedTask] = []
    skipped: list[SkippedTask] = []
    # Per-task config-resolution failures are collected here rather than raised
    # inline. After the loop they are demoted to ``skipped`` — UNLESS every task
    # that reached resolution failed, which signals a global invocation error
    # (bad --type, an invalid -D value, repeats over the cap) that trips every
    # task identically rather than a per-task incompatibility; that case is
    # re-raised so the CLI aborts cleanly instead of producing an empty run.
    resolution_errors: list[tuple[Path, Exception]] = []
    attempted = 0

    # Resolve variant-level initial_prompt_file paths before the main loop
    exp_dir = experiment_file.parent if experiment_file is not None else None
    for variant in experiment.variants:
        if variant.initial_prompt_file is not None:
            if exp_dir is None:
                raise ValueError(
                    f"variant '{variant.variant_id}' uses initial_prompt_file but no experiment file path "
                    + "is available for resolving relative paths"
                )
            resolve_variant_initial_prompt_file(variant, exp_dir)

    for task_file in task_files:
        try:
            task, source_yaml = load_task(task_file)
            # Honor `skip: true` before dataset expansion — quarantined tasks
            # skip row fan-out, variant resolution, and any further I/O. The
            # task is reported in RunSummary.skipped_tasks so the suite shows
            # which YAMLs were intentionally excluded vs. failed to load.
            # Bypassed by --include-skipped (config.include_skipped) so on-demand /
            # local runs can execute quarantined or opt-in tasks; the nightly/CI
            # leave the flag off and keep excluding them.
            if task.skip and not config.include_skipped:
                reason = f"skip: true (task_id={task.task_id!r})"
                logger.info("Skipping task %s — skip: true in YAML", task.task_id)
                skipped.append(SkippedTask(path=str(task_file), reason=reason))
                continue
            # Dataset fan-out BEFORE variant resolution: one task per row, each
            # treated as an independent task for the 4-layer merge below. This
            # locks the invariant that variants cannot override the dataset.
            expanded_tasks = expand_dataset(
                task,
                task_file.parent,
                max_rows=config.max_rows,
                sample_per_stratum=config.sample_per_stratum,
            )
        # Narrow set: real load failures only. We deliberately don't catch
        # AttributeError / TypeError / ImportError — those signal a regression
        # in load_task / expand_dataset and should crash loudly rather than
        # silently demote every task to "skipped". Pydantic ValidationError
        # is a ValueError subclass in v2, so it's covered.
        except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("Skipping task file %s — %s", task_file, reason)
            skipped.append(SkippedTask(path=str(task_file), reason=reason))
            continue

        # Isolate layer 1-5 config resolution per task file. A task whose own
        # YAML config is incompatible with the resolved run — e.g. Claude-only
        # agent fields (`sdk_options`) surviving a `--type codex` override, which
        # `CodexAgentConfig` forbids (`extra="forbid"`) — raises here. Without
        # isolation that single task aborts the entire coder-eval run. Buffer the
        # file's resolved tasks and commit them only once the whole file
        # resolves, so a mid-file failure discards this file's fan-out as a unit
        # (mirroring the load/expand isolation above) rather than leaving a
        # partial, lopsided fan-out behind.
        attempted += 1
        file_resolved: list[ResolvedTask] = []
        try:
            for expanded_task in expanded_tasks:
                for variant in experiment.variants:
                    # Apply layers 1-4 (default → experiment-defaults → task → variant) + resolve repeats
                    resolved_task, lineage, effective_repeats = resolve_task_for_variant(
                        default_experiment, expanded_task, experiment, variant, config
                    )

                    # Resolve file paths injected by variant overrides
                    resolve_task_files(resolved_task, task_file, experiment_file)

                    # Apply prompt mutations or overrides (between file resolution and CLI overrides)
                    _apply_prompt_overrides(resolved_task, experiment, variant, lineage)

                    # Apply layer 5 (CLI overrides)
                    _apply_cli_overrides(resolved_task, config, lineage)

                    # Early-stop guardrails: run once the task is fully resolved (all 5
                    # layers merged, incl. the -D run_limits.stop_early kill switch). No-op unless armed;
                    # a bad arming raises EarlyStopConfigError (a ValueError).
                    validate_early_stop(resolved_task)

                    # Fan-out: simulation n_trials takes precedence over experiment repeats
                    # when simulation is active; otherwise use experiment-level repeats.
                    sim = resolved_task.simulation
                    n_trials = sim.n_trials if (sim is not None and sim.enabled) else 1
                    fan_count = n_trials if n_trials > 1 else effective_repeats
                    for rep in range(fan_count):
                        file_resolved.append(
                            ResolvedTask(
                                task=resolved_task,
                                task_file=task_file,
                                run_dir=build_task_run_dir(
                                    config.run_dir,
                                    variant.variant_id,
                                    resolved_task.task_id,
                                    replicate_index=rep,
                                ),
                                variant_id=variant.variant_id,
                                replicate_index=rep,
                                source_yaml=source_yaml,
                                config_lineage=dict(lineage),
                            )
                        )
        # Early-stop arming errors are a deliberate hard stop: they always
        # propagate (never demoted to skipped) so a misconfigured run fails loudly
        # instead of quietly shrinking the suite.
        except EarlyStopConfigError:
            raise
        # Narrow set, matching the load/expand block above: config-resolution
        # and IO failures are collected (decided after the loop, below);
        # AttributeError / TypeError / ImportError still crash loudly as
        # regressions. Pydantic ValidationError is a ValueError subclass in v2,
        # so it's covered.
        except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
            resolution_errors.append((task_file, exc))
            continue

        resolved.extend(file_resolved)

    # Decide the fate of collected per-task resolution failures. If EVERY task
    # that reached resolution failed, refusing to proceed (rather than producing
    # an empty run) is the right call. We surface the first task's own error —
    # not a synthesized "global misconfig" message — because we can't actually
    # tell a genuine global cause (a bad --type / -D value that trips every task
    # identically) from N tasks each independently incompatible for the same
    # per-task reason. A ValueError (incl. Pydantic ValidationError and the "no
    # agent registered" guard) is re-raised verbatim so its message stays clean;
    # only a non-ValueError (FileNotFoundError/OSError/yaml.YAMLError — e.g. a
    # missing system_prompt_file) is normalized to ValueError so it still lands
    # in the caller's `except ValueError` (clean typer.BadParameter) instead of
    # escaping as a raw traceback.
    if resolution_errors and len(resolution_errors) == attempted:
        first_exc = resolution_errors[0][1]
        if isinstance(first_exc, ValueError):
            raise first_exc
        raise ValueError(str(first_exc)) from first_exc
    for err_file, exc in resolution_errors:
        reason = f"{type(exc).__name__}: {exc}"[:500]
        logger.warning("Skipping task file %s — config resolution failed: %s", err_file, reason)
        skipped.append(SkippedTask(path=str(err_file), reason=reason))

    # Filter by tags
    if config.include_tags or config.exclude_tags:
        from .batch import filter_tasks_by_tags

        tagged = [(rt.task_file, rt.task) for rt in resolved]
        filtered = filter_tasks_by_tags(tagged, include_tags=config.include_tags, exclude_tags=config.exclude_tags)
        filtered_ids = {t.task_id for _, t in filtered}
        resolved = [rt for rt in resolved if rt.task.task_id in filtered_ids]

    # Validate no duplicate (task_id, variant_id, replicate_index) combinations.
    # Simulation replicates legitimately share (task_id, variant_id); the tuple
    # is only a duplicate when the replicate_index also matches.
    seen: dict[tuple[str, str, int], list[Path]] = {}
    for rt in resolved:
        key = (rt.task.task_id, rt.variant_id, rt.replicate_index)
        seen.setdefault(key, []).append(rt.task_file)
    duplicates = {k: files for k, files in seen.items() if len(files) > 1}
    if duplicates:
        lines = [
            f"  - '{tid}' (variant '{vid}', replicate {rep}): {', '.join(str(f) for f in files)}"
            for (tid, vid, rep), files in duplicates.items()
        ]
        raise ValueError("Duplicate task IDs found:\n" + "\n".join(lines))

    # Sort so tasks run interleaved: replicate 0 of every (task, variant) first,
    # then replicate 1, etc. Within the same replicate, preserve original
    # task-file and variant declaration order.
    task_order = {tf: i for i, tf in enumerate(dict.fromkeys(rt.task_file for rt in resolved))}
    variant_order = {v.variant_id: i for i, v in enumerate(experiment.variants)}
    resolved.sort(key=lambda rt: (rt.replicate_index, task_order[rt.task_file], variant_order[rt.variant_id]))

    return resolved, skipped


def _pick_worst_status(statuses: list[FinalStatus]) -> FinalStatus:
    """Pick the worst final_status across replicates (error > failed > succeeded).

    Unknown categories fall back to priority -1 so they sort as worst-of-all
    (fail-closed: a new unrecognised status becomes the most urgent).

    "ungraded" sorts LEAST urgent (above "succeeded") — it carries no verdict, so
    any replicate that does have one must win. It therefore survives only when
    every replicate is ungraded, which is the only case reachable today anyway
    (``grade`` is run-level, so replicates never mix).
    """
    priority = {"error": 0, "failed": 1, "succeeded": 2, "ungraded": 3}
    return min(statuses, key=lambda s: priority.get(s.category, -1))


def _mean_graded_score(vr_list: list[VariantResult]) -> float | None:
    """Mean ``weighted_score`` over the graded rows; ``None`` when none were graded."""
    graded = [v.weighted_score for v in vr_list if v.weighted_score is not None]
    return sum(graded) / len(graded) if graded else None


def _mean_reference_similarity(reps: list[TaskResult]) -> float | None:
    """Return the mean reference_comparison score across replicates that have one."""
    scores = [
        cr.score
        for r in reps
        for cr in r.result.success_criteria_results
        if cr.criterion_type == "reference_comparison"
    ]
    return sum(scores) / len(scores) if scores else None


def aggregate_results(
    experiment_id: str,
    description: str,
    variant_ids: list[str],
    task_results: list[TaskResult],
    total_duration: float,
) -> ExperimentResult:
    """Aggregate typed task results into an ExperimentResult with cross-variant comparisons.

    Replicates of the same (task_id, variant_id) are folded into a single
    VariantResult whose weighted_score is the mean across replicates.
    Per-replicate raw scores are preserved in ExperimentResult.per_replicate_scores
    for statistical analysis.

    Args:
        experiment_id: Identifier for the experiment.
        description: Human-readable description.
        variant_ids: List of variant IDs in the experiment.
        task_results: Typed results from run_batch execution.
        total_duration: Total wall-clock duration in seconds.

    Returns:
        ExperimentResult with task summaries and variant aggregates.
    """
    # Group by (task_id, variant_id) — replicates of the same (task, variant) fold into one VariantResult.
    task_variant_reps: dict[tuple[str, str], list[TaskResult]] = {}
    for tr in task_results:
        task_variant_reps.setdefault((tr.task_id, tr.variant_id), []).append(tr)

    # Collect per-replicate scores keyed variant_id → task_id → [scores] for stats rendering.
    per_replicate_scores: dict[str, dict[str, list[float]]] = {}
    for (task_id, variant_id), reps in task_variant_reps.items():
        per_replicate_scores.setdefault(variant_id, {})[task_id] = [
            r.result.weighted_score for r in reps if r.result.weighted_score is not None
        ]

    task_variants: dict[str, list[VariantResult]] = {}
    for (task_id, variant_id), reps in task_variant_reps.items():
        # Ungraded replicates drop out entirely rather than contributing 0.0 —
        # `or 0.0` would average a clean `execute` run down to a real-looking zero.
        scores = [r.result.weighted_score for r in reps if r.result.weighted_score is not None]
        non_errored = [r for r in reps if r.result.final_status.category != "error"]
        durations = [r.result.duration_seconds for r in non_errored]
        statuses = [r.result.final_status for r in reps]
        iter_counts = [r.result.iteration_count for r in reps if r.result.iteration_count is not None]
        asst_turns = [r.result.total_assistant_turns for r in reps if r.result.total_assistant_turns is not None]
        token_vals = [r.result.total_token_usage.total_tokens for r in reps if r.result.total_token_usage is not None]
        ref_similarity = _mean_reference_similarity(reps)
        final_status = _pick_worst_status(statuses)

        variant_result = VariantResult(
            variant_id=variant_id,
            task_id=task_id,
            weighted_score=sum(scores) / len(scores) if scores else None,
            final_status=final_status,
            duration_seconds=sum(durations),
            total_tokens=sum(token_vals) if token_vals else None,
            iteration_count=round(sum(iter_counts) / len(iter_counts)) if iter_counts else None,
            total_assistant_turns=round(sum(asst_turns) / len(asst_turns)) if asst_turns else None,
            reference_similarity=ref_similarity,
            replicate_index=0,  # aggregate — points at first replicate for link rendering
            replicate_count=len(reps),
        )
        task_variants.setdefault(task_id, []).append(variant_result)

    # Build task summaries
    task_summaries: list[TaskExperimentSummary] = []
    for task_id, variants in task_variants.items():
        # Only graded variants can win or set a spread. Including ungraded ones
        # at 0.0 would name an arbitrary "best" among scores that do not exist.
        scored = [(v, v.weighted_score) for v in variants if v.weighted_score is not None]
        best = max(scored, key=lambda pair: (pair[1], pair[0].variant_id))[0] if scored else variants[0]
        scores = [s for _, s in scored]
        top_count = sum(1 for _, s in scored if s == best.weighted_score)
        rep_counts = {v.replicate_count for v in variants}
        task_summaries.append(
            TaskExperimentSummary(
                task_id=task_id,
                variant_results=variants,
                best_variant=best.variant_id,
                is_tie=top_count > 1,
                score_spread=(max(scores) - min(scores)) if scores else 0.0,
                replicate_count=min(rep_counts) if rep_counts else 1,
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
                tasks_error=0,
                average_score=None,
                average_duration=0.0,
            )
            continue

        token_values = [v.total_tokens for v in vr_list if v.total_tokens is not None]
        total_tokens = sum(token_values) if token_values else None
        variant_aggregates[vid] = VariantAggregate(
            variant_id=vid,
            tasks_run=len(vr_list),
            tasks_succeeded=sum(1 for v in vr_list if v.final_status.category == "succeeded"),
            tasks_failed=sum(1 for v in vr_list if v.final_status.category == "failed"),
            tasks_error=sum(1 for v in vr_list if v.final_status.category == "error"),
            tasks_not_graded=sum(1 for v in vr_list if v.final_status.category == "ungraded"),
            tasks_token_budget_exceeded=sum(1 for v in vr_list if v.final_status == FinalStatus.TOKEN_BUDGET_EXCEEDED),
            tasks_cost_budget_exceeded=sum(1 for v in vr_list if v.final_status == FinalStatus.COST_BUDGET_EXCEEDED),
            # Mean over GRADED rows only, and None when there are none: a clean
            # execute run has no average score, and reporting 0.000 next to
            # "Pass Rate: n/a" is a number indistinguishable from "scored zero".
            average_score=_mean_graded_score(vr_list),
            average_duration=sum(v.duration_seconds / v.replicate_count for v in vr_list) / len(vr_list),
            total_tokens=total_tokens,
            replicate_count=vr_list[0].replicate_count if vr_list else 1,
        )

    return ExperimentResult(
        experiment_id=experiment_id,
        description=description,
        variant_ids=variant_ids,
        task_summaries=task_summaries,
        variant_aggregates=variant_aggregates,
        total_duration_seconds=total_duration,
        per_replicate_scores=per_replicate_scores,
    )
