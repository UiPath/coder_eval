"""Sandbox configuration models."""

from __future__ import annotations

import warnings
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from coder_eval.models.container_paths import CONTAINER_WORK_DIR, RESERVED_CONTAINER_DIRS
from coder_eval.models.merge_strategy import MergeField
from coder_eval.models.templates import TemplateSource
from coder_eval.resources import normalize_ignore_pattern_entry
from coder_eval.utils import get_default_docker_image_tag


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


class PythonEnvConfig(BaseModel):
    """Configuration for the Python virtual environment in the sandbox."""

    model_config = ConfigDict(extra="forbid")

    env_packages: list[str] = MergeField(strategy="replace", default_factory=list, description="Packages to install")


class NodeEnvConfig(BaseModel):
    """Configuration for Node.js environment in the sandbox."""

    model_config = ConfigDict(extra="forbid")

    env_packages: list[str] = MergeField(
        strategy="replace", default_factory=list, description="npm packages to install (e.g., '@uipath/cli@0.1.5')"
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


class DockerBuildConfig(BaseModel):
    """``docker build`` customization for a ``dockerfile_path`` task image.

    Only consulted when ``DockerDriverConfig.dockerfile_path`` is set. BuildKit
    (required for ``secrets``) is inherited from the invoking environment by
    default; set ``buildkit`` to force it on or off.

    SECURITY: these fields are task-author-controlled and flow straight into the
    ``docker build`` argv. Task YAMLs are trusted infra; treat ``extra_args``
    like any other shell-adjacent config.
    """

    model_config = ConfigDict(extra="forbid")

    buildkit: bool | None = Field(
        default=None,
        description=(
            "Controls the DOCKER_BUILDKIT env var for `docker build`. None (default) inherits the "
            "invoker's environment -- export DOCKER_BUILDKIT before running coder-eval to control it. "
            "true forces it on; false forces it off. BuildKit is REQUIRED for `secrets`: set this true "
            "(or export DOCKER_BUILDKIT=1) when using build secrets, else the build fails."
        ),
    )

    args: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Build-time variables -> `--build-arg KEY=VALUE`. Values are environment-expanded "
            "($VAR / ${VAR}) against the host env, so you can forward host values "
            "(e.g. {VERSION: '${BUILD_VERSION}'}). For credentials, prefer `secrets` -- build-args "
            "are recorded in the image history."
        ),
    )
    secrets: list[str] = MergeField(
        strategy="replace",
        default_factory=list,
        description=(
            "BuildKit secret specs -> `--secret <spec>`, e.g. 'id=mytoken,env=MY_TOKEN' (forward a "
            "host env var) or 'id=mytoken,src=/path/to/file'. Exposed only to RUN steps that mount "
            "them, never baked into image layers. Reference in the Dockerfile via "
            "`RUN --mount=type=secret,id=mytoken ...`."
        ),
    )
    extra_args: list[str] = MergeField(
        strategy="replace",
        default_factory=list,
        description=(
            "Additional raw `docker build` flags inserted before the build context, e.g. "
            "['--target', 'runtime'] or ['--network', 'host']. Escape hatch for options without a "
            "dedicated field."
        ),
    )


