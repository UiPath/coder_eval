"""Utility functions for version tracking and reproducibility."""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


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


def _uip_version(search_path: str | None = None) -> str:
    """Return `uip --version` output, or 'unknown' if the CLI isn't installed.

    ``search_path`` overrides the PATH used to resolve ``uip``; pass the
    agent-aligned PATH to report the binary the agent actually executed
    instead of whichever ``uip`` this process happens to see first.
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
            return result.stdout.strip() or "unknown"
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
    return {
        "cli_version": _uip_version(search_path),
        "tool_plugins": _tool_plugin_versions(Path(plugin_tools_dir)) if plugin_tools_dir else {},
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
    # The dashboard sets this env var to its configured path so custom layouts get the right SHA.
    sibling_root = project_root.parent.parent
    skills_override = os.environ.get("CODER_EVAL_SKILLS_DIR")
    skills_path = Path(skills_override) if skills_override else sibling_root / "skills"
    version_info["skills_git_commit"] = _git_short_sha(skills_path)

    # uip CLI is installed via npm; capture `uip --version`. Read by
    # dashboard/scripts/ci/slack_summary.py.
    version_info["cli_version"] = _uip_version()

    # The CLI shell (cli_version) and its `@uipath/*-tool` plugins (e.g.
    # maestro-tool) version independently, so the shell version alone can
    # mislead regression timelines. Record the installed plugin versions too.
    version_info["tool_plugins"] = _tool_plugin_versions()

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
