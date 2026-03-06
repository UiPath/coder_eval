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

    if task_file.is_dir():
        msg = (
            f"Expected a YAML task file but got a directory: {task_file}\n"
            f"Hint: use a glob pattern like '{task_file}/*.yaml' to select task files."
        )
        raise ValueError(msg)

    with open(task_file) as f:
        task_data = yaml.safe_load(f)

    try:
        task = TaskDefinition(**task_data)
        # Resolve relative template paths
        task = resolve_template_paths(task, task_file.parent)
        return task
    except Exception as e:
        raise ValueError(f"Invalid task definition: {e}") from e


def expand_task_for_agents(task: TaskDefinition) -> list[TaskDefinition]:
    """Expand a multi-agent task into one TaskDefinition per agent.

    For single-agent tasks (task.agents is None) returns [task] unchanged.
    For multi-agent tasks returns N tasks, each with:
    - task.agent set to the specific AgentConfig for that agent
    - task.agents set to None (normalized to single-agent form)
    - task.task_id and all other fields identical to the original

    Args:
        task: Task definition (possibly with agents list)

    Returns:
        List of single-agent TaskDefinitions ready for independent evaluation
    """
    if task.agents is None:
        return [task]

    expanded: list[TaskDefinition] = []
    base = task.model_dump(exclude={"agent", "agents"})
    for agent_config in task.agents:
        new_task = TaskDefinition(**{**base, "agent": agent_config.model_dump()})
        expanded.append(new_task)
    return expanded


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
