"""Sandbox configuration and snapshot models."""

from __future__ import annotations

import warnings
from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from coder_eval.models.enums import SnapshotMode
from coder_eval.models.templates import TemplateSource


class ResourceLimits(BaseModel):
    """Resource limits for sandbox execution.

    Note: The sandbox uses a temporary directory on the host filesystem with no
    containerisation or cgroup enforcement.  Only ``timeout`` is actively enforced
    (via ``subprocess.run(timeout=...)``).  ``max_memory_mb`` and ``max_disk_mb``
    are reserved for future use and are **not** enforced today -- agent commands
    can consume arbitrary host memory and disk.
    """

    timeout: int = Field(default=300, description="Maximum execution time in seconds")
    max_memory_mb: int | None = Field(
        default=None,
        description="Maximum memory in MB (reserved -- not enforced today)",
    )
    max_disk_mb: int | None = Field(
        default=None,
        description="Maximum disk usage in MB (reserved -- not enforced today)",
    )


class SnapshotConfig(BaseModel):
    """Configuration for iteration snapshots.

    Note: No 'enabled' flag - use mode=DISABLED to disable snapshots.
    This avoids redundant state (e.g., enabled=False, mode=FULL).
    """

    mode: SnapshotMode = Field(
        default=SnapshotMode.DISABLED, description="Snapshot mode (default: disabled for backward compatibility)"
    )
    checkpoint_frequency: int = Field(
        default=5, ge=1, description="Full snapshot every N iterations (hybrid mode only)"
    )
    ignore_patterns: list[str] = Field(
        default_factory=list, description="Additional file patterns to exclude (beyond sandbox defaults like .venv)"
    )


class SnapshotManifest(BaseModel):
    """Metadata for a single snapshot.

    Stored as manifest.json in each snapshot directory.
    """

    created_at: datetime = Field(description="When this snapshot was created")
    iteration: int = Field(description="Iteration number (0-indexed)")
    mode: SnapshotMode = Field(description="Snapshot mode used (full/incremental)")
    size_bytes: int = Field(description="Total size of snapshot in bytes")
    file_count: int = Field(description="Number of files in snapshot")
    changed_files: list[str] = Field(
        default_factory=list,
        description="List of changed file paths (for incremental snapshots, includes DELETED: markers)",
    )
    base_iteration: int | None = Field(
        default=None, description="For incremental: which iteration to apply changes to (typically iteration - 1)"
    )


class PythonEnvConfig(BaseModel):
    """Configuration for the Python virtual environment in the sandbox."""

    env_packages: list[str] = Field(default_factory=list, description="Packages to install")


class NodeEnvConfig(BaseModel):
    """Configuration for Node.js environment in the sandbox."""

    env_packages: list[str] = Field(
        default_factory=list, description="npm packages to install (e.g., '@uipath/cli@0.1.5')"
    )


def validate_template_sources_list(sources: list[TemplateSource]) -> None:
    """Validate a list of template sources for correctness.

    Checks:
      - At most one RepoSource
      - RepoSource must be first (git clone requires empty directory)
      - Warns if more than 10 sources

    Args:
        sources: List of template sources to validate.

    Raises:
        ValueError: If validation fails.
    """
    from coder_eval.models.templates import RepoSource

    repo_sources = [src for src in sources if isinstance(src, RepoSource)]
    if len(repo_sources) > 1:
        raise ValueError("Only one RepoSource is allowed in template_sources.")

    if len(repo_sources) == 1 and not isinstance(sources[0], RepoSource):
        raise ValueError(
            "RepoSource must be the first element in template_sources (git clone requires an empty directory)."
        )

    if len(sources) > 10:
        warnings.warn(
            f"Many template sources ({len(sources)}) - this may be a misconfiguration",
            UserWarning,
            stacklevel=2,
        )


class SandboxConfig(BaseModel):
    """Configuration for the sandboxed execution environment.

    The only supported driver is ``tempdir``, which creates a plain temporary
    directory on the host.  There is no container or VM isolation -- commands
    executed inside the sandbox share the host's network, process table, and
    filesystem (outside the temp directory).  See :class:`ResourceLimits` for
    details on which limits are enforced.
    """

    model_config = ConfigDict(populate_by_name=True)

    driver: Literal["tempdir"] = Field(default="tempdir", description="Sandbox driver type (only tempdir supported)")
    python: PythonEnvConfig | None = Field(
        default_factory=PythonEnvConfig,
        description="Python environment config; set to null in YAML (or None in Python) to skip venv creation",
    )
    node: NodeEnvConfig | None = Field(
        default=None,
        description="Node.js environment config; set to enable npm package installation in the sandbox",
    )
    limits: ResourceLimits = Field(default_factory=ResourceLimits, description="Resource limits for execution")

    # Multi-source template support
    template_sources: list[TemplateSource] | None = Field(
        default=None, description="Sequential list of template sources to apply"
    )

    mock_path_dirs: list[str] | None = Field(
        default=None,
        description=(
            "Sandbox-relative directories whose contents act as PATH-prepended mock "
            "binaries for the agent subprocess. After templates are applied, the "
            "sandbox marks plain files in each listed directory executable (+x) and "
            "returns absolute paths to the orchestrator, which forwards them to the "
            "agent. Missing entries are skipped silently. Example: "
            '["mocks"] with a `mocks/uip` script placed via `template_sources`.'
        ),
    )

    # Snapshot configuration
    snapshots: SnapshotConfig = Field(default_factory=SnapshotConfig, description="Iteration snapshot configuration")

    # Customizable ignore patterns
    ignore_patterns: list[str] = Field(
        default_factory=list,
        description="Additional patterns to ignore during template setup and snapshots (beyond defaults)",
        validation_alias=AliasChoices("ignore_patterns", "additional_ignore_patterns"),
    )

    @model_validator(mode="after")
    def validate_template_sources(self) -> SandboxConfig:
        """Validate template sources configuration."""
        if self.template_sources:
            validate_template_sources_list(self.template_sources)
        return self
