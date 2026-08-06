"""Task definition loading and validation."""

from __future__ import annotations

import json
import os
import random
import re
from pathlib import Path
from typing import Any

import yaml

from ..models import (
    AgentConfig,
    BaseAgentConfig,
    Dataset,
    ExperimentVariant,
    TaskDefinition,
    TemplateDirSource,
    TemplateSource,
)


# Fixed seed for the CLI --sample (max_rows) uniform draw: a smoke sample should
# be reproducible run-to-run, just not first-path-biased like a raw slice.
_SMOKE_SAMPLE_SEED = 0

_ROW_VAR_PATTERN = re.compile(r"\$\{row\.([A-Za-z_][A-Za-z0-9_]*)\}")
_ROW_ID_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def load_task(task_file: Path) -> tuple[TaskDefinition, str]:
    """Load a task definition from a YAML file.

    Args:
        task_file: Path to the task YAML file

    Returns:
        Tuple of (parsed TaskDefinition, raw YAML text)

    Raises:
        FileNotFoundError: If task file doesn't exist
        ValueError: If task file is invalid
    """
    if not task_file.exists():
        raise FileNotFoundError(f"Task file not found: {task_file}")

    if task_file.is_dir():
        msg = (
            f"Expected a YAML task file but got a directory: {task_file}\n"
            f"Hint: use a glob pattern like '{task_file}/*.yaml' to select task files."
        )
        raise ValueError(msg)

    raw_yaml = task_file.read_text(encoding="utf-8")
    task_data = yaml.safe_load(raw_yaml)

    try:
        task = TaskDefinition(**task_data)
        # Resolve relative template paths
        task = resolve_template_paths(task, task_file.parent)
        task = resolve_protected_mock_paths(task, task_file.parent)
        task = resolve_initial_prompt_file(task, task_file.parent)
        task = resolve_system_prompt_files(task, task_file.parent)
        task = resolve_dockerfile_path(task, task_file.parent)
        return task, raw_yaml
    except Exception as e:
        raise ValueError(f"Invalid task definition: {e}") from e


def resolve_template_source_paths(sources: list[TemplateSource], base_dir: Path) -> None:
    """Resolve TemplateDirSource paths to absolute, in place.

    Expands $VAR / ${VAR} environment variables, then normalizes the path:
    relative paths are resolved against ``base_dir``; absolute paths are
    used as-is (but still go through ``Path(...)`` for string normalization).

    Undefined env variables raise ``ValueError`` — a template directory is a
    load-bearing config field and an unresolved variable would otherwise
    surface as a cryptic "Template directory not found" error at sandbox
    setup, far from the actual configuration mistake.

    Scope: only environment variables (``$VAR`` / ``${VAR}``) are expanded
    here. Dataset row substitution (``${row.field}`` in ``expand_dataset``)
    runs over ``initial_prompt`` and ``success_criteria`` only — it does
    NOT touch ``sandbox.template_sources``. The two regexes are disjoint
    (env requires ``[A-Za-z_][A-Za-z0-9_]*``, row-var requires the dot)
    but a ``${row.X}`` left inside a template path will not be substituted
    and will fail at sandbox setup.

    Skips non-TemplateDirSource entries.

    Args:
        sources: List of template sources (TemplateDirSource, RepoSource, etc.)
        base_dir: Base directory for resolving relative paths.

    Raises:
        ValueError: If a ``TemplateDirSource.path`` references an undefined
            environment variable.
    """
    for source in sources:
        if isinstance(source, TemplateDirSource):
            raw = source.path
            undefined: list[str] = []
            for match in _ENV_VAR_PATTERN.finditer(raw):
                var_name = match.group(1) or match.group(2)
                if var_name not in os.environ:
                    undefined.append(var_name)
            if undefined:
                names = ", ".join(f"${v}" for v in undefined)
                msg = (
                    f"Template path {raw!r} references undefined environment variable(s): {names}. "
                    f"Set them before loading the task (e.g. in .env) so the template directory can be resolved."
                )
                raise ValueError(msg)
            expanded = os.path.expandvars(raw)
            template_path = Path(expanded)
            if template_path.is_absolute():
                source.path = str(template_path)
            else:
                source.path = str((base_dir / template_path).resolve())


