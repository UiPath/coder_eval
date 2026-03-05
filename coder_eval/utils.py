"""Utility functions for version tracking and reproducibility."""

import subprocess
from pathlib import Path


def get_version_info(sandbox_path: Path | None = None) -> dict[str, str]:
    """Captures versions of key dependencies for reproducibility.

    Args:
        sandbox_path: Optional path to sandbox directory. When provided,
            CLAUDE.md in the sandbox will be hashed for reproducibility tracking.

    Returns:
        Dictionary containing version information for critical dependencies.
    """
    version_info = {}

    # Get coder_eval version
    try:
        from importlib.metadata import version

        version_info["coder_eval"] = version("coder_eval")
    except Exception:
        version_info["coder_eval"] = "unknown"

    # Try to get Claude CLI version
    try:
        result = subprocess.run(["claude", "-v"], capture_output=True, text=True, timeout=5)
        version_info["claude_code_cli"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        version_info["claude_code_cli"] = "Not Found"

    # Try to get uv version
    try:
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=5)
        version_info["uv"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
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
