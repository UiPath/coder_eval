"""Resource management for default configurations."""

from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache(maxsize=1)
def load_default_ignore_patterns() -> set[str]:
    """Load default ignore patterns from YAML resource file.

    Returns:
        Set of pattern strings to ignore during file operations.

    Note:
        Result is cached for performance. Changes to the YAML file
        require process restart to take effect.
    """
    resource_dir = Path(__file__).parent
    patterns_file = resource_dir / "default_ignore_patterns.yaml"

    if not patterns_file.exists():
        # Fallback to hardcoded patterns if file missing
        return {
            ".venv",
            "venv",
            "ENV",
            "env",
            ".git",
            ".svn",
            "__pycache__",
            ".pytest_cache",
            "*.pyc",
            "*.egg-info",
            "dist",
            "build",
            "node_modules",
            ".DS_Store",
            "Thumbs.db",
        }

    with open(patterns_file, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Flatten all categories into a single set
    patterns = set()
    for category in data.values():
        if isinstance(category, list):
            patterns.update(category)

    return patterns


def get_ignore_patterns(additional_patterns: list[str] | None = None) -> set[str]:
    """Get ignore patterns with optional additions.

    Args:
        additional_patterns: Optional list of additional patterns to include

    Returns:
        Combined set of default and additional patterns
    """
    patterns = load_default_ignore_patterns().copy()

    if additional_patterns:
        patterns.update(additional_patterns)

    return patterns


def matches_pattern(path_part: str, pattern: str) -> bool:
    """Check if a path part matches an ignore pattern.

    Supports:
    - Exact match: "node_modules"
    - Prefix wildcard: "*.pyc" (matches "file.pyc")
    - Suffix wildcard: "test_*" (matches "test_utils.py")

    Args:
        path_part: Single path component (e.g., "file.pyc")
        pattern: Pattern to match against

    Returns:
        True if path_part matches pattern
    """
    if pattern.startswith("*"):
        return path_part.endswith(pattern[1:])
    elif pattern.endswith("*"):
        return path_part.startswith(pattern[:-1])
    else:
        return path_part == pattern


def should_ignore_path(path: Path, patterns: set[str]) -> bool:
    """Check if any part of a path matches ignore patterns.

    Args:
        path: Path to check
        patterns: Set of patterns to match against

    Returns:
        True if any part of the path matches any pattern
    """
    return any(any(matches_pattern(str(part), pattern) for pattern in patterns) for part in path.parts)
