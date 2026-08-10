"""General-purpose utilities.

Groups several concerns that are deliberately agent-agnostic so any agent or
subsystem can reuse them:

* JSON-safe serialization of arbitrary values / dataclasses (``serialize_value``,
  ``dump_dataclass``).
* Environment handling: ``$VAR`` expansion (``expand_env_vars``) and secret
  redaction (``redact_env``).
* Plugin path processing (``process_plugins``).
* Version / reproducibility capture (``get_version_info`` and the ``uip``
  helpers).
"""

import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Collection
from pathlib import Path
from typing import Any, TypeGuard


logger = logging.getLogger(__name__)

# Matches $VAR or ${VAR} env-var references in a path string.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def expand_env_vars(text: str) -> str:
    """Expand ``$VAR`` and ``${VAR}`` references in ``text`` from ``os.environ``.

    Undefined references are left untouched (matching ``os.path.expandvars``
    semantics), so callers can detect and warn about them afterwards.
    """
    return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)), text)


def process_plugins(
    plugins: list[dict[str, Any]],
    *,
    log: logging.Logger | logging.LoggerAdapter[Any] = logger,
) -> list[dict[str, Any]]:
    """Process plugins by expanding environment variable placeholders in paths.

    Expands any $VAR or ${VAR} patterns in plugin paths using environment variables.
    Logs a warning if a path contains an env var reference that is not set.

    Args:
        plugins: List of plugin configuration dictionaries (with optional 'path' keys)
        log: Logger (or adapter) for the undefined-env-var warning; defaults to
            this module's logger.

    Returns:
        List of processed plugin configurations with env vars expanded
    """
    if not plugins:
        return []

    processed = []

    for plugin in plugins:
        # Create a copy to avoid modifying the original
        processed_plugin = dict(plugin)

        # Expand env vars in path if present
        if "path" in processed_plugin:
            path = processed_plugin["path"]
            # Check for unset env vars before expansion (for better error messages)
            for match in _ENV_VAR_PATTERN.finditer(path):
                # group(1) is ${VAR}, group(2) is $VAR
                var_name = match.group(1) or match.group(2)
                if var_name not in os.environ:
                    log.warning(f"Plugin path contains undefined environment variable ${var_name}: {path}")

            # Expand all env vars in the path, then resolve relative paths
            # against the process cwd (not the sandbox cwd) so plugins are found
            expanded = expand_env_vars(path)
            processed_plugin["path"] = str(Path(expanded).resolve())

        processed.append(processed_plugin)

    return processed


SKIP = object()  # Sentinel marking values that serialize_value should drop from the result.


def serialize_value(
    value: Any,
    *,
    skip: Any = SKIP,
    skip_if_has_attrs: Collection[str] = frozenset({"write", "read"}),
) -> Any:
    """Recursively serialize a value to JSON-safe types.

    Traverses dataclasses, dicts, lists, and tuples, converting ``Path`` to
    ``str`` and falling back to ``str(value)`` for unknown types.

    Returns the ``skip`` sentinel for values that should be excluded from the
    result: callables, and any object exposing *all* of ``skip_if_has_attrs``
    (file-like objects — those with both ``write`` and ``read`` — by default).
    Callers drop any field/item that comes back as ``skip``.

    Args:
        value: The value to serialize.
        skip: Sentinel returned for non-serializable values; callers compare
            results against this same object with ``is`` to filter them out.
        skip_if_has_attrs: Attribute names that, when all present on a value,
            mark it as non-serializable. Defaults to file-like detection
            (``{"write", "read"}``); pass an empty collection to disable.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if callable(value):
        return skip
    if skip_if_has_attrs and all(hasattr(value, attr) for attr in skip_if_has_attrs):
        return skip
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        serialized: dict[str, Any] = {}
        for field in dataclasses.fields(value):
            field_value = serialize_value(getattr(value, field.name), skip=skip, skip_if_has_attrs=skip_if_has_attrs)
            if field_value is not skip:
                serialized[field.name] = field_value
        return serialized
    if isinstance(value, dict):
        serialized_dict: dict[str, Any] = {}
        for k, v in value.items():
            v_serialized = serialize_value(v, skip=skip, skip_if_has_attrs=skip_if_has_attrs)
            if v_serialized is not skip:
                serialized_dict[str(k)] = v_serialized
        return serialized_dict
    if isinstance(value, (list, tuple)):
        serialized_list: list[Any] = []
        for item in value:
            item_serialized = serialize_value(item, skip=skip, skip_if_has_attrs=skip_if_has_attrs)
            if item_serialized is not skip:
                serialized_list.append(item_serialized)
        return serialized_list
    # Fallback: convert unknown types to string representation
    return str(value)


SENSITIVE_ENV_KEYWORDS = {"TOKEN", "KEY", "SECRET"}


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Redact sensitive values from an environment variable dict.

    Keys containing TOKEN, KEY, or SECRET (case-insensitive) are replaced with ***REDACTED***.
    """
    return {k: "***REDACTED***" if any(kw in k.upper() for kw in SENSITIVE_ENV_KEYWORDS) else v for k, v in env.items()}


