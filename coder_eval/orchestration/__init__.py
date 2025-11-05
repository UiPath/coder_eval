"""Orchestration components for task evaluation lifecycle.

This package coordinates the complete evaluation flow from task loading
through sandbox setup, agent interaction, success checking, and cleanup.

Main components:
- config: Configuration models for batch execution
- task_loader: YAML task definition loading and validation

NO re-exports - use explicit imports:
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestration.task_loader import load_task, resolve_template_paths
"""
