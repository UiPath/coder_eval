"""Autogen — generate evaluation tasks from Claude Code plugin directories."""

from coder_eval.tools.autogen.generator import generate_experiment, generate_tasks, task_to_yaml
from coder_eval.tools.autogen.validator import validate_tasks


__all__ = ["generate_experiment", "generate_tasks", "task_to_yaml", "validate_tasks"]
