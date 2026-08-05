"""World-readable skill-DOCS projection (dependency-free leaf).

Under the docker user/permission isolation barrier the raw skills-repo mount is
locked root-0700 (it carries grader trees, reference agents, RESOLUTION.md — all
grading material). The agent still needs the *documentation* half of a plugin to
discover and use a skill, so the host stages a sanitized copy carrying ONLY the
plugin-discovery subtrees (skills/commands/agents/hooks/.claude-plugin) and chowns
that copy to the agent uid.

``PLUGIN_AGENT_ALLOWED_SUBDIRS`` is the allowlist — an allowlist, not a denylist,
so a new answer-bearing directory added to a plugin repo is excluded by default
(it is only readable if explicitly added here). ``project_plugin_for_agent``
copies only those subtrees, dropping ``tests/``, ``reference_agents/``,
``fixtures/``, ``seeds/``, ``RESOLUTION.md``, and everything else.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


# Claude Code's plugin discovery surface. Only these top-level subdirs of a
# plugin root are exposed to the agent uid; grader / reference / fixture trees
# are never in this set.
PLUGIN_AGENT_ALLOWED_SUBDIRS = frozenset({"skills", "commands", "agents", ".claude-plugin", "hooks"})


def plugin_path(plugin: Any) -> str | None:
    """Extract a plugin entry's ``path`` regardless of its runtime shape.

    A plugin entry is a ``LocalPluginConfig`` (a ``TypedDict`` — plain ``dict`` at
    runtime) today, but a future refactor could make it a Pydantic model. This is
    the single accessor every isolation-barrier site uses (staging, skill-DOCS
    projection, auto-mount, the in-container lock) so that flip cannot silently
    disable the lock at any one site: it handles a mapping (``.get("path")``) AND
    an object exposing a ``path`` attribute, returning the value only when it is a
    non-empty string, else ``None``.
    """
    raw: Any = plugin.get("path") if isinstance(plugin, dict) else getattr(plugin, "path", None)
    return raw if isinstance(raw, str) and raw else None


def project_plugin_for_agent(src: Path, dst: Path) -> None:
    """Copy only the agent-legitimate subtrees of plugin root ``src`` into ``dst``.

    Copies each present ``PLUGIN_AGENT_ALLOWED_SUBDIRS`` entry, skipping graders,
    references, fixtures, seeds, RESOLUTION.md and any other top-level content. If
    ``src`` has none of the allowed subdirs the result is an empty ``dst`` (the
    agent sees no skills — correct; nothing leaks).

    ``dst`` is created if absent. Symlinks are copied verbatim (not followed) to
    stay loop-proof against self-referential marketplace symlinks, mirroring
    ``_copy_claude_home``. The allowlist is symlink-target-safe: a relative link
    inside an allowed subtree (e.g. ``skills/x -> ../tests/check.py``) resolves
    within the sanitized copy root — where the grader tree was never copied — so it
    dangles harmlessly; an absolute link resolves against the container's own
    rootfs, not the host or the locked grader mount.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for name in sorted(PLUGIN_AGENT_ALLOWED_SUBDIRS):
        sub = src / name
        if not sub.exists():
            continue
        target = dst / name
        if sub.is_dir():
            shutil.copytree(
                sub,
                target,
                symlinks=True,
                ignore_dangling_symlinks=True,
                dirs_exist_ok=True,
            )
        else:
            # .claude-plugin can be a file (plugin manifest) in some layouts.
            shutil.copy2(sub, target, follow_symlinks=False)
