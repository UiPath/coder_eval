"""Stage every ``agent.plugins`` entry into one plugin root each harness receives.

The root holds ``<root>/skills/<name>``, a skills index every harness reads (a symlink
to the authored skill directory, or a copy where symlinks fail), and
``<root>/plugins/<plugin>``, each authored plugin whole for a harness that loads full
plugins (a bare skills directory gets a name-only manifest wrapper). A plugin root is
read the way Claude Code reads it: the default ``skills/`` plus every manifest-declared
path inside the root, where a path may parent skills or be one skill, and a root holding
``SKILL.md`` is a single-skill plugin. A bare skills directory is accepted too. A
skill's name is its ``SKILL.md`` frontmatter ``name``, else its directory name.

Rationale: .claude/notes/agents.md § Skills, per harness
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from coder_eval.models import SkillTriggeredCriterion
from coder_eval.orchestration.harness_contract import TaskResolutionError
from coder_eval.utils import expand_env_vars


if TYPE_CHECKING:
    from coder_eval.models import LocalPluginConfig, TaskDefinition


_MANIFEST_RELPATH = (".claude-plugin", "plugin.json")
_DEFAULT_SKILLS_SUBDIR = "skills"
_PLUGINS_SUBDIR = "plugins"
_SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class StagedPlugins:
    """The staged root handed to ``Agent.start`` (``skills/`` and ``plugins/``) and the skill names it offers."""

    root: Path
    skills_offered: tuple[str, ...]


class PluginStagingError(TaskResolutionError):
    """A plugins: entry that yields no skill, a duplicate skill or plugin name, or an unresolvable path."""


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


def _read_manifest(root: Path) -> dict[str, Any]:
    """The parsed ``.claude-plugin/plugin.json``, or ``{}`` when it is absent, unreadable or not an object."""
    manifest = root.joinpath(*_MANIFEST_RELPATH)
    if not manifest.is_file():
        return {}
    try:
        data: Any = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _declared_skill_paths(root: Path) -> list[Path]:
    value = _read_manifest(root).get("skills")
    if isinstance(value, str):
        declared = [value]
    elif isinstance(value, list):
        declared = [entry for entry in value if isinstance(entry, str)]
    else:
        declared = []
    paths: list[Path] = []
    for relative in declared:
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise PluginStagingError(
                f"agent.plugins {root}: manifest skills path {relative!r} leaves the plugin root; "
                + "Claude Code loads no skill from it"
            )
        paths.append(path)
    return paths


def _plugin_name(root: Path) -> str:
    """The manifest ``name`` when it is a non-empty string, else the directory name: Claude Code's rule."""
    name = _read_manifest(root).get("name")
    return name if isinstance(name, str) and name else root.name


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


def _plugin_skill_dirs(root: Path) -> list[Path]:
    """The skill directories Claude Code itself loads from ``root`` given as a plugin, in its reading order."""
    candidates = [root / _DEFAULT_SKILLS_SUBDIR, *_declared_skill_paths(root)]
    found: list[Path] = []
    for candidate in candidates:
        if (candidate / _SKILL_FILE).is_file():
            found.append(candidate)
        elif candidate.is_dir():
            found += [skill_file.parent for skill_file in sorted(candidate.glob(f"*/{_SKILL_FILE}"))]
    if found:
        return found
    return [root] if (root / _SKILL_FILE).is_file() else []


def _skill_dirs(root: Path) -> list[Path]:
    """Every skill directory one ``plugins:`` root offers: the plugin reading, else a bare skills directory."""
    return _plugin_skill_dirs(root) or [skill_file.parent for skill_file in sorted(root.glob(f"*/{_SKILL_FILE}"))]


def _claim_name(kind: str, name: str, source: Path, taken: Mapping[str, Path], hint: str) -> None:
    """Refuse a staged name that is not one path segment, or that another source took (ignoring case).

    Case is ignored because the staged names are directory entries on a filesystem that may fold case.
    """
    if name in {"", ".", ".."} or any(char in name for char in "/\\\x00"):
        raise PluginStagingError(f"agent.plugins {source}: {kind} name {name!r} is not one path segment; {hint}")
    for other, previous in taken.items():
        if other.casefold() == name.casefold() and previous != source:
            raise PluginStagingError(
                f"ambiguous {kind} name {name!r}: agent.plugins offers it from both {previous} and {source}; {hint}"
            )


def scan_plugin_skills(plugins: Sequence[LocalPluginConfig]) -> dict[str, Path]:
    """Skill name -> its directory, over every entry and every accepted layout.

    Raises:
        PluginStagingError: an unresolvable path, a skill name that is not one path segment, a skill
            name from two sources, or no skill at all.
    """
    skills: dict[str, Path] = {}
    for plugin in plugins:
        for skill_dir in _skill_dirs(resolve_plugin_path(plugin["path"])):
            name, source = _skill_name(skill_dir), skill_dir.resolve()
            _claim_name("skill", name, source, skills, "rename it in its SKILL.md frontmatter")
            skills[name] = source
    if not skills:
        paths = [plugin["path"] for plugin in plugins]
        raise PluginStagingError(
            f"agent.plugins {paths} offers no skill: point each path at a plugin root holding "
            + "skills/<name>/SKILL.md or at a bare skills directory holding <name>/SKILL.md"
        )
    return skills


