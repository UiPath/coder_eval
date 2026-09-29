"""Allowlist (default-deny) mask for auto-mounted Claude-plugin trees.

``mask_dirs`` names the child dirs of a mounted plugin root to tmpfs-mask so that only the
plugin surface (``.claude-plugin`` + the manifest-declared skill dirs) stays readable.
See docs/DOCKER_ISOLATION.md for the two residuals the mask cannot cover.
"""

from __future__ import annotations

from pathlib import Path

from coder_eval.agents._skills import manifest_skill_dirs


_PLUGIN_MANIFEST_RELPATH = (".claude-plugin", "plugin.json")


def mask_dirs(root: Path) -> list[Path]:
    """Directories under a mounted plugin ``root`` to tmpfs-mask, or ``[]`` for a non-plugin root.

    A nested skills path keeps its ancestors unmasked but masks their other children;
    symlinked children are skipped.
    """
    root = root.resolve()
    if not (root / Path(*_PLUGIN_MANIFEST_RELPATH)).is_file():
        return []

    keep = {(root / ".claude-plugin").resolve(), *manifest_skill_dirs(root)}

    # A kept path or an ancestor of one: never masked, descended into instead.
    protected: set[Path] = set()
    for kept in keep:
        protected.add(kept)
        for ancestor in kept.parents:
            if ancestor == root:
                break
            if root in ancestor.parents:
                protected.add(ancestor)

    # Never descend into a kept path. Under manifest `skills: "."` the root itself
    # is kept, so nothing is masked (the caller warns).
    descend = {p for p in ({root} | protected) if p not in keep}

    masked: set[Path] = set()
    for directory in descend:
        if not directory.is_dir() or directory.is_symlink():
            continue
        for child in directory.iterdir():
            if child.is_symlink():
                continue
            if not child.is_dir():
                continue
            resolved = child.resolve()
            if resolved in protected:
                continue
            masked.add(resolved)

    return sorted(masked)
