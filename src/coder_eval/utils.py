"""Utility functions for version tracking and reproducibility."""

import os
import subprocess
from pathlib import Path
from typing import Any


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
    # Path resolution: env var first (CODER_EVAL_{SKILLS,CLI}_DIR), then sibling-of-coder_eval default.
    # The dashboard sets these env vars to its configured paths so custom layouts get the right SHA.
    sibling_root = project_root.parent.parent
    for name, env_var in (("skills", "CODER_EVAL_SKILLS_DIR"), ("cli", "CODER_EVAL_CLI_DIR")):
        override = os.environ.get(env_var)
        repo_path = Path(override) if override else sibling_root / name
        version_info[f"{name}_git_commit"] = _git_short_sha(repo_path)

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
