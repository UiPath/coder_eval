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

    Under ``driver: tempdir`` only ``timeout`` is actively enforced (via
    ``subprocess.run(timeout=...)``); other fields are accepted but not
    enforced -- the agent can consume arbitrary host memory/CPU/PIDs/disk.

    Under ``driver: docker`` ``max_memory_mb``, ``max_cpus``, and
    ``max_pids`` translate to ``--memory``, ``--cpus``, and ``--pids-limit``
    respectively. ``max_disk_mb`` remains reserved (no portable docker knob).
    """

    model_config = ConfigDict(extra="forbid")

    timeout: int = Field(default=300, description="Maximum execution time in seconds")
    max_memory_mb: int | None = Field(
        default=None,
        description="Maximum memory in MB. Mapped to `docker run --memory` under driver:docker; reserved otherwise.",
    )
    max_cpus: float | None = Field(
        default=None,
        gt=0,
        description="Max CPU shares (fractional). Mapped to `docker --cpus` under driver:docker; reserved otherwise.",
    )
    max_pids: int | None = Field(
        default=None,
        gt=0,
        description="Max PID count. Mapped to `docker run --pids-limit` under driver:docker; reserved otherwise.",
    )
    max_disk_mb: int | None = Field(
        default=None,
        description="Maximum disk usage in MB (reserved -- no portable docker knob).",
    )


class SnapshotConfig(BaseModel):
    """Configuration for iteration snapshots.

    Note: No 'enabled' flag - use mode=DISABLED to disable snapshots.
    This avoids redundant state (e.g., enabled=False, mode=FULL).
    """

    model_config = ConfigDict(extra="forbid")

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

    model_config = ConfigDict(extra="forbid")

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

    model_config = ConfigDict(extra="forbid")

    env_packages: list[str] = Field(default_factory=list, description="Packages to install")


class NodeEnvConfig(BaseModel):
    """Configuration for Node.js environment in the sandbox."""

    model_config = ConfigDict(extra="forbid")

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


class DockerDriverConfig(BaseModel):
    """Per-task overrides for ``driver: docker``.

    Only consulted when ``SandboxConfig.driver == "docker"``. The
    ``env_passthrough`` allowlist is the **only** source of host env vars
    visible inside the container -- nothing else leaks from ``os.environ``.
    Default list covers the credentials the in-container Orchestrator needs
    to stand up its own LLM Gateway proxy.
    """

    model_config = ConfigDict(extra="forbid")

    image: str | None = Field(
        default=None,
        description="Container image. Defaults to coder-eval-agent:<pkg-version> (built via `make docker-image`).",
    )
    network: Literal["bridge", "none"] = Field(
        default="bridge",
        description="Container network. 'bridge' for tasks needing LLM/pkg access; 'none' for fully sealed runs.",
    )
    env_passthrough: list[str] = Field(
        default_factory=lambda: [
            "ANTHROPIC_API_KEY",
            # Selects routing: direct Anthropic vs. LLM Gateway proxy vs. Bedrock.
            "API_BACKEND",
            # LLM Gateway proxy credentials -- forwarded so the container's
            # Orchestrator can stand up its own in-container proxy.
            "LLMGW_PROXY_ENABLED",
            "LLMGW_URL",
            "LLMGW_CLIENT_ID",
            "LLMGW_CLIENT_SECRET",
            "LLMGW_SEMANTIC_ORG_ID",
            "LLMGW_SEMANTIC_TENANT_ID",
            "LLMGW_SEMANTIC_USER_ID",
            "LLMGW_REQUESTING_PRODUCT",
            "LLMGW_REQUESTING_FEATURE",
            "LLMGW_TIMEOUT_SECONDS",
            "UIPATH_LLM_BACKEND",
            "UIPATH_ACCESS_TOKEN",
            "UIPATH_URL",
            "UIPATH_TENANT_ID",
            "UIPATH_ORGANIZATION_ID",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_REGION",
            # Claude Code SDK Bedrock toggle + optional model override; required
            # alongside AWS_BEARER_TOKEN_BEDROCK to route the in-container SDK
            # through Bedrock instead of falling back to ~/.claude OAuth.
            "CLAUDE_CODE_USE_BEDROCK",
            "ANTHROPIC_MODEL",
            # User HOME used to keep ~/.claude resolution symmetric with the host.
            # See docs/DOCKER_ISOLATION.md "HOME is forwarded by default" for the
            # contract. tl;dr: Path.home() inside the container returns the
            # host's HOME (the dir is auto-created by the ~/.claude bind mount);
            # writes outside ~/.claude land in the container's ephemeral rootfs.
            # Remove this entry if you don't want host HOME leakage.
            "HOME",
        ],
        description=(
            "Explicit allowlist of host env vars to forward into the container. "
            "This is the ONLY source of forwarded env -- nothing else from os.environ leaks in. "
            "Extend per-task to expose extra credentials/config. "
            "Note: HOME forwarding is intentional (keeps ~/.claude path symmetric with host); "
            "see docs/DOCKER_ISOLATION.md for the contract."
        ),
    )
    extra_mounts: list[str] = Field(
        default_factory=list,
        description="Extra `-v src:dst[:ro]` mount specs forwarded to `docker run`. Validated for basic syntax.",
    )


class SandboxConfig(BaseModel):
    """Configuration for the sandboxed execution environment.

    ``driver: tempdir`` (default) runs the agent in a plain temp directory on
    the host with no container isolation -- agent commands share the host's
    network, process table, and filesystem outside the temp dir.
    ``driver: docker`` runs each task inside its own container; see
    :class:`DockerDriverConfig` for knobs. ``ResourceLimits.timeout`` is the
    only limit enforced in tempdir mode; under docker, ``max_memory_mb`` also
    maps to ``--memory`` when set.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    driver: Literal["tempdir", "docker"] = Field(
        default="tempdir",
        description="Sandbox driver: 'tempdir' = in-process on host; 'docker' = one container per task.",
    )
    docker: DockerDriverConfig = Field(
        default_factory=DockerDriverConfig,
        description="Docker-driver overrides; ignored unless driver == 'docker'.",
    )
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
