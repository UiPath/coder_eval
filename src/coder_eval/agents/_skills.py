"""Shared ``agent.plugins`` -> skills-directory resolver for CLI harnesses.

Both OpenCode (maps each skills dir into ``skills.paths`` in
``OPENCODE_CONFIG_CONTENT``) and Pi (passes each as a ``--skill <dir>`` argument)
honor only the *skills* half of a Claude plugin. This module holds that one
resolver so neither agent has to reach into the other's private module for it
(the alternative — ``pi_agent`` importing ``opencode_agent._plugin_skill_dirs`` —
coupled the two harnesses through an implementation-private symbol).

Only the skills half of a plugin is honored. A plugin's agents, hooks, commands
and MCP servers have no CLI equivalent and are dropped by both harnesses.
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
    or a list of strings, each relative to the root) and falls back to the
    convention default ``<root>/skills`` when the manifest is absent, unreadable,
    or declares none. Honoring the manifest rather than hardcoding ``skills/``
    keeps a plugin that relocates its skills working on both harnesses.
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
    a ``type: local`` plugin root declares. Shared by OpenCode (``skills.paths``
    in ``OPENCODE_CONFIG_CONTENT``) and Pi (a ``--skill <dir>`` argument each);
    ``harness`` only labels the diagnostics. Every way this can come up empty is
    logged rather than passed over: a plugin whose skills never reach the agent
    still *looks* like a normal run, which is precisely the failure this closes.
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
        # A path that is ALREADY a bare skills directory (<root>/<name>/SKILL.md)
        # has no `skills/` subdir, so use it as-is. Deliberately not a fallback for
        # a root that HAS one: `skills.paths` is scanned recursively and a repo
        # root can contain self-referential symlinks (UiPath/skills has
        # `plugins/uipath -> ..`), which resolves skills through an arbitrary path
        # and silently drops duplicate names.
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