def dump_dataclass(obj: Any, *, skip: Any = SKIP) -> dict[str, Any]:
    """Dump a dataclass instance to a plain JSON-serializable dict.

    Recursively serializes each field via :func:`serialize_value`, skipping
    non-serializable values (callables, file-like objects). A field named
    ``env`` holding a dict is passed through :func:`redact_env` so secrets
    (tokens, keys) never reach the dump.

    Args:
        obj: A dataclass instance.
        skip: Sentinel forwarded to :func:`serialize_value` to mark dropped values.

    Returns:
        Dictionary of field names to JSON-serializable values.
    """
    result: dict[str, Any] = {}
    for field in dataclasses.fields(obj):
        value = serialize_value(getattr(obj, field.name), skip=skip)
        if value is not skip:
            if field.name == "env" and isinstance(value, dict):
                value = redact_env(value)
            result[field.name] = value
    return result


def get_default_docker_image_tag() -> str:
    """Return the default coder-eval-agent image tag for this package version.

    Returns 'coder-eval-agent:<version>' if installed, or 'coder-eval-agent:latest'
    if running from source without -e installation.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"coder-eval-agent:{version('coder-eval')}"
    except PackageNotFoundError:
        logger.debug("coder-eval package not installed; defaulting image tag to :latest")
        return "coder-eval-agent:latest"


def _git_short_sha(repo_path: Path) -> str:
    """Return short HEAD SHA for a git repo, or 'unknown' if not a git repo / git missing."""
    if not repo_path.exists():
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=repo_path,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Best-effort metadata lookup: treat git/process/filesystem issues as unavailable.
        return "unknown"
    return "unknown"


# A semver-ish version token: major.minor.patch with an optional leading `v`
# and any prerelease/build tail (e.g. `1.196.0-alpha.20260605.7426`). Anchored
# at the start of a line so it rejects non-version `uip --version` output.
_VERSION_TOKEN = re.compile(r"^v?\d+\.\d+\.\d+\S*$")


def looks_like_version(value: object) -> TypeGuard[str]:
    """True when ``value`` is a string that parses as a ``major.minor.patch`` version.

    Guards every version-capture sink against non-version ``uip --version``
    output: newer CLI builds can print a JSON envelope (e.g.
    ``{"Result": "Success"}``) or an auto-update/sync line instead of a bare
    version, and a raw ``stdout.strip()`` would otherwise record that verbatim.
    """
    return isinstance(value, str) and bool(_VERSION_TOKEN.match(value.strip()))


def _uip_version(search_path: str | None = None) -> str:
    """Return the ``uip --version`` version string, or 'unknown'.

    ``search_path`` overrides the PATH used to resolve ``uip``; pass the
    agent-aligned PATH to report the binary the agent actually executed
    instead of whichever ``uip`` this process happens to see first.

    The output is validated against :func:`looks_like_version` and the first
    version-shaped line is returned — so a CLI that prefixes the version with
    an auto-update/sync envelope (newer builds do) still yields the version,
    and one that prints only a non-version envelope yields ``"unknown"``
    rather than polluting ``cli_version`` with ``{"Result": "Success"}``.
    """
    uip = "uip"
    if search_path is not None:
        resolved = shutil.which("uip", path=search_path)
        if not resolved:
            return "unknown"
        uip = resolved
    try:
        result = subprocess.run([uip, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=5)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if looks_like_version(stripped):
                    return stripped
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"
    return "unknown"


def resolve_uipath_plugin_dir(search_path: str | None = None) -> Path | None:
    """Resolve the canonical ``node_modules/@uipath`` plugin-tools directory.

    Shared core of ``Sandbox._refresh_plugin_tools_dir`` and
    :func:`_resolve_plugin_tools_dir` (MST-9795). Resolve ``uip`` on
    ``search_path`` (``None`` → process PATH), follow symlinks (Bun installs
    ``uip`` as a symlink into the package dist), and walk up to the first
    ``@uipath`` directory whose parent is ``node_modules``.

    ``search_path`` is forwarded to :func:`shutil.which`; pass the
    agent-aligned PATH (``Sandbox.uip_search_path``) to resolve the binary the
    agent actually executed, or ``None`` to use this process's PATH.

    Returns ``None`` when no usable ``uip`` is on PATH or the resolved binary
    does not live inside a recognizable ``node_modules/@uipath`` tree (e.g.
    development monorepo runs). Logs at debug on every non-resolving branch.
    """
    resolved = shutil.which("uip", path=search_path)
    if not resolved:
        return None
    try:
        real = Path(resolved).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        logger.debug("Failed to resolve `uip` symlink %s: %s", resolved, exc)
        return None
    # Walk up looking for `.../node_modules/@uipath`. We accept the first
    # @uipath dir whose parent is named `node_modules` — the cli is always
    # inside one, e.g. `~/.bun/.../node_modules/@uipath/cli/dist/index.js`.
    for ancestor in real.parents:
        if ancestor.name == "@uipath" and ancestor.parent.name == "node_modules":
            logger.debug("Resolved @uipath plugin-tools dir=%s from `uip` at %s", ancestor, real)
            return ancestor
    logger.debug("`uip` at %s is not inside a recognizable node_modules/@uipath tree", real)
    return None


def _resolve_plugin_tools_dir() -> Path | None:
    """Resolve the canonical ``node_modules/@uipath`` plugin-tools directory.

    An external ``PLUGIN_TOOLS_DIR`` pin wins; otherwise delegates to
    :func:`resolve_uipath_plugin_dir` against this process's PATH.
    """
    if pinned := os.environ.get("PLUGIN_TOOLS_DIR"):
        pinned_path = Path(pinned)
        return pinned_path if pinned_path.is_dir() else None

    return resolve_uipath_plugin_dir()


def _tool_plugin_versions(tools_dir: Path | None = None) -> dict[str, str]:
    """Return ``{plugin_name: version}`` for installed ``@uipath/*-tool`` plugins.

    The UiPath CLI shell (``@uipath/cli``, reported as ``cli_version``) and its
    tool plugins (e.g. ``@uipath/maestro-tool``) are versioned independently, so
    the shell version alone can mislead regression timelines. Enumerates the
    ``@uipath`` plugin-tools dir for packages whose ``package.json`` name ends in
    ``-tool`` and records their ``version``.

    ``tools_dir`` overrides the directory to enumerate (e.g. the sandbox's
    agent-aligned ``plugin_tools_dir``); when ``None``, resolve from this
    process's own environment via :func:`_resolve_plugin_tools_dir`.

    Best-effort: a missing dir or unreadable/unparseable ``package.json`` is
    skipped, never raised — capturing reproducibility metadata must not fail a run.
    """
    if tools_dir is None:
        tools_dir = _resolve_plugin_tools_dir()
    if tools_dir is None or not tools_dir.is_dir():
        return {}

    plugins: dict[str, str] = {}
    # npm installs ``@uipath/<pkg>`` into ``@uipath/<pkg>/``, so a ``-tool`` plugin
    # dir is always named ``<pkg>-tool``. Mirror the manifest-name contract
    # (``name.endswith("-tool")`` below) in the glob to avoid statting unrelated dirs.
    for pkg_json in sorted(tools_dir.glob("*-tool/package.json")):
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Failed to read or parse tool-plugin package.json at %s: %s", pkg_json, exc)
            continue
        name = data.get("name")
        if not isinstance(name, str) or not name.endswith("-tool"):
            continue
        # Key on the short package name (drop the @uipath scope) for readability.
        short_name = name.rsplit("/", 1)[-1]
        version = data.get("version")
        plugins[short_name] = version if isinstance(version, str) else "unknown"
    return plugins


def _cli_version_from_manifest(tools_dir: Path | None) -> str | None:
    """Read ``@uipath/cli``'s version straight from its installed ``package.json``.

    This is the same source of truth :func:`_tool_plugin_versions` reads for the
    tool plugins — the published package's ``package.json`` carries the full
    ``-alpha.<date>.<run>`` version (CI stamps it at publish time), and
    ``uip --version`` merely prints that field. Reading the manifest avoids
    parsing ``uip --version`` stdout, which newer CLI builds can pollute with a
    JSON envelope / auto-update line. The CLI lives at ``<tools_dir>/cli`` (the
    plugin-tools dir is derived by walking up from the resolved ``uip`` binary).

    Returns ``None`` (so callers fall back to the validated stdout path) when the
    dir/manifest is absent or the version isn't version-shaped — e.g. in-process
    dev runs where ``uip`` isn't inside a ``node_modules/@uipath`` tree.
    """
    if tools_dir is None:
        return None
    try:
        data = json.loads((tools_dir / "cli" / "package.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read @uipath/cli package.json under %s: %s", tools_dir, exc)
        return None
    version = data.get("version")
    return version if looks_like_version(version) else None


def _resolve_cli_version(tools_dir: Path | None, search_path: str | None) -> str:
    """The CLI shell version: prefer its ``package.json`` manifest, fall back to ``uip --version``.

    Symmetric with ``tool_plugins`` (manifest-first); the validated stdout path
    (:func:`_uip_version`) covers installs not under ``node_modules/@uipath``.
    """
    return _cli_version_from_manifest(tools_dir) or _uip_version(search_path)


def runtime_uip_versions(plugin_tools_dir: str | Path | None, search_path: str | None = None) -> dict[str, Any]:
    """Capture uip shell + tool-plugin versions as resolved at task runtime.

    :func:`get_version_info` resolves ``uip`` from this process's own
    environment, which describes the *pre-task* state: under ``--driver
    docker`` the CLI auto-installs/upgrades its ``@uipath/*-tool`` plugins on
    first use inside the task, so versions captured at setup time are the
    image-baked ones, not what the agent and criteria actually executed
    (coder_eval#366 follow-up). Call this AFTER the task with the sandbox's
    agent-aligned ``plugin_tools_dir``/PATH to record the real runtime state.

    Strict about its inputs, unlike :func:`get_version_info`: a ``None``
    ``plugin_tools_dir`` yields ``tool_plugins == {}`` rather than falling
    back to process-env discovery — on an in-process (non-docker) run that
    fallback would reach into the host's installs, re-introducing exactly the
    host-pollution this function exists to remove. Callers keep their
    setup-time values on empty results instead.

    Returns a partial env-info dict (``cli_version`` + ``tool_plugins``)
    suitable for ``environment_info.update(...)``. Best-effort like the rest
    of the version capture: never raises.
    """
    tools_dir = Path(plugin_tools_dir) if plugin_tools_dir else None
    return {
        "cli_version": _resolve_cli_version(tools_dir, search_path),
        "tool_plugins": _tool_plugin_versions(tools_dir) if tools_dir else {},
    }


def get_version_info(sandbox_path: Path | None = None) -> dict[str, Any]:
    """Captures versions of key dependencies for reproducibility.

    Args:
        sandbox_path: Optional path to sandbox directory. When provided,
            CLAUDE.md in the sandbox will be hashed for reproducibility tracking.

    Returns:
        Dictionary containing version information for critical dependencies.
    """
    version_info = {}

    # Get git commit hash (pinned to project root, not CWD which may be a sandbox)
    project_root = Path(__file__).resolve().parent.parent
    version_info["git_commit"] = _git_short_sha(project_root)

    # Sibling repos that contribute to the agent's runtime context.
    # Path resolution: env var first (CODER_EVAL_SKILLS_DIR), then sibling-of-coder_eval default.
    # A downstream runner can set this env var to its configured path so custom layouts get the right SHA.
    sibling_root = project_root.parent.parent
    skills_override = os.environ.get("CODER_EVAL_SKILLS_DIR")
    skills_path = Path(skills_override) if skills_override else sibling_root / "skills"
    version_info["skills_git_commit"] = _git_short_sha(skills_path)

    # uip CLI is installed via npm; read its version from @uipath/cli's
    # package.json (same source tool_plugins uses), falling back to a validated
    # `uip --version`. Consumed by downstream run-summary tooling.
    tools_dir = resolve_uipath_plugin_dir()
    version_info["cli_version"] = _resolve_cli_version(tools_dir, None)

    # The CLI shell (cli_version) and its `@uipath/*-tool` plugins (e.g.
    # maestro-tool) version independently, so the shell version alone can
    # mislead regression timelines. Record the installed plugin versions too.
    version_info["tool_plugins"] = _tool_plugin_versions(tools_dir)

    # Get coder_eval version
    from importlib.metadata import PackageNotFoundError, version

    try:
        version_info["coder_eval"] = version("coder_eval")
    except PackageNotFoundError:
        version_info["coder_eval"] = "unknown"

    # Try to get Claude CLI version
    try:
        result = subprocess.run(
            ["claude", "-v"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        version_info["claude_code_cli"] = result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        version_info["claude_code_cli"] = "Not Found"

    # Try to get uv version
    try:
        result = subprocess.run(
            ["uv", "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        version_info["uv"] = result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        version_info["uv"] = "Not Found"

    # Get Python packages
    try:
        import anthropic

        version_info["anthropic"] = anthropic.__version__
    except (ImportError, AttributeError):
        version_info["anthropic"] = "Not Installed"

    try:
        import openai  # pyright: ignore[reportMissingImports]

        version_info["openai"] = openai.__version__
    except (ImportError, AttributeError):
        version_info["openai"] = "Not Installed"

    try:
        import pydantic

        version_info["pydantic"] = pydantic.__version__
    except (ImportError, AttributeError):
        version_info["pydantic"] = "Not Installed"

    # Hash CLAUDE.md if sandbox path provided
    if sandbox_path:
        claude_md = sandbox_path / "CLAUDE.md"
        if claude_md.is_file():
            import hashlib

            content = claude_md.read_bytes()
            version_info["claude_md_sha256"] = hashlib.sha256(content).hexdigest()
            version_info["claude_md_size_bytes"] = str(len(content))

    return version_info
