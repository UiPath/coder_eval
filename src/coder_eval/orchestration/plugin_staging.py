"""Stage every ``agent.plugins`` entry into one canonical plugin root each harness receives.

The canonical root is ``<root>/.claude-plugin/plugin.json`` plus
``<root>/skills/<name>`` (a symlink to the authored skill directory, or a copy where
symlinks fail). A plugin root is read the way Claude Code reads it: the default
``skills/`` plus every manifest-declared path, where a path may parent skills or be one
skill, and a root holding ``SKILL.md`` is a single-skill plugin. A bare skills
directory is accepted too. A skill's name is its ``SKILL.md`` frontmatter ``name``,
else its directory name.

Rationale: .claude/notes/agents.md § Skills, per harness
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from coder_eval.models import SkillTriggeredCriterion
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


def _declared_skill_paths(root: Path) -> list[Path]:
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
    return [(root / relative).resolve() for relative in declared]


def _skill_name(skill_dir: Path) -> str:
    """The frontmatter ``name`` of ``<skill_dir>/SKILL.md``, else the directory name."""
    text = (skill_dir / _SKILL_FILE).read_text(encoding="utf-8", errors="replace")
    if text.startswith("---"):
        front, _, _rest = text[3:].partition("\n---")
        try:
            data: Any = yaml.safe_load(front)
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"].strip():
            return data["name"].strip()
    return skill_dir.name


def _skill_dirs(root: Path) -> list[Path]:
    """Every skill directory one ``plugins:`` root offers, in Claude Code's reading order."""
    candidates = [root / _DEFAULT_SKILLS_SUBDIR, *_declared_skill_paths(root)]
    found: list[Path] = []
    for candidate in candidates:
        if (candidate / _SKILL_FILE).is_file():
            found.append(candidate)
        elif candidate.is_dir():
            found += [skill_file.parent for skill_file in sorted(candidate.glob(f"*/{_SKILL_FILE}"))]
    if found:
        return found
    if (root / _SKILL_FILE).is_file():
        return [root]
    return [skill_file.parent for skill_file in sorted(root.glob(f"*/{_SKILL_FILE}"))]


def scan_plugin_skills(plugins: Sequence[LocalPluginConfig]) -> dict[str, Path]:
    """Skill name -> its directory, over every entry and every accepted layout.

    Raises:
        PluginStagingError: an unresolvable path, a skill name from two sources, or no skill at all.
    """
    skills: dict[str, Path] = {}
    for plugin in plugins:
        for skill_dir in _skill_dirs(resolve_plugin_path(plugin["path"])):
            name, source = _skill_name(skill_dir), skill_dir.resolve()
            previous = skills.get(name)
            if previous is not None and previous != source:
                raise PluginStagingError(
                    f"ambiguous skill name {name!r}: agent.plugins offers it from both {previous} and {source}"
                )
            skills[name] = source
    if not skills:
        paths = [plugin["path"] for plugin in plugins]
        raise PluginStagingError(
            f"agent.plugins {paths} offers no skill: point each path at a plugin root holding "
            + "skills/<name>/SKILL.md or at a bare skills directory holding <name>/SKILL.md"
        )
    return skills


def validate_plugins(task: TaskDefinition) -> None:
    """Resolution-time refusal; no-op when plugins is unset or empty.

    Also refuses a ``skill_triggered`` criterion whose ``skill_name`` the plugins do
    not offer, before the run is paid for. A name still holding a ``${row...}``
    placeholder is checked on its expanded row instead.

    Raises:
        PluginStagingError: see ``scan_plugin_skills``, or a ``skill_triggered`` target not offered.
    """
    plugins = task.agent.plugins if task.agent is not None else None
    if not plugins:
        return
    offered = scan_plugin_skills(plugins)
    targets = {c.skill_name for c in task.success_criteria if isinstance(c, SkillTriggeredCriterion)}
    missing = sorted(name for name in targets if "${" not in name and name not in offered)
    if missing:
        raise PluginStagingError(
            f"skill_triggered names skill(s) {missing} but agent.plugins offers only {sorted(offered)}: "
            + "the positive control cannot run. With agent.plugins set, the skill under test must come "
            + "from a plugin path (a skill from a template or setting_sources is not offered)."
        )


def link_or_copy(source: Path, target: Path) -> None:
    """Symlink ``target`` to ``source``, or copy the tree where symlinks are unavailable."""
    try:
        target.symlink_to(source, target_is_directory=True)
    except (OSError, NotImplementedError):
        shutil.copytree(source, target, dirs_exist_ok=True)


def stage_plugins(plugins: Sequence[LocalPluginConfig], staging_dir: Path) -> StagedPlugins:
    """Write ``<staging_dir>/.claude-plugin/plugin.json`` and ``<staging_dir>/skills/<name>``.

    The returned root is absolute: every harness runs with the sandbox as its cwd. An
    existing ``staging_dir`` is removed first, so a re-executed row starts clean.

    Raises:
        PluginStagingError: see ``scan_plugin_skills``.
    """
    skills = scan_plugin_skills(plugins)
    staging_dir = staging_dir.absolute()
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
