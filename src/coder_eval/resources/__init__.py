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


def normalize_ignore_pattern_entry(raw: str) -> str:
    """Strip whitespace from an ignore-pattern entry and validate its shape.

    Returns the cleaned entry. Raises ``ValueError`` on empty / whitespace-only
    entries and on bare ``"!"`` (negation with no target). Shared by the
    runtime resolver (:func:`get_ignore_patterns`) and Pydantic
    ``field_validator`` hooks on ``SandboxConfig.ignore_patterns`` and
    ``AgentConfig.ignore_patterns`` so malformed YAML fails at load time
    rather than mid-run.
    """
    entry = raw.strip()
    if not entry:
        raise ValueError(f"ignore pattern entry is empty or whitespace-only: {raw!r}")
    if entry == "!":
        raise ValueError("ignore pattern entry '!' is bare negation with no target")
    return entry


def get_ignore_patterns(additional_patterns: list[str] | None = None) -> set[str]:
    """Get ignore patterns with optional additions and negations.

    Args:
        additional_patterns: Optional list of pattern overrides. Plain entries
            are added on top of the defaults; entries prefixed with ``!`` are
            removed from the defaults (gitignore-style negation). Example:
            ``["!dist", "!node_modules", "*.bak"]`` un-ignores ``dist`` and
            ``node_modules`` (so vendored JS build outputs survive sandbox
            copy) and additionally ignores ``*.bak`` files.

    Returns:
        Combined set of default and additional patterns, with negations
        applied to the defaults.

    Raises:
        ValueError: If any entry fails :func:`normalize_ignore_pattern_entry`.
    """
    patterns = load_default_ignore_patterns().copy()

    if additional_patterns:
        for raw in additional_patterns:
            entry = normalize_ignore_pattern_entry(raw)
            if entry.startswith("!"):
                patterns.discard(entry[1:])
            else:
                patterns.add(entry)

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