class DockerDriverConfig(BaseModel):
    """Per-task overrides for ``driver: docker``.

    Only consulted when ``SandboxConfig.driver == "docker"``. Controls which
    host environment variables are forwarded to the container via an explicit
    allowlist. Use ``env_passthrough_extra`` to add vars to the default allowlist
    without replacing it.

    Default allowlist covers credentials the in-container Orchestrator needs
    (Anthropic API keys, Bedrock credentials, etc.).
    """

    model_config = ConfigDict(extra="forbid")

    image: str = Field(
        default_factory=get_default_docker_image_tag,
        description=(
            "Container image (default: coder-eval-agent:<pkg-version>). Override to use a custom image "
            "(e.g., for BYOD: Bring Your Own Docker)."
        ),
    )
    dockerfile_path: str | None = Field(
        default=None,
        description=(
            "Optional path to a Dockerfile to build a custom image for this task, relative to the "
            "task YAML directory (resolved to an absolute path at load time). When set it overrides "
            "`image`: the image is built with the Dockerfile's parent directory as the build context, "
            "so relative COPY paths resolve. Example: `./environment/Dockerfile`."
        ),
    )
    build: DockerBuildConfig = Field(
        default_factory=DockerBuildConfig,
        description=(
            "`docker build` customization (build args / BuildKit secrets / extra flags) applied when "
            "`dockerfile_path` is set. Ignored when building is not in play."
        ),
    )
    network: Literal["bridge", "none"] = Field(
        default="bridge",
        description="Container network. 'bridge' for tasks needing LLM/pkg access; 'none' for fully sealed runs.",
    )
    working_dir: str | None = Field(
        default=None,
        description=(
            "Run the agent at this absolute path inside the container instead of the default "
            "run_dir/artifacts workspace, and copy it out to run_dir/artifacts/<task> afterward. "
            "Use the exact WORKDIR the task's Dockerfile/inputs assume (e.g. '/root', '/app'). "
            "The sentinel 'auto' detects the image's WORKDIR at run time (falls back to /root). "
            "None (default) keeps the standard behavior. Docker driver only; ignored under driver:tempdir."
        ),
    )
    env_passthrough: list[str] = MergeField(
        strategy="replace",
        default_factory=lambda: [
            "ANTHROPIC_API_KEY",
            # Selects routing: direct Anthropic vs. Bedrock.
            "API_BACKEND",
            "UIPATH_LLM_BACKEND",
            "UIPATH_ACCESS_TOKEN",
            "UIPATH_URL",
            "UIPATH_TENANT_ID",
            "UIPATH_ORGANIZATION_ID",
            # Disable uip CLI version-sync: the shared ~/.uipath mount lets one
            # task's post-login re-pin downgrade later tasks' CLI/tools.
            "UIPATH_CLI_DISABLE_VERSION_SYNC",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_REGION",
            "BEDROCK_MODEL",
            # Claude Code SDK Bedrock toggle + optional model override; required
            # alongside AWS_BEARER_TOKEN_BEDROCK to route the in-container SDK
            # through Bedrock instead of falling back to ~/.claude OAuth.
            "CLAUDE_CODE_USE_BEDROCK",
            "ANTHROPIC_MODEL",
            # Codex agent auth/routing — without these the in-container codex
            # binary falls back to a ChatGPT login that doesn't exist in the
            # container and auth fails. CODEX_API_KEY drives login_api_key;
            # CODEX_BASE_URL routes to a custom endpoint (e.g. gateway);
            # CODEX_MODEL selects the model when agent.model is unset.
            "CODEX_API_KEY",
            "CODEX_BASE_URL",
            "CODEX_MODEL",
            # Antigravity agent auth/routing — the google-antigravity local harness
            # authenticates against the Gemini API with GEMINI_API_KEY; without it
            # the in-container harness has no credential and fails. ANTIGRAVITY_MODEL
            # selects the Gemini model when agent.model is unset.
            "GEMINI_API_KEY",
            "ANTIGRAVITY_MODEL",
            # User HOME used to keep ~/.claude resolution symmetric with the host.
            # See docs/DOCKER_ISOLATION.md "HOME is forwarded by default" for the
            # contract. tl;dr: Path.home() inside the container returns the
            # host's HOME (the dir is auto-created by the ~/.claude bind mount);
            # writes outside ~/.claude land in the container's ephemeral rootfs.
            # Remove this entry if you don't want host HOME leakage.
            "HOME",
        ],
        description=(
            "Allowlist of host environment variables to forward to the container. Only vars that exist in the host "
            "environment are forwarded. To extend the default with custom vars (e.g., MY_API_TOKEN), use "
            "env_passthrough_extra instead of replacing this field. Note: HOME is intentional in default "
            "(keeps ~/.claude path symmetric with host); see docs/DOCKER_ISOLATION.md for details."
        ),
    )
    env_passthrough_extra: list[str] = MergeField(
        strategy="append",
        default_factory=list,
        description=(
            "Additional environment variables to forward beyond the default allowlist. Merged with env_passthrough "
            "at runtime. Use this to add one or two custom vars (e.g., MY_API_TOKEN) without replacing the defaults. "
            "Example: env_passthrough_extra: ['MY_CUSTOM_TOKEN', 'DEBUG_MODE']. Appended across config layers."
        ),
    )
    extra_mounts: list[str] = MergeField(
        strategy="replace",
        default_factory=list,
        description="Extra `-v src:dst[:ro]` mount specs forwarded to `docker run`. Validated for basic syntax.",
    )

    @field_validator("working_dir")
    @classmethod
    def _validate_working_dir(cls, v: str | None) -> str | None:
        """Reject a WORKDIR that collides with a framework-reserved container path.

        Reserved set is the single source of truth in ``models.container_paths``;
        ``docker_runner`` re-asserts against the same set host-side.
        """
        if v is None or v == "auto":
            return v
        if not v.startswith("/"):
            raise ValueError(f"working_dir {v!r} must be an absolute path or the sentinel 'auto'.")
        norm = v.rstrip("/") or "/"
        if norm in RESERVED_CONTAINER_DIRS or norm.startswith(CONTAINER_WORK_DIR + "/"):
            raise ValueError(
                f"working_dir {v!r} collides with a framework-reserved container path "
                + "(/, /work, /work/*). Choose the task image's own WORKDIR (e.g. /root, /app)."
            )
        return v


class SandboxConfig(BaseModel):
    """Configuration for the sandboxed execution environment.

    ``driver: tempdir`` (default) runs the agent in a plain temp directory on
    the host with no container isolation -- agent commands share the host's
    network, process table, and filesystem outside the temp dir.
    ``driver: docker`` runs each task inside its own container; see
    :class:`DockerDriverConfig` for knobs. ``ResourceLimits.timeout`` is the
    only limit enforced in tempdir mode; under docker, ``max_memory_mb`` /
    ``max_cpus`` / ``max_pids`` also map to ``--memory`` / ``--cpus`` /
    ``--pids-limit`` when set.
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
    template_sources: list[TemplateSource] | None = MergeField(
        strategy="append",
        default=None,
        description="Sequential list of template sources to apply. Appended across config layers.",
    )

    mock_path_dirs: list[str] | None = MergeField(
        strategy="replace",
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

    # Customizable ignore patterns
    ignore_patterns: list[str] = MergeField(
        strategy="replace",
        default_factory=list,
        description=(
            "Pattern overrides applied during template setup. "
            "Plain entries add to the defaults; entries prefixed with '!' "
            "remove a default (gitignore-style negation). Use e.g. ['!dist', "
            "'!node_modules'] to let vendored JS build outputs survive the "
            "template copy."
        ),
        validation_alias=AliasChoices("ignore_patterns", "additional_ignore_patterns"),
    )

    @field_validator("ignore_patterns")
    @classmethod
    def _validate_ignore_patterns(cls, values: list[str]) -> list[str]:
        return [normalize_ignore_pattern_entry(v) for v in values]

    @model_validator(mode="after")
    def validate_template_sources(self) -> SandboxConfig:
        """Validate template sources configuration."""
        if self.template_sources:
            validate_template_sources_list(self.template_sources)
        return self
