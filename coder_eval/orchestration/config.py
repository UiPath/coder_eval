"""Configuration models for orchestration."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class BatchRunConfig(BaseModel):
    """Configuration for batch task execution.

    This configuration object encapsulates all parameters needed to run
    multiple tasks in batch mode with optional parallelism.
    """

    run_dir: Path = Field(description="Directory for this batch run")
    max_parallel: int = Field(default=1, ge=1, description="Max concurrent tasks")
    preserve_sandbox: bool = Field(default=False, description="Preserve sandbox after execution")
    max_iterations: int | None = Field(default=None, description="Override max iterations for all tasks")
    snapshot_mode: str | None = Field(default=None, description="Override snapshot mode for all tasks")
    snapshot_checkpoint_freq: int | None = Field(
        default=None, description="Override checkpoint frequency for hybrid mode"
    )
    include_tags: set[str] | None = Field(default=None, description="Only run tasks matching any of these tags")
    exclude_tags: set[str] | None = Field(default=None, description="Skip tasks matching any of these tags")

    # Agent overrides (CLI > .env > task YAML)
    agent_model: str | None = Field(default=None, description="Override agent model for all tasks")
    permission_mode: str | None = Field(default=None, description="Override permission mode for all tasks")
    max_turns: int | None = Field(default=None, description="Override max turns for all tasks")

    allowed_tools: list[str] | None = Field(default=None, description="Override allowed tools for all tasks")
    plugins: list[Any] | None = Field(
        default=None, description="Override plugins (SdkPluginConfig objects) for all tasks"
    )
    ignore_patterns: list[str] | None = Field(default=None, description="Override ignore patterns for all tasks")

    # Timeout overrides (CLI > task YAML)
    task_timeout: int | None = Field(default=None, ge=30, description="Override task timeout for all tasks")
    turn_timeout: int | None = Field(default=None, ge=10, description="Override turn timeout for all tasks")
