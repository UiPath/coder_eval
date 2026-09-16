"""Stage every ``agent.plugins`` entry into one canonical plugin root each harness receives.

The canonical root is ``<root>/.claude-plugin/plugin.json`` plus
``<root>/skills/<name>`` (a symlink to the authored skill directory, or a copy where
symlinks fail). Both authored layouts are accepted: a plugin root whose manifest (or
default ``skills/``) parents the skills, and a bare skills directory.

Rationale: .claude/notes/agents.md § Skills, per harness
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coder_eval.orchestration.harness_contract import TaskResolutionError
from coder_eval.utils import expand_env_vars


if TYPE_CHECKING:
    from coder_eval.models import LocalPluginConfig, TaskDefinition


STAGED_MANIFEST: dict[str, str] = {"name": "coder-eval-plugins"}

_MANIFEST_RELPATH = (".claude-plugin", "plugin.json")
_DEFAULT_SKILLS_SUBDIR = "skills"
_SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class StagedPlugins:
    """The staged root handed to ``Agent.start`` and the skill names it offers."""

    root: Path
    skills_offered: tuple[str, ...]


class PluginStagingError(TaskResolutionError):
    """A plugins: entry that yields no skill, a duplicate skill name, or an unresolvable path."""


def resolve_plugin_path(raw: str) -> Path:
    """``raw`` with ``$VAR`` references expanded, resolved against the process cwd.

    Raises:
        PluginStagingError: the expanded path still names a variable or is not a directory.
    """
    expanded = expand_env_vars(raw)
    root = Path(expanded).resolve()
    if "$" in expanded or not root.is_dir():
        hint = "env var likely unset" if "$" in expanded else "path does not exist"
        raise PluginStagingError(
            f"agent.plugins path {raw!r} resolved to {expanded!r}, which is not a directory ({hint})"
        )
    return root


def _declared_skill_dirs(root: Path) -> list[Path]:
    manifest = root.joinpath(*_MANIFEST_RELPATH)
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
        declared = [_DEFAULT_SKILLS_SUBDIR]
    return [(root / relative).resolve() for relative in declared]


def scan_plugin_skills(plugins: Sequence[LocalPluginConfig]) -> dict[str, Path]:
    """Skill name -> its directory, over every entry, both authored layouts.

    For each root, the manifest-declared skill directories that exist are scanned;
    if none exists, the root itself is a bare skills directory.

    Raises:
        PluginStagingError: an unresolvable path, a skill name from two sources, or no skill at all.
    """
    skills: dict[str, Path] = {}
    for plugin in plugins:
        root = resolve_plugin_path(plugin["path"])
        candidates = [directory for directory in _declared_skill_dirs(root) if directory.is_dir()] or [root]
        for candidate in candidates:
            for skill_file in sorted(candidate.glob(f"*/{_SKILL_FILE}")):
                skill_dir = skill_file.parent
                previous = skills.get(skill_dir.name)
                if previous is not None and previous != skill_dir.resolve():
                    raise PluginStagingError(
                        f"ambiguous skill name {skill_dir.name!r}: agent.plugins offers it from both "
                        + f"{previous} and {skill_dir.resolve()}"
                    )
                skills[skill_dir.name] = skill_dir.resolve()
    if not skills:
        paths = [plugin["path"] for plugin in plugins]
        raise PluginStagingError(
            f"agent.plugins {paths} offers no skill: point each path at a plugin root holding "
            + "skills/<name>/SKILL.md or at a bare skills directory holding <name>/SKILL.md"
        )
    return skills


def validate_plugins(task: TaskDefinition) -> None:
    """Resolution-time refusal; no-op when plugins is unset or empty.

    Raises:
        PluginStagingError: see ``scan_plugin_skills``.
    """
    plugins = task.agent.plugins if task.agent is not None else None
    if plugins:
        scan_plugin_skills(plugins)


def link_or_copy(source: Path, target: Path) -> None:
    """Symlink ``target`` to ``source``, or copy the tree where symlinks are unavailable."""
    try:
        target.symlink_to(source, target_is_directory=True)
    except (OSError, NotImplementedError):
        shutil.copytree(source, target, dirs_exist_ok=True)


def stage_plugins(plugins: Sequence[LocalPluginConfig], staging_dir: Path) -> StagedPlugins:
    """Write ``<staging_dir>/.claude-plugin/plugin.json`` and ``<staging_dir>/skills/<name>``.

    An existing ``staging_dir`` is removed first, so a re-executed row starts clean.

    Raises:
        PluginStagingError: see ``scan_plugin_skills``.
    """
    skills = scan_plugin_skills(plugins)
    if staging_dir.is_symlink() or staging_dir.is_file():
        staging_dir.unlink()
    elif staging_dir.exists():
        shutil.rmtree(staging_dir)
    manifest = staging_dir.joinpath(*_MANIFEST_RELPATH)
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(STAGED_MANIFEST), encoding="utf-8")
    skills_dir = staging_dir / _DEFAULT_SKILLS_SUBDIR
    skills_dir.mkdir()
    for name, source in sorted(skills.items()):
        link_or_copy(source, skills_dir / name)
    return StagedPlugins(root=staging_dir, skills_offered=tuple(sorted(skills)))
