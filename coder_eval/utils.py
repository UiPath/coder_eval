"""Utility functions for version tracking and reproducibility."""

import subprocess


def get_version_info() -> dict[str, str]:
    """Captures versions of key dependencies for reproducibility.

    Returns:
        Dictionary containing version information for critical dependencies.
    """
    version_info = {}

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

    return version_info
