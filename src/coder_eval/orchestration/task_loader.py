"""Task definition loading and validation."""

from pathlib import Path

import yaml

from ..models import AgentConfig, ExperimentVariant, TaskDefinition, TemplateDirSource, TemplateSource


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
    """Resolve relative TemplateDirSource paths to absolute, in place.

    Skips already-absolute paths and non-TemplateDirSource entries.

    Args:
        sources: List of template sources (TemplateDirSource, RepoSource, etc.)
        base_dir: Base directory for resolving relative paths.
    """
    for source in sources:
        if isinstance(source, TemplateDirSource):
            template_path = Path(source.path)
            if not template_path.is_absolute():
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
    """Resolve initial_prompt_file to inline initial_prompt."""
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
        raise ValueError("Either 'initial_prompt' or 'initial_prompt_file' must be set")
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
