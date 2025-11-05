"""Task definition loading and validation."""

from pathlib import Path

import yaml

from ..models import TaskDefinition, TemplateDirSource


def load_task(task_file: Path) -> TaskDefinition:
    """Load a task definition from a YAML file.

    Args:
        task_file: Path to the task YAML file

    Returns:
        Parsed TaskDefinition

    Raises:
        FileNotFoundError: If task file doesn't exist
        ValueError: If task file is invalid
    """
    if not task_file.exists():
        raise FileNotFoundError(f"Task file not found: {task_file}")

    with open(task_file) as f:
        task_data = yaml.safe_load(f)

    try:
        task = TaskDefinition(**task_data)
        # Resolve relative template paths
        task = resolve_template_paths(task, task_file.parent)
        return task
    except Exception as e:
        raise ValueError(f"Invalid task definition: {e}") from e


def resolve_template_paths(task: TaskDefinition, base_dir: Path) -> TaskDefinition:
    """Resolve relative template paths to absolute paths.

    Mutates TemplateDirSource.path in place for both new API (template_sources)
    and legacy API (template_dir). Other source types don't need path resolution.

    Args:
        task: Task definition with possibly relative paths
        base_dir: Directory containing the task YAML file

    Returns:
        Task with resolved absolute paths (modified in place)
    """
    sandbox_config = task.sandbox

    # Handle new API: iterate template_sources and resolve TemplateDirSource paths
    if sandbox_config.template_sources:
        for source in sandbox_config.template_sources:
            if isinstance(source, TemplateDirSource):
                template_path = Path(source.path)
                if not template_path.is_absolute():
                    source.path = str((base_dir / template_path).resolve())
            # Other source types (RepoSource, StarterFilesSource) don't need resolution

    return task
