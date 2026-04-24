"""Task definition loading and validation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from ..models import AgentConfig, Dataset, ExperimentVariant, TaskDefinition, TemplateDirSource, TemplateSource


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
        task = resolve_initial_prompt_file(task, task_file.parent)
        task = resolve_system_prompt_files(task, task_file.parent)
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
        if not in_simulation:
            raise ValueError(
                "Either 'initial_prompt' or 'initial_prompt_file' must be set "
                + "(unless 'simulation.enabled' is true, in which case the simulator generates the opener)"
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


def resolve_agent_system_prompt(agent_config: AgentConfig | None, base_dir: Path) -> None:
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


def _load_dataset_rows(dataset: Dataset, task_file_dir: Path) -> list[dict[str, Any]]:
    """Load dataset rows from inline list or a JSONL file."""
    if dataset.rows is not None:
        return [dict(r) for r in dataset.rows]

    assert dataset.path is not None  # guaranteed by Dataset.check_source
    p = Path(dataset.path)
    if not p.is_absolute():
        p = (task_file_dir / p).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Dataset file not found: {p}")

    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for line_num, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Dataset {p}: invalid JSON on line {line_num}: {e}") from e
            if not isinstance(row, dict):
                raise ValueError(f"Dataset {p}: row on line {line_num} is not a JSON object: {row!r}")
            rows.append(row)
    return rows


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
        task_file_dir: Directory of the source task YAML (for resolving dataset.path).
        max_rows: Optional CLI cap on rows used (for cheap smoke runs). First
            N rows. When provided, overrides ``dataset.sample`` from the task YAML.

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

    # Precedence: CLI --sample (max_rows) wins over task-level dataset.sample.
    effective_cap = max_rows if max_rows is not None else task.dataset.sample
    if effective_cap is not None:
        rows = rows[:effective_cap]

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
