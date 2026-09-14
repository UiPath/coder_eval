"""Allowlist (default-deny) mask for auto-mounted Claude-plugin trees.

A ``driver: docker`` run auto-mounts an ``agent.plugins[].path`` (or a
``TemplateDirSource.path`` that is itself a plugin root) at its host path, ``:ro``,
so the plugin loads. That same tree often holds eval material as siblings of the
skills dir -- sibling task YAMLs, reference solutions, test fixtures -- which the
agent under evaluation could then read as its own answer key.

This module answers the one question the docker runner needs: *given a mounted
plugin root, which child directories must be tmpfs-masked so that only the plugin
surface (``.claude-plugin`` + the manifest-declared skill dirs) stays readable?*
It is a **security** control, so the posture is default-deny: keep the skill
surface, mask everything else. An unknown or new eval layout is masked by
default and can never leak.

Pure and dependency-light: a shallow ``iterdir`` + keep-set. No ``os.walk``, no
YAML parsing, no prune-set. The caller (``DockerRunner._build_argv``) emits a
``--tmpfs`` over each returned path and logs it.
"""

from __future__ import annotations

from pathlib import Path

from coder_eval.agents._skills import manifest_skill_dirs


_PLUGIN_MANIFEST_RELPATH = (".claude-plugin", "plugin.json")


def mask_dirs(root: Path) -> list[Path]:
    """Directories under a mounted plugin ``root`` to tmpfs-mask.

    Returns ``[]`` unless ``root`` is a plugin root (has
    ``.claude-plugin/plugin.json``) -- a plain template dir or the
    ``system_prompt_file`` parent carries no skill/eval convention, so its author
    controls it and nothing is masked.

    Otherwise default-deny: the keep-set is ``.claude-plugin`` + the
    manifest-declared skill dirs (via the shared ``manifest_skill_dirs``
    resolver -- one SSOT for "what is a skill dir"; ``"skills"`` is never
    hardcoded). Every other child directory is masked. For a NESTED skills path
    (e.g. declared ``src/skills``) the mask is applied at the granularity needed
    to keep exactly the declared skill dir(s) + ``.claude-plugin`` -- siblings are
    masked at each level down to the skills dir, so ``src/`` is not over-exposed.

    Symlinked children are skipped: a symlink is not a valid tmpfs mountpoint,
    and a symlink inside the mounted tree resolves against the CONTAINER's
    filesystem (which holds no eval material), not the host's -- so it is not a
    leak.
    """
    root = root.resolve()
    if not (root / Path(*_PLUGIN_MANIFEST_RELPATH)).is_file():
        return []

    keep = {(root / ".claude-plugin").resolve(), *manifest_skill_dirs(root)}

    # "Protected" = every kept path AND every ancestor of a kept path down to
    # (but excluding) the root. A protected dir is never masked; instead we
    # descend into it and mask ITS non-protected children. This is what keeps a
    # nested `src/skills` from over-exposing all of `src/`.
    protected: set[Path] = set()
    for kept in keep:
        protected.add(kept)
        for ancestor in kept.parents:
            if ancestor == root:
                break
            if root in ancestor.parents:
                protected.add(ancestor)

    # Directories to descend into: the root plus every protected ANCESTOR (a
    # protected path that is not itself a kept leaf). We never descend into a
    # kept skill dir -- everything inside it stays readable, including the
    # skill's own supporting assets.
    descend = {root, *(p for p in protected if p not in keep)}

    masked: set[Path] = set()
    for directory in descend:
        # Only descend into a real (non-symlink) directory.
        if not directory.is_dir() or directory.is_symlink():
            continue
        for child in directory.iterdir():
            if child.is_symlink():
                # Not a valid tmpfs mountpoint; resolves against the container fs
                # (no host eval material). Skip -- not a leak.
                continue
            if not child.is_dir():
                continue
            resolved = child.resolve()
            if resolved in protected:
                continue
            masked.add(resolved)

    return sorted(masked)