def scan_plugin_roots(plugins: Sequence[LocalPluginConfig]) -> dict[str, tuple[Path, bool]]:
    """Plugin name -> (resolved root, wrapped), where a wrapped root is a bare skills directory.

    A root is wrapped when Claude Code would load no skill from it as a plugin but it
    holds ``<name>/SKILL.md`` skills. Every other root, including one with no skill, is
    linked whole.

    Raises:
        PluginStagingError: an unresolvable path, a plugin name that is not one path segment,
            or one plugin name from two roots.
    """
    roots: dict[str, tuple[Path, bool]] = {}
    for plugin in plugins:
        root = resolve_plugin_path(plugin["path"])
        name = _plugin_name(root)
        taken = {other: previous for other, (previous, _wrapped) in roots.items()}
        _claim_name("plugin", name, root, taken, "set a distinct name in its .claude-plugin/plugin.json")
        roots[name] = (root, not _plugin_skill_dirs(root) and bool(_skill_dirs(root)))
    return roots


def validate_plugins(task: TaskDefinition) -> None:
    """Resolution-time refusal; no-op when plugins is unset or empty.

    Also refuses a ``skill_triggered`` criterion whose ``skill_name`` the plugins do
    not offer, before the run is paid for. A name still holding a ``${row...}``
    placeholder is checked on its expanded row instead.

    Raises:
        PluginStagingError: see ``scan_plugin_skills`` and ``scan_plugin_roots``, or a ``skill_triggered``
            target not offered.
    """
    plugins = task.agent.plugins if task.agent is not None else None
    if not plugins:
        return
    offered = scan_plugin_skills(plugins)
    scan_plugin_roots(plugins)
    targets = {c.skill_name for c in task.success_criteria if isinstance(c, SkillTriggeredCriterion)}
    missing = sorted(name for name in targets if "${" not in name and name not in offered)
    if missing:
        raise PluginStagingError(
            f"skill_triggered names skill(s) {missing} but agent.plugins offers only {sorted(offered)}: "
            + "the positive control cannot run. With agent.plugins set, the skill under test must come "
            + "from a plugin path (a skill from a template or setting_sources is not offered)."
        )


def link_or_copy(source: Path, target: Path) -> None:
    """Symlink ``target`` to ``source``, or copy the tree where symlinks are unavailable.

    Only a failure to create symlinks at all falls back: an existing or unreachable ``target`` raises. The copy
    follows symlinks, since the host cannot make them, but skips a link to one of its own ancestors and skips
    ``target`` and its ancestors, so neither a link loop nor a source that contains the target recurses.
    """
    try:
        target.symlink_to(source, target_is_directory=True)
    except (FileExistsError, FileNotFoundError):
        raise
    except (OSError, NotImplementedError):
        resolved = target.resolve()

        def _skipped(directory: str, names: list[str]) -> list[str]:
            entries = [Path(directory) / name for name in names]
            return [
                entry.name
                for entry in entries
                if resolved.is_relative_to(entry)
                or (entry.is_symlink() and entry.resolve() in entry.absolute().parents)
            ]

        shutil.copytree(source, target, dirs_exist_ok=True, ignore_dangling_symlinks=True, ignore=_skipped)


def stage_plugins(plugins: Sequence[LocalPluginConfig], staging_dir: Path) -> StagedPlugins:
    """Write ``<staging_dir>/skills/<name>`` per skill and ``<staging_dir>/plugins/<plugin>`` per entry.

    A plugin entry is linked whole; a bare skills directory becomes
    ``plugins/<plugin>/.claude-plugin/plugin.json`` (name only) plus a ``skills`` link. The
    returned root is absolute: every harness runs with the sandbox as its cwd. An existing
    ``staging_dir`` is removed first, so a re-executed row starts clean.

    Raises:
        PluginStagingError: see ``scan_plugin_skills`` and ``scan_plugin_roots``.
    """
    skills = scan_plugin_skills(plugins)
    roots = scan_plugin_roots(plugins)
    staging_dir = staging_dir.absolute()
    if staging_dir.is_symlink() or staging_dir.is_file():
        staging_dir.unlink()
    elif staging_dir.exists():
        shutil.rmtree(staging_dir)
    skills_dir = staging_dir / _DEFAULT_SKILLS_SUBDIR
    skills_dir.mkdir(parents=True)
    for name, source in sorted(skills.items()):
        link_or_copy(source, skills_dir / name)
    plugins_dir = staging_dir / _PLUGINS_SUBDIR
    plugins_dir.mkdir()
    for name, (root, wrapped) in sorted(roots.items()):
        if wrapped:
            manifest = plugins_dir.joinpath(name, *_MANIFEST_RELPATH)
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"name": name}), encoding="utf-8")
            link_or_copy(root, plugins_dir / name / _DEFAULT_SKILLS_SUBDIR)
        else:
            link_or_copy(root, plugins_dir / name)
    return StagedPlugins(root=staging_dir, skills_offered=tuple(sorted(skills)))


def staged_plugin_dirs(root: Path) -> list[Path]:
    """Each ``<root>/plugins/<plugin>`` a staged root holds, in name order; empty without a ``plugins/``."""
    plugins_dir = root / _PLUGINS_SUBDIR
    return sorted(plugins_dir.iterdir()) if plugins_dir.is_dir() else []
