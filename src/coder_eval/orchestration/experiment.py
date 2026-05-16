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
import warnings
from pathlib import Path
from typing import Any, Literal

import yaml

from ..models import (
    AgentConfig,
    AgentKind,
    ConfigLineageEntry,
    ExperimentDefinition,
    ExperimentResult,
    ExperimentVariant,
    FinalStatus,
    PromptRephrase,
    ResolvedTask,
    RunLimits,
    SandboxConfig,
    SimulationConfig,
    SkippedTask,
    SnapshotMode,
    TaskDefinition,
    TaskExperimentSummary,
    TaskResult,
    TemplateSource,
    VariantAggregate,
    VariantResult,
    apply_prompt_mutations,
    validate_template_sources_list,
)
from ..path_utils import build_task_run_dir
from .config import BatchRunConfig
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

    with open(experiment_file) as f:
        data = yaml.safe_load(f)

    try:
        exp = ExperimentDefinition(**data)
        _resolve_experiment_template_paths(exp, experiment_file.parent)
        return exp
    except Exception as e:
        raise ValueError(f"Invalid experiment definition: {e}") from e


def _hoist_agent_timing_dict(
    agent_dict: dict[str, Any] | None,
    *,
    layer_label: str,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """Pop legacy ``max_turns``/``turn_timeout`` out of an agent dict.

    Mirrors ``TaskDefinition._hoist_legacy_agent_timing`` for experiment-side
    agent dicts (``ExperimentDefaults.agent``, ``ExperimentVariant.agent``)
    which are typed ``dict[str, Any]`` and bypass Pydantic validation.

    Emits a single ``DeprecationWarning`` per hoisted field. ``layer_label``
    is included in the warning text so users can pinpoint which layer is on
    the legacy shape (e.g. ``"experiment defaults"``, ``"variant 'sonnet'"``).
    Scheduled removal: 2026-05-20.

    Returns:
        Tuple of ``(cleaned_dict_or_None, run_limits_patch)``. The cleaned
        dict is ``None`` when the input was ``None`` or when removing the
        timing keys leaves it empty. ``run_limits_patch`` is a partial dict
        keyed by ``RunLimits`` field names; empty when no hoisting occurred.
    """
    if agent_dict is None:
        return None, {}
    cleaned = dict(agent_dict)
    patch: dict[str, int] = {}
    for name in ("max_turns", "turn_timeout"):
        if name in cleaned:
            val = cleaned.pop(name)
            if val is None:
                continue
            patch[name] = val
            warnings.warn(
                f"{name!r} under {layer_label} agent: is deprecated and will be removed on "
                + f"2026-05-20; move it to run_limits.{name}.",
                DeprecationWarning,
                stacklevel=3,
            )
    return (cleaned or None), patch


def _merge_agent_dicts(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """Merge multiple partial agent config dicts (left to right, later wins).

    Shallow merge only — each layer's keys overwrite the previous entirely.
    Lists and nested dicts are replaced, not recursively merged.
    None layers are skipped.

    Handles mutually exclusive prompt fields: when a layer sets system_prompt,
    the previously merged system_prompt_file is cleared, and vice versa.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer is not None:
            if layer.get("system_prompt") is not None:
                merged.pop("system_prompt_file", None)
            if layer.get("system_prompt_file") is not None:
                merged.pop("system_prompt", None)
            merged.update(layer)
    return merged


type ConfigSource = Literal[
    "default",
    "task",
    "experiment-defaults",
    "variant",
    "cli",
    "default-agent-deprecated",
    "experiment-defaults-agent-deprecated",
    "variant-agent-deprecated",
]


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

    The merge is a shallow dict merge (same semantics as agent merge):
    each layer's keys overwrite earlier ones, lists are replaced not
    appended. After merging, the final dict is validated by constructing
    a SimulationConfig.
    """
    default_sim = default_experiment.defaults.simulation if default_experiment.defaults else None
    exp_sim = experiment.defaults.simulation if experiment.defaults else None
    task_sim_dict = task.simulation.model_dump(exclude_unset=True) if task.simulation else None
    variant_sim = variant.simulation

    layers = [default_sim, exp_sim, task_sim_dict, variant_sim]
    if all(layer is None for layer in layers):
        return None

    merged: dict[str, Any] = {}
    for layer in layers:
        if layer is not None:
            merged.update(layer)

    # Persona and goal are required by the model. If no layer provided them
    # (e.g. an experiment enables simulation defaults without a persona), the
    # resulting SimulationConfig construction will raise with a helpful error.
    resolved = SimulationConfig(**merged)

    # Track lineage — record the most-specific (highest-precedence) non-None
    # source explicitly via reversed iteration, so the recorded source is
    # correct regardless of the order ``sim_layers`` is declared in.
    sim_layers: list[tuple[ConfigSource, dict[str, Any] | None]] = [
        ("default", default_sim),
        ("experiment-defaults", exp_sim),
        ("task", task_sim_dict),
        ("variant", variant_sim),
    ]
    most_specific: ConfigSource | None = next(
        (source_name for source_name, layer in reversed(sim_layers) if layer),
        None,
    )
    if most_specific is not None:
        lineage["simulation"] = ConfigLineageEntry(value=merged, source=most_specific)

    return resolved


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

    # Lazily create rephrase_fn only if any rephrase mutation exists
    rephrase_fn = None
    if any(isinstance(m, PromptRephrase) for m in combined):
        from .rephrase import create_rephrase_fn

        rephrase_fn = create_rephrase_fn()

    try:
        task.initial_prompt = apply_prompt_mutations(task.initial_prompt, combined, rephrase_fn=rephrase_fn)
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

    After resolution, CLI / .env overrides (layer 5) are applied separately
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
    # Layer 1-4 raw agent dicts. Hoist legacy max_turns/turn_timeout out of the
    # experiment-side dicts up front (task.agent is already pre-hoisted by
    # TaskDefinition._hoist_legacy_agent_timing into task.run_limits). The
    # hoisted patches feed into the field-merge accumulator below alongside
    # the canonical RunLimits blocks.
    default_agent, default_agent_rl_patch = _hoist_agent_timing_dict(
        default_experiment.defaults.agent if default_experiment.defaults else None,
        layer_label="default experiment defaults",
    )
    exp_defaults_agent, exp_defaults_agent_rl_patch = _hoist_agent_timing_dict(
        experiment.defaults.agent if experiment.defaults else None,
        layer_label="experiment defaults",
    )
    variant_agent_clean, variant_agent_rl_patch = _hoist_agent_timing_dict(
        variant.agent,
        layer_label=f"variant '{variant.variant_id}'",
    )

    # Layer 3: task agent (only explicitly-set fields, not Pydantic defaults)
    task_agent = task.agent.model_dump(exclude_unset=True) if task.agent else None

    # Merge agent dicts. Type is enforced after CLI overrides (layer 5) are
    # applied — see _apply_cli_overrides — so `--type` can satisfy the contract
    # for tasks that omit `agent.type` entirely.
    merged_agent_dict = _merge_agent_dicts(default_agent, exp_defaults_agent, task_agent, variant_agent_clean)
    resolved_agent = AgentConfig(**merged_agent_dict)

    # Build agent lineage
    agent_lineage = _build_agent_lineage(
        [
            ("default", default_agent),
            ("experiment-defaults", exp_defaults_agent),
            ("task", task_agent),
            ("variant", variant_agent_clean),
        ]
    )

    # Resolve run_limits via field-merge across all 4 layers. Later layers
    # overwrite individual keys; absent keys leave earlier values intact.
    scalar_lineage: dict[str, ConfigLineageEntry] = {}
    rl_accum: dict[str, Any] = {}
    rl_lineage: dict[str, ConfigSource] = {}

    def _merge_rl(
        layer_rl: RunLimits | dict[str, Any] | None,
        source: ConfigSource,
    ) -> None:
        if layer_rl is None:
            return
        if isinstance(layer_rl, RunLimits):
            # exclude_unset (not exclude_none): non-Optional fields like
            # count_cached_input have a default of False, so exclude_none
            # would always include them in the patch, clobbering a True from
            # a lower-precedence layer that DID set it. exclude_unset emits
            # only the fields the caller explicitly provided.
            patch = layer_rl.model_dump(exclude_unset=True)
        else:
            patch = {k: v for k, v in layer_rl.items() if v is not None}
        for k, v in patch.items():
            rl_accum[k] = v
            rl_lineage[k] = source

    # Within each layer, merge the legacy agent-hoisted patch FIRST so the
    # canonical run_limits block wins on conflict (preserves the existing
    # "top-level wins over agent-hoisted in the same layer" precedence rule).
    # Layer 1: default experiment defaults
    _merge_rl(default_agent_rl_patch, "default-agent-deprecated")
    if default_experiment.defaults:
        _merge_rl(default_experiment.defaults.run_limits, "default")
    # Layer 2: experiment defaults
    _merge_rl(exp_defaults_agent_rl_patch, "experiment-defaults-agent-deprecated")
    if experiment.defaults:
        _merge_rl(experiment.defaults.run_limits, "experiment-defaults")
    # Layer 3: task (its own agent-level hoist already happened inside
    # TaskDefinition._hoist_legacy_agent_timing).
    _merge_rl(task.run_limits, "task")
    # Layer 4: variant
    _merge_rl(variant_agent_rl_patch, "variant-agent-deprecated")
    _merge_rl(variant.run_limits, "variant")

    resolved_run_limits = RunLimits(**rl_accum) if rl_accum else None

    for k, source in rl_lineage.items():
        scalar_lineage[f"run_limits.{k}"] = ConfigLineageEntry(value=rl_accum[k], source=source)

    # Resolve sandbox via field-merge across all 4 layers (default → exp-defaults → task → variant).
    # Later layers overwrite individual keys; absent keys leave earlier values intact.
    # Special case: env_passthrough_extra lists are appended (not overridden).
    sandbox_accum: dict[str, Any] = {}
    sandbox_docker_extras: list[str] = []

    def _merge_sandbox(layer_sandbox: SandboxConfig | None) -> None:
        nonlocal sandbox_docker_extras
        if layer_sandbox is None:
            return
        # Extract only explicitly-set fields using exclude_unset
        patch = layer_sandbox.model_dump(exclude_unset=True)
        # Handle env_passthrough_extra specially: append instead of override
        if "docker" in patch and patch["docker"] and "env_passthrough_extra" in patch["docker"]:
            sandbox_docker_extras.extend(patch["docker"]["env_passthrough_extra"])
            del patch["docker"]["env_passthrough_extra"]
        # Merge remaining fields (later layers win)
        for k, v in patch.items():
            sandbox_accum[k] = v

    # Layer 1: default experiment defaults
    if default_experiment.defaults and default_experiment.defaults.sandbox:
        _merge_sandbox(default_experiment.defaults.sandbox)
    # Layer 2: experiment defaults
    if experiment.defaults and experiment.defaults.sandbox:
        _merge_sandbox(experiment.defaults.sandbox)
    # Layer 3: task
    _merge_sandbox(task.sandbox)
    # Layer 4: variant (variant.sandbox is not yet a field, but future-proofing)
    # For now, variant overrides come via driver and template_sources only.

    # If env_passthrough_extra was accumulated, merge it into docker config
    if sandbox_docker_extras:
        docker_config = sandbox_accum.get("docker") or {}
        existing_extras = docker_config.get("env_passthrough_extra") or []
        docker_config["env_passthrough_extra"] = existing_extras + sandbox_docker_extras
        sandbox_accum["docker"] = docker_config

    # Resolve template_sources: task base + experiment defaults + variant (append semantics)
    base_sources: list[TemplateSource] = list(task.sandbox.template_sources or [])
    exp_defaults_sources: list[TemplateSource] = (
        list(experiment.defaults.template_sources)
        if experiment.defaults and experiment.defaults.template_sources
        else []
    )
    variant_sources: list[TemplateSource] = list(variant.template_sources) if variant.template_sources else []
    combined_sources = base_sources + exp_defaults_sources + variant_sources
    if exp_defaults_sources or variant_sources:
        validate_template_sources_list(combined_sources)
        sandbox_accum["template_sources"] = combined_sources

    # Driver resolution: layer 2 (experiment defaults) → layer 3 (task) → layer 4 (variant).
    if experiment.defaults and experiment.defaults.driver is not None:
        sandbox_accum["driver"] = experiment.defaults.driver
        scalar_lineage["sandbox.driver"] = ConfigLineageEntry(
            value=experiment.defaults.driver, source="experiment-defaults"
        )
    if "driver" in task.sandbox.model_fields_set:
        sandbox_accum["driver"] = task.sandbox.driver
        scalar_lineage["sandbox.driver"] = ConfigLineageEntry(value=task.sandbox.driver, source="task")
    if variant.driver is not None:
        sandbox_accum["driver"] = variant.driver
        scalar_lineage["sandbox.driver"] = ConfigLineageEntry(
            value=variant.driver, source="variant", source_detail=variant.variant_id
        )

    # Reconstruct sandbox with proper model validation to handle nested objects
    if sandbox_accum:
        resolved_sandbox = SandboxConfig(**{**task.sandbox.model_dump(), **sandbox_accum})
    else:
        resolved_sandbox = task.sandbox

    # Resolve post_run: task-level commands first, experiment defaults appended after.
    # Experiment defaults are typically tenant/sandbox cleanup that should run last,
    # after any task-specific artifact extraction.
    exp_defaults_post_run = (
        list(experiment.defaults.post_run) if experiment.defaults and experiment.defaults.post_run else []
    )
    resolved_post_run = list(task.post_run) + exp_defaults_post_run

    # Resolve pre_run: experiment defaults run first (baseline environment setup),
    # task commands appended after (task-specific augmentation).
    exp_defaults_pre_run = (
        list(experiment.defaults.pre_run) if experiment.defaults and experiment.defaults.pre_run else []
    )
    resolved_pre_run = exp_defaults_pre_run + list(task.pre_run)

    # Combine lineage
    lineage = {**agent_lineage, **scalar_lineage}

    # Resolve simulation: shallow-merge across default → experiment-defaults → task → variant.
    # Mirrors agent merge semantics — a later layer's keys overwrite earlier ones, and
    # the final dict is validated by building a SimulationConfig from it.
    resolved_simulation = _resolve_simulation(default_experiment, experiment, task, variant, lineage)

    # Build resolved task (copy with overrides)
    resolved_task = task.model_copy(
        update={
            "agent": resolved_agent,
            "run_limits": resolved_run_limits,
            "sandbox": resolved_sandbox,
            "post_run": resolved_post_run,
            "pre_run": resolved_pre_run,
            "simulation": resolved_simulation,
        }
    )

    # Resolve repeats (4-layer: default → experiment-defaults → variant → cli; skips task layer)
    _config = config if config is not None else BatchRunConfig(run_dir=Path("."))
    effective_repeats = _resolve_repeats(default_experiment, experiment, variant, _config, lineage)

    # When no config was supplied (direct callers / tests), enforce the agent.type
    # contract here — _apply_cli_overrides won't run to do it later.
    if config is None and resolved_agent.type is None:
        raise ValueError(
            f"Agent 'type' is required but was not set by any layer (default experiment, "
            f"experiment defaults, task, or variant) for task {task.task_id!r}. "
            f"Set it in the task YAML or the experiment."
        )

    return resolved_task, lineage, effective_repeats


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

    if config.agent_type is not None:
        task.agent.type = AgentKind(config.agent_type)  # validated by AgentKind enum
        _record("agent.type", config.agent_type, "--type")

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

    # Run-limits overrides (CLI > .env > task YAML). Field-merge into the
    # existing run_limits block so a CLI flag for one key doesn't drop others.
    # exclude_unset preserves user-set Booleans like count_cached_input
    # without polluting rl_base with default-False values that would survive
    # the {**rl_base, **rl_patch} merge below.
    rl_base = task.run_limits.model_dump(exclude_unset=True) if task.run_limits else {}
    rl_patch: dict[str, Any] = {}
    effective_max_turns = config.max_turns if config.max_turns is not None else app_settings.default_max_turns
    if effective_max_turns is not None:
        rl_patch["max_turns"] = effective_max_turns
        detail = "--max-turns" if config.max_turns is not None else ".env DEFAULT_MAX_TURNS"
        _record("run_limits.max_turns", effective_max_turns, detail)
    if config.task_timeout is not None:
        rl_patch["task_timeout"] = config.task_timeout
        _record("run_limits.task_timeout", config.task_timeout, "--task-timeout")
    if config.turn_timeout is not None:
        rl_patch["turn_timeout"] = config.turn_timeout
        _record("run_limits.turn_timeout", config.turn_timeout, "--turn-timeout")
    if rl_patch:
        task.run_limits = RunLimits(**{**rl_base, **rl_patch})

    # Tool/plugin overrides
    if config.allowed_tools is not None:
        task.agent.allowed_tools = config.allowed_tools
        _record("agent.allowed_tools", config.allowed_tools, "--allowed-tools")
    if config.disallowed_tools is not None:
        task.agent.disallowed_tools = config.disallowed_tools
        _record("agent.disallowed_tools", config.disallowed_tools, "--disallowed-tools")
    if config.plugins is not None:
        task.agent.plugins = config.plugins
        _record("agent.plugins", config.plugins, "--plugins")
    if config.ignore_patterns is not None:
        task.agent.ignore_patterns = config.ignore_patterns
        _record("agent.ignore_patterns", config.ignore_patterns, "--ignore-patterns")

    # Sandbox driver override (CLI > task YAML). Driver value is already
    # Literal-validated upstream via BatchRunConfig; nothing to re-check.
    if config.driver is not None:
        if task.sandbox is None:
            raise ValueError(f"Task '{task.task_id}' has no sandbox config; cannot apply --driver override.")
        task.sandbox.driver = config.driver
        _record("sandbox.driver", config.driver, "--driver")

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

    # Final guard: agent.type must be set after all 5 layers have merged.
    if task.agent.type is None:
        raise ValueError(
            f"Agent 'type' is required but was not set by any layer (default experiment, "
            f"experiment defaults, task, variant, or CLI) for task {task.task_id!r}. "
            f"Set it in the task YAML, the experiment, or via --type."
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

    # Resolve system_prompt_file (may be injected by variant as relative or absolute path)
    if task.agent is not None and task.agent.system_prompt_file is not None:
        resolve_agent_system_prompt(task.agent, exp_dir)

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
        5. CLI / .env overrides

    Also handles tag filtering and unique task ID validation.

    Task YAMLs that fail to load (YAML parse error, Pydantic validation,
    dataset expansion error) are recorded in the returned ``skipped`` list
    and excluded from the resolved set rather than aborting the suite. The
    caller surfaces ``skipped`` in the run summary so the failure is loud
    but recoverable.

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
    resolved: list[ResolvedTask] = []
    skipped: list[SkippedTask] = []

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
            if task.skip:
                reason = f"skip: true (task_id={task.task_id!r})"
                logger.info("Skipping task %s — skip: true in YAML", task.task_id)
                skipped.append(SkippedTask(path=str(task_file), reason=reason))
                continue
            # Dataset fan-out BEFORE variant resolution: one task per row, each
            # treated as an independent task for the 4-layer merge below. This
            # locks the invariant that variants cannot override the dataset.
            expanded_tasks = expand_dataset(task, task_file.parent, max_rows=config.max_rows)
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

                # Apply layer 5 (CLI / .env overrides)
                _apply_cli_overrides(resolved_task, config, lineage)

                # Fan-out: simulation n_trials takes precedence over experiment repeats
                # when simulation is active; otherwise use experiment-level repeats.
                sim = resolved_task.simulation
                n_trials = sim.n_trials if (sim is not None and sim.enabled) else 1
                fan_count = n_trials if n_trials > 1 else effective_repeats
                for rep in range(fan_count):
                    resolved.append(
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
    """
    priority = {"error": 0, "failed": 1, "succeeded": 2}
    return min(statuses, key=lambda s: priority.get(s.category, -1))


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
        per_replicate_scores.setdefault(variant_id, {})[task_id] = [r.result.weighted_score or 0.0 for r in reps]

    task_variants: dict[str, list[VariantResult]] = {}
    for (task_id, variant_id), reps in task_variant_reps.items():
        scores = [r.result.weighted_score or 0.0 for r in reps]
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
            weighted_score=sum(scores) / len(scores),
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
        best = max(variants, key=lambda v: (v.weighted_score, v.variant_id))
        scores = [v.weighted_score for v in variants]
        top_count = sum(1 for v in variants if v.weighted_score == best.weighted_score)
        rep_counts = {v.replicate_count for v in variants}
        task_summaries.append(
            TaskExperimentSummary(
                task_id=task_id,
                variant_results=variants,
                best_variant=best.variant_id,
                is_tie=top_count > 1,
                score_spread=max(scores) - min(scores),
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
                average_score=0.0,
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
            tasks_token_budget_exceeded=sum(1 for v in vr_list if v.final_status == FinalStatus.TOKEN_BUDGET_EXCEEDED),
            tasks_cost_budget_exceeded=sum(1 for v in vr_list if v.final_status == FinalStatus.COST_BUDGET_EXCEEDED),
            average_score=sum(v.weighted_score for v in vr_list) / len(vr_list),
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