def resolve_template_paths(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve relative template paths to absolute paths.

    Mutates TemplateDirSource.path in place. Other source types don't need resolution.

    Args:
        task: Task definition with possibly relative paths
        base_dir: Directory containing the task YAML file

    Returns:
        Task with resolved absolute paths (modified in place)
    """
    if task.sandbox.template_sources:
        resolve_template_source_paths(task.sandbox.template_sources, base_dir)

    return task


def resolve_protected_mock_paths(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve and validate protected mock fixtures relative to the task YAML."""

    for mock in task.sandbox.protected_mocks or []:
        fixture = Path(os.path.expandvars(mock.fixture))
        if not fixture.is_absolute():
            fixture = (base_dir / fixture).resolve()
        if not fixture.is_file():
            raise FileNotFoundError(f"Protected mock fixture not found: {fixture}")
        mock.fixture = str(fixture)
    return task


def resolve_dockerfile_path(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve ``sandbox.docker.dockerfile_path`` to an absolute path, in place.

    When set, ``dockerfile_path`` is interpreted relative to the task YAML's
    directory (``base_dir``), with ``$VAR`` / ``${VAR}`` environment variables
    expanded first (mirroring :func:`resolve_template_source_paths`). The
    resolved file must exist -- a missing Dockerfile is a configuration error
    surfaced at load time rather than as an opaque ``docker build`` failure.

    No-op when ``dockerfile_path`` is unset. Resolution runs regardless of the
    configured ``driver`` so the absolute path stays stable even if a later
    layer flips the driver to ``docker``.

    Args:
        task: Task definition possibly carrying a relative ``dockerfile_path``.
        base_dir: Directory containing the task YAML file.

    Returns:
        The same task with an absolute ``dockerfile_path`` (modified in place).

    Raises:
        FileNotFoundError: If the resolved Dockerfile does not exist.
    """
    docker_cfg = task.sandbox.docker
    raw = docker_cfg.dockerfile_path
    if raw is None:
        return task
    dockerfile = Path(os.path.expandvars(raw))
    if not dockerfile.is_absolute():
        dockerfile = (base_dir / dockerfile).resolve()
    if not dockerfile.is_file():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")
    docker_cfg.dockerfile_path = str(dockerfile)
    return task


def resolve_initial_prompt_file(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve initial_prompt_file to inline initial_prompt.

    In simulation mode, both ``initial_prompt`` and ``initial_prompt_file`` may
    be absent — the simulator generates the opening user utterance itself.
    """
    if task.initial_prompt_file is not None:
        prompt_path = Path(task.initial_prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = (base_dir / prompt_path).resolve()
        if not prompt_path.exists():
            raise FileNotFoundError(f"initial_prompt_file not found: {prompt_path}")
        content = prompt_path.read_text(encoding="utf-8").strip()
        # Clear file field BEFORE setting inline to avoid mutual-exclusivity validator
        task.initial_prompt_file = None
        task.initial_prompt = content
    if task.initial_prompt is None:
        in_simulation = task.simulation is not None and task.simulation.enabled
        if not in_simulation and not task.is_none_agent:
            raise ValueError(
                "Either 'initial_prompt' or 'initial_prompt_file' must be set "
                + "(unless 'simulation.enabled' is true, in which case the simulator generates the opener, "
                + "or 'agent.type' is 'none', in which case no agent runs)"
            )
    return task


def resolve_variant_initial_prompt_file(variant: ExperimentVariant, base_dir: Path) -> None:
    """Resolve initial_prompt_file on a variant to inline initial_prompt. Mutates in place.

    Args:
        variant: The experiment variant (may have initial_prompt_file set).
        base_dir: Directory to resolve relative paths against (experiment YAML dir).

    Raises:
        FileNotFoundError: If the file doesn't exist.
    """
    if variant.initial_prompt_file is None:
        return
    prompt_path = Path(variant.initial_prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = (base_dir / prompt_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(f"variant initial_prompt_file not found: {prompt_path}")
    content = prompt_path.read_text(encoding="utf-8").strip()
    # Clear file field BEFORE setting inline to avoid mutual-exclusivity validator
    variant.initial_prompt_file = None
    variant.initial_prompt = content


def resolve_agent_system_prompt(agent_config: AgentConfig | BaseAgentConfig | None, base_dir: Path) -> None:
    """Resolve system_prompt_file to inline system_prompt. Mutates in place."""
    if agent_config is None:
        return
    if agent_config.system_prompt_file is not None:
        prompt_path = Path(agent_config.system_prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = (base_dir / prompt_path).resolve()
        if not prompt_path.exists():
            raise FileNotFoundError(f"system_prompt_file not found: {prompt_path}")
        content = prompt_path.read_text(encoding="utf-8").strip()
        # Clear file field BEFORE setting inline to avoid mutual-exclusivity validator
        agent_config.system_prompt_file = None
        agent_config.system_prompt = content


def resolve_system_prompt_files(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve system_prompt_file on agent config."""
    if task.agent is not None:
        resolve_agent_system_prompt(task.agent, base_dir)
    return task


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts."""
    if not path.exists():
        raise FileNotFoundError(f"Dataset file not found: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Dataset {path}: invalid JSON on line {line_num}: {e}") from e
            if not isinstance(row, dict):
                raise ValueError(f"Dataset {path}: row on line {line_num} is not a JSON object: {row!r}")
            rows.append(row)
    return rows


def _resolve_path(p: str, task_file_dir: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (task_file_dir / path).resolve()


def _load_dataset_rows(dataset: Dataset, task_file_dir: Path) -> list[dict[str, Any]]:
    """Load dataset rows from inline list or one or more JSONL files."""
    if dataset.rows is not None:
        return [dict(r) for r in dataset.rows]

    assert dataset.paths is not None  # guaranteed by Dataset.check_source
    rows: list[dict[str, Any]] = []
    for p in dataset.paths:
        rows.extend(_load_jsonl(_resolve_path(p, task_file_dir)))
    return rows


def _stratified_sample(
    rows: list[dict[str, Any]],
    field: str,
    n: int,
    seed: int | None,
) -> list[dict[str, Any]]:
    """Randomly keep up to ``n`` rows per stratum, keyed on ``str(row[field])``.

    Strata with <= n rows are taken whole. Output preserves first-seen stratum
    order; within a sampled stratum, rows are in their drawn (random) order.
    Rows missing ``field`` fall into the "" stratum (this is where the activation
    dataset's shared negatives — ``expected_skill: ""`` — collect). ``seed=None``
    uses a fresh nondeterministic RNG, so the draw differs every run.
    """
    rng = random.Random(seed)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(field, "")), []).append(row)
    out: list[dict[str, Any]] = []
    for grp in groups.values():
        out.extend(grp if len(grp) <= n else rng.sample(grp, n))
    return out


def _substitute_row_in_str(s: str, row: dict[str, Any]) -> str:
    """Replace ${row.<field>} occurrences in s with scalar values from row."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in row:
            raise KeyError(f"${{row.{key}}}: key not found (available: {sorted(row.keys())})")
        value = row[key]
        if isinstance(value, dict | list):
            raise TypeError(
                f"${{row.{key}}}: value must be a scalar (str/int/float/bool/None), got {type(value).__name__}"
            )
        return "" if value is None else str(value)

    return _ROW_VAR_PATTERN.sub(replace, s)


def _substitute_row_in_tree(obj: Any, row: dict[str, Any]) -> Any:
    """Walk a nested dict/list structure and substitute ${row.X} in every string leaf."""
    if isinstance(obj, str):
        return _substitute_row_in_str(obj, row)
    if isinstance(obj, list):
        return [_substitute_row_in_tree(x, row) for x in obj]
    if isinstance(obj, dict):
        return {k: _substitute_row_in_tree(v, row) for k, v in obj.items()}
    return obj


def expand_dataset(
    task: TaskDefinition,
    task_file_dir: Path,
    max_rows: int | None = None,
    sample_per_stratum: int | None = None,
) -> list[TaskDefinition]:
    """Fan out a task with ``dataset:`` into one TaskDefinition per row.

    Tasks without ``dataset:`` pass through unchanged as ``[task]``.

    Each expanded task:
      - has task_id rewritten to ``"<original_task_id>/<row_id>"``
      - has ``dataset`` cleared (prevents re-expansion downstream)
      - has ``${row.<field>}`` substituted in ``initial_prompt`` and in all
        string leaves of ``success_criteria`` entries

    Row ids are validated against a safe pattern so they're filesystem-safe
    when used as directory names under the run_dir.

    Args:
        task: Task that may carry a dataset.
        task_file_dir: Directory of the source task YAML (for resolving dataset.paths).
        max_rows: Optional CLI cap on rows used (for cheap smoke runs). A
            fixed-seed uniform-random N-row sample over the whole dataset
            (reproducible, but unbiased across ``dataset.paths`` — unlike a raw
            slice). When provided, overrides both ``sample_per_stratum`` args.
            Absent it, ``sample_per_stratum`` (stratified random) applies.
        sample_per_stratum: Optional CLI override (``--sample-per-stratum``) for
            ``dataset.sample_per_stratum`` — keep up to N rows per stratum
            (stratum = ``dataset.stratify_field``, default ``expected_skill``).
            Lets a runner cap a stratified dataset without editing the task YAML
            (the nightly activation suite uses this). Ignored when ``max_rows``
            is set. When None, falls back to ``dataset.sample_per_stratum``.

    Returns:
        Expanded list of TaskDefinitions. Length is 1 when dataset is None.

    Raises:
        ValueError: Empty dataset, duplicate row ids, missing id_field, or
            malformed row id.
        FileNotFoundError: Dataset path does not exist.
    """
    if task.dataset is None:
        return [task]

    rows = _load_dataset_rows(task.dataset, task_file_dir)
    if not rows:
        raise ValueError(f"Dataset for task '{task.task_id}' is empty")

    # Row selection precedence:
    #   1. CLI --sample (max_rows): flat uniform-random N over the whole dataset.
    #      Fixed seed => reproducible across runs, but (unlike a first-N slice)
    #      unbiased across the concatenated dataset.paths.
    #   2. sample_per_stratum: stratified random N-per-stratum. CLI
    #      --sample-per-stratum (the arg) overrides dataset.sample_per_stratum
    #      (the YAML), so a runner can cap a dataset without editing its task.
    ds = task.dataset
    n_per_stratum = sample_per_stratum if sample_per_stratum is not None else ds.sample_per_stratum
    # Stratified sampling is seeded only by dataset.sample_seed. When that is None the sample is
    # deliberately nondeterministic — re-drawn every run — regardless of whether the CLI
    # --sample-per-stratum flag or the YAML supplied the count (see Dataset.sample_seed). The
    # nightly activation suite relies on this to broaden coverage across runs.
    stratum_seed = ds.sample_seed
    if max_rows is not None and max_rows < len(rows):
        rows = random.Random(_SMOKE_SAMPLE_SEED).sample(rows, max_rows)
    elif max_rows is None and n_per_stratum is not None:
        rows = _stratified_sample(rows, ds.stratify_field, n_per_stratum, stratum_seed)

    id_field = task.dataset.id_field
    seen_ids: set[str] = set()
    expanded: list[TaskDefinition] = []

    for i, row in enumerate(rows):
        if id_field not in row:
            raise ValueError(f"Dataset row {i} for task '{task.task_id}' missing id_field '{id_field}': {row}")
        row_id = str(row[id_field])
        if not _ROW_ID_PATTERN.match(row_id):
            raise ValueError(
                f"Dataset row id {row_id!r} must match {_ROW_ID_PATTERN.pattern}"
                + " (letters, digits, underscore, hyphen, dot)"
            )
        if row_id in seen_ids:
            raise ValueError(f"Duplicate dataset row id for task '{task.task_id}': {row_id!r}")
        seen_ids.add(row_id)

        data = task.model_dump(exclude_unset=True)
        if isinstance(data.get("initial_prompt"), str):
            data["initial_prompt"] = _substitute_row_in_str(data["initial_prompt"], row)
        if isinstance(data.get("success_criteria"), list):
            data["success_criteria"] = [_substitute_row_in_tree(c, row) for c in data["success_criteria"]]
        data["suite_id"] = task.task_id
        data["row_id"] = row_id
        data["task_id"] = f"{task.task_id}/{row_id}"
        data["dataset"] = None
        expanded.append(TaskDefinition(**data))

    return expanded
