"""Shared ``agent.plugins`` -> skills-directory resolver for CLI harnesses.

One resolver, so neither OpenCode nor Pi has to reach into the other's private
module for it.

Rationale: .claude/notes/agents.md § Skills, per harness
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from coder_eval.utils import expand_env_vars


logger = logging.getLogger(__name__)

_PLUGIN_MANIFEST_RELPATH = (".claude-plugin", "plugin.json")
_DEFAULT_PLUGIN_SKILLS_SUBDIR = "skills"
_SKILL_FILE = "SKILL.md"


def _manifest_skill_dirs(root: Path) -> list[Path]:
    """Skill directories a Claude-plugin root declares, in manifest order.

    Reads the ``skills`` field of ``<root>/.claude-plugin/plugin.json`` (a string
    or a list, each relative to the root), falling back to ``<root>/skills`` when
    the manifest is absent, unreadable, or declares none.
    """
    manifest = root.joinpath(*_PLUGIN_MANIFEST_RELPATH)
    declared: list[str] = []
    if manifest.is_file():
        try:
            data: Any = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            value = data.get("skills")
            if isinstance(value, str):
                declared = [value]
            elif isinstance(value, list):
                declared = [entry for entry in value if isinstance(entry, str)]
    if not declared:
        declared = [_DEFAULT_PLUGIN_SKILLS_SUBDIR]
    return [(root / relative).resolve() for relative in declared]


def _plugin_skill_dirs(
    plugins: Sequence[Mapping[str, Any]] | None,
    log: logging.Logger | logging.LoggerAdapter[Any] = logger,
    harness: str = "opencode",
) -> list[str]:
    """Resolve ``plugins:`` entries to skill-directory paths for a CLI harness.

    Returns the skills-parent directories (each holding ``<name>/SKILL.md``) that
    a ``type: local`` plugin root declares. ``harness`` only labels the
    diagnostics. EVERY way this can come up empty is logged rather than passed
    over, because a plugin whose skills never reach the agent still *looks* like a
    normal run.
    """
    resolved: list[str] = []
    for plugin in plugins or []:
        if not isinstance(plugin, Mapping) or plugin.get("type") != "local":
            log.warning(f"{harness}: ignoring non-local plugin entry %r — only `type: local` maps to skills.", plugin)
            continue
        path_str = plugin.get("path")
        if not path_str:
            continue
        expanded = expand_env_vars(str(path_str))
        root = Path(expanded).resolve()
        if not root.is_dir():
            hint = "env var likely unset" if "$" in expanded else "path does not exist"
            log.warning(
                f"{harness}: plugin skills path did not resolve: %r -> %r (%s); no skills injected from it",
                path_str,
                expanded,
                hint,
            )
            continue
        candidates = [directory for directory in _manifest_skill_dirs(root) if directory.is_dir()]
        # A path that is ALREADY a bare skills directory has no `skills/` subdir,
        # so use it as-is. Deliberately NOT a fallback for a root that HAS one.
        # Rationale: .claude/notes/agents.md § Skills, per harness
        if not candidates:
            candidates = [root]
        for directory in candidates:
            if next(directory.glob(f"*/{_SKILL_FILE}"), None) is None:
                log.warning(
                    f"{harness}: no <name>/%s directly under %s (from plugin %r) — the CLI still scans it "
                    + "recursively, but check the plugin path points at a skills root",
                    _SKILL_FILE,
                    directory,
                    path_str,
                )
            as_text = str(directory)
            if as_text not in resolved:
                resolved.append(as_text)
    return resolved
