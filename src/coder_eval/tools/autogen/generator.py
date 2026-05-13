"""Task generation via the Anthropic Messages API."""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import yaml

import coder_eval.models.criteria as _criteria_module  # noqa: CE001 -- runtime introspection of the criteria module (iterates members), not importing a type
from coder_eval.tools.autogen.config import AutogenConfig


logger = logging.getLogger(__name__)

_COVERAGE_INSTRUCTIONS: dict[str, str] = {
    "quick": "Generate exactly 1 task, covering only the primary happy-path use case.",
    "golden": (
        "Generate 2-3 tasks covering the main use cases and at least one variant "
        "(e.g. different framework, secondary feature, or input type)."
    ),
    "comprehensive": (
        "Generate 3-5 tasks covering primary use cases, multiple framework variants, "
        "edge cases, error-handling paths, and integration with other skills where applicable."
    ),
}

_TASK_STRUCTURE = """\
## coder_eval Task Schema

Tasks are YAML files. A task definition has these top-level fields:

```
task_id        (str, required)  -- unique kebab-case id, e.g. "build-langgraph-calculator"
description    (str, required)  -- what is being tested
initial_prompt (str, required)  -- a SHORT, natural human request (see rules below)
tags           (list[str])      -- lowercase kebab-case; may use 'key:value' namespacing (e.g. 'lifecycle:generate')

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: list[str]   # match the skill's allowed-tools frontmatter exactly
  model: str | null

sandbox:
  driver: "tempdir"
  python:
    env_packages: list[str]  # required pip packages

success_criteria:            # list[SuccessCriterion] -- see full definitions below
```

## Rules for initial_prompt

The agent has a plugin loaded that gives it access to skills via the `Skill` tool.
The `initial_prompt` should be written as a **natural human request** -- short, conversational,
and free of implementation details. The agent must invoke the skill to discover those details.

GOOD: "Write a greeting script for me."
GOOD: "Create a farewell script that counts down."
BAD:  "Write greet.py that appends 'GREET: <name> on <date>' to greet.log..."  ← leaks skill internals

You MAY reference the skill name or the high-level task from the skill's `description` frontmatter.
You MUST NOT copy implementation requirements, file names, output formats, or constraints from the skill body.

The `success_criteria` SHOULD test for the skill-specific requirements in full detail -- that is fine
because criteria validate the output, not the input.

## Other best practices

- Always verify syntax: type: run_command, command: "python -m py_compile <file>", timeout: 10
- Check imports with: type: run_command, command: "python -c 'import <module>'", timeout: 30
- Use weight to signal importance (2.0-3.0 for core logic, 0.5 for existence checks)
"""

_FEW_SHOT = """\
## Example 1 -- simple smoke test

```yaml
task_id: fibonacci-cli-smoke
description: Verify the agent produces a working Fibonacci CLI tool.
initial_prompt: Write me a Fibonacci number generator I can run from the command line.
tags: [smoke, golden, pure-python]

agent:
  type: claude-code
  permission_mode: acceptEdits
  allowed_tools: [Read, Write, Bash]

sandbox:
  driver: tempdir
  python: {}

success_criteria:
  - type: file_exists
    path: fibonacci.py
    description: fibonacci.py must exist

  - type: run_command
    command: python -m py_compile fibonacci.py
    timeout: 10
    description: Syntax valid
    weight: 1.0

  - type: run_command
    command: python fibonacci.py 10
    timeout: 10
    description: Script runs with an argument
    weight: 2.0
```

## Example 2 -- LangGraph agent (network required)

```yaml
task_id: uipath-calculator-langgraph
description: Build a calculator agent using UiPath LangGraph StateGraph.
initial_prompt: Build me a calculator agent using LangGraph.
tags: [golden, uipath-langchain, network]

agent:
  type: claude-code
  permission_mode: acceptEdits
  allowed_tools: [Bash]

sandbox:
  driver: tempdir
  python:
    env_packages:
      - uipath-langchain>=0.0.140
      - pydantic>=2.0

success_criteria:
  - type: file_exists
    path: main.py
    description: main.py exists
    weight: 0.5

  - type: file_contains
    path: main.py
    includes: [StateGraph, START, END, BaseModel]
    description: Uses LangGraph and Pydantic
    weight: 2.0

  - type: run_command
    command: python -m py_compile main.py
    timeout: 10
    description: Syntax valid
    weight: 1.0

  - type: run_command
    command: "python -c 'import main'"
    timeout: 30
    description: Imports resolve
    weight: 2.5
```
"""

# Larger reference budget per skill (safe now that context is per-skill, not all-skills-at-once)
_MAX_REF_CHARS = 16000


class _LiteralDumper(yaml.SafeDumper):
    """YAML dumper that uses block literals for multi-line strings."""


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralDumper.add_representer(str, _str_representer)


def _to_kebab(name: str) -> str:
    """Normalize a directory/plugin name to a lowercase kebab-case tag."""
    # Insert hyphens before uppercase runs: "MyPlugin" -> "My-Plugin"
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    # Replace underscores and whitespace with hyphens, then lowercase
    s = re.sub(r"[_\s]+", "-", s).lower()
    # Collapse repeated hyphens and strip leading/trailing hyphens
    return re.sub(r"-{2,}", "-", s).strip("-")


def _get_criteria_source() -> str:
    """Return the full source of the criteria module for use in the LLM prompt."""
    return inspect.getsource(_criteria_module)


def _discover_skills(plugin_dir: Path) -> list[Path]:
    """Return sorted list of skill directories that contain a SKILL.md."""
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        raise ValueError(f"No 'skills/' subdirectory found in {plugin_dir}")
    return sorted(d for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists())


def _collect_skill_content(skill_dir: Path) -> str:
    """Return SKILL.md + all references for a single skill as one string."""
    parts: list[str] = [skill_dir.name, "\n\n", (skill_dir / "SKILL.md").read_text(encoding="utf-8"), "\n"]

    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.glob("*.md")):
            content = ref_file.read_text(encoding="utf-8")
            if len(content) > _MAX_REF_CHARS:
                content = content[:_MAX_REF_CHARS] + "\n\n[...truncated...]\n"
            parts.append(f"\n### Reference: {ref_file.name}\n\n{content}\n")

    return "".join(parts)


def _parse_allowed_tools(skill_dir: Path) -> list[str]:
    """Extract allowed-tools from SKILL.md YAML frontmatter (excludes 'Skill' itself)."""
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    if not skill_md.startswith("---"):
        logger.debug("No YAML frontmatter in %s", skill_dir / "SKILL.md")
        return []
    end = skill_md.find("---", 3)
    if end == -1:
        logger.debug("Unterminated YAML frontmatter in %s", skill_dir / "SKILL.md")
        return []
    try:
        frontmatter = yaml.safe_load(skill_md[3:end])
        raw = frontmatter.get("allowed-tools") if isinstance(frontmatter, dict) else None
        if isinstance(raw, list):
            return [str(t) for t in raw if str(t) != "Skill"]
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip() and t.strip() != "Skill"]
        if raw is None:
            logger.debug("No allowed-tools in frontmatter for %s", skill_dir.name)
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse allowed-tools from %s: %s", skill_dir / "SKILL.md", exc)
    return []


def _collect_existing_task_ids(output_dir: Path) -> list[str]:
    """Return task_ids already present in the output directory."""
    ids: list[str] = []
    for yaml_file in sorted(output_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "task_id" in data:
                ids.append(str(data["task_id"]))
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Failed to read existing task id from %s: %s", yaml_file, exc)
    return ids


def _generate_for_skill(
    skill_dir: Path,
    plugin_name: str,
    plugin_readme: str,
    config: AutogenConfig,
    existing_ids: list[str],
    criteria_source: str,
    client: anthropic.Anthropic,
) -> list[dict[str, Any]]:
    """Run one LLM call to generate tasks for a single skill.

    Returns raw task dicts with plugin_name and skill_name tags injected.
    """
    skill_name = skill_dir.name
    skill_content = _collect_skill_content(skill_dir)
    coverage_instr = _COVERAGE_INSTRUCTIONS[config.coverage]
    agent_defaults = json.dumps(config.agent.model_dump(exclude_none=True, mode="json"), indent=2)

    # Tags the LLM should include (provenance + user-configured)
    required_tags = list(dict.fromkeys([_to_kebab(plugin_name), f"skill-{_to_kebab(skill_name)}", *config.tags]))

    existing_section = (
        "\n## Already-generated tasks (DO NOT use these task_ids)\n\n"
        + "\n".join(f"- {tid}" for tid in existing_ids)
        + "\n"
        if existing_ids
        else ""
    )

    system = f"""\
You are an expert at writing evaluation tasks for AI coding agents using the coder_eval framework.
Your job: generate tasks that evaluate the SINGLE skill definition provided by the user.

{_TASK_STRUCTURE}

## SuccessCriterion -- full Pydantic definitions (use ONLY these types)

```python
{criteria_source}
```

{_FEW_SHOT}

## Plugin context

{plugin_readme}

## Agent config base

Use this as the base. Set `allowed_tools` to match the skill's `allowed-tools` frontmatter.

```json
{agent_defaults}
```

## Required tags

Every generated task MUST include all of these tags (add more as appropriate):
{required_tags}

## Coverage target

{coverage_instr}
{existing_section}"""

    user = f"""\
Generate evaluation tasks for this skill. Call `emit_tasks` when done.

{skill_content}

Reminder: {coverage_instr}
"""

    emit_tool: anthropic.types.ToolParam = {
        "name": "emit_tasks",
        "description": "Output the generated evaluation task definitions for this skill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Complete task definition objects.",
                    "items": {"type": "object"},
                    "minItems": 1,
                }
            },
            "required": ["tasks"],
        },
    }

    response = client.messages.create(
        model=config.generator_model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[emit_tool],
        tool_choice={"type": "any"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "emit_tasks":
            tasks: list[dict[str, Any]] = block.input.get("tasks", [])  # type: ignore[union-attr]

            # Guarantee provenance tags are present regardless of what the LLM generated
            for task in tasks:
                existing = task.get("tags")
                if existing is not None and not isinstance(existing, list):
                    tid = task.get("task_id", "?")
                    logger.warning("LLM returned non-list tags (%s) for %r, dropping", type(existing).__name__, tid)
                existing_list = existing if isinstance(existing, list) else []
                task["tags"] = list(dict.fromkeys(existing_list + required_tags))

            return tasks

    raise RuntimeError(f"LLM did not call emit_tasks for skill '{skill_name}'")


def generate_tasks(
    plugin_dir: Path,
    config: AutogenConfig,
    output_dir: Path | None = None,
    on_skill: Callable[[str, int, int], None] | None = None,
    proxy_port: int | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, Exception]]]:
    """Generate task definition dicts for all skills in a plugin directory.

    Iterates skills one at a time, making one focused LLM call per skill.
    Returns raw dicts -- call validate_tasks() before writing.

    Args:
        plugin_dir: Plugin directory containing skills/
        config: Autogen configuration (model, coverage, agent defaults, tags)
        output_dir: If provided, existing task_ids are passed to the LLM as
                    context so it avoids generating duplicates.
        on_skill: Optional progress callback: (skill_name, current_index, total).
        proxy_port: Optional port for LLM Gateway proxy. If provided, routes API calls through proxy.

    Returns:
        Tuple of (task dicts, per-skill errors). Task dicts are not yet Pydantic-validated.
        Errors is a list of (skill_name, exception) for skills that failed.

    Raises:
        ValueError: If the plugin directory structure is invalid
        RuntimeError: If all skills failed to generate tasks
    """
    plugin_name = plugin_dir.name
    skills = _discover_skills(plugin_dir)
    if not skills:
        raise ValueError(f"No skills with SKILL.md found under {plugin_dir / 'skills'}")

    readme_path = plugin_dir / "README.md"
    plugin_readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

    # Seed with tasks already on disk; grows as we generate to prevent cross-skill ID collisions
    seen_ids = _collect_existing_task_ids(output_dir) if output_dir else []
    criteria_source = _get_criteria_source()

    # Create client: use proxy if port provided, otherwise direct API
    if proxy_port:
        client = anthropic.Anthropic(
            base_url=f"http://127.0.0.1:{proxy_port}",
            api_key="llmgw-proxy",  # Dummy key; proxy handles real auth
        )
    else:
        client = anthropic.Anthropic()

    all_tasks: list[dict[str, Any]] = []
    skill_errors: list[tuple[str, Exception]] = []

    for i, skill_dir in enumerate(skills):
        if on_skill:
            on_skill(skill_dir.name, i + 1, len(skills))

        try:
            tasks = _generate_for_skill(
                skill_dir=skill_dir,
                plugin_name=plugin_name,
                plugin_readme=plugin_readme,
                config=config,
                existing_ids=seen_ids,
                criteria_source=criteria_source,
                client=client,
            )
            # Track newly generated IDs so subsequent skill calls don't reuse them
            seen_ids.extend(t["task_id"] for t in tasks if "task_id" in t)
            all_tasks.extend(tasks)
            logger.info("skill=%s generated=%d", skill_dir.name, len(tasks))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.BadRequestError):
            raise
        except Exception as exc:
            logger.warning("skill=%s failed: %s", skill_dir.name, exc)
            skill_errors.append((skill_dir.name, exc))
            # Don't raise -- continue generating remaining skills

    if skill_errors and not all_tasks:
        errors_str = "; ".join(f"{name}: {exc}" for name, exc in skill_errors)
        raise RuntimeError(f"All skills failed to generate tasks: {errors_str}")

    return all_tasks, skill_errors


def task_to_yaml(task_dict: dict[str, Any]) -> str:
    """Serialize a task dict to a readable YAML string with block literals."""
    return yaml.dump(task_dict, Dumper=_LiteralDumper, default_flow_style=False, sort_keys=False, allow_unicode=True)


def generate_experiment(plugin_dir: Path, config: AutogenConfig) -> dict[str, Any]:
    """Generate an experiment definition comparing with-plugin vs without-plugin.

    The with-plugin variant loads the plugin and includes the Skill tool.
    The without-plugin variant omits both, testing the agent without skill guidance.

    Args:
        plugin_dir: Plugin directory (same path used for generate_tasks).
        config: Autogen configuration (used for base agent settings).

    Returns:
        Experiment definition dict ready for task_to_yaml() serialisation.
    """
    plugin_name = plugin_dir.name
    skills = _discover_skills(plugin_dir)

    # Collect union of tools across all skills (excluding Skill itself)
    base_tools: list[str] = []
    for skill_dir in skills:
        for tool in _parse_allowed_tools(skill_dir):
            if tool not in base_tools:
                base_tools.append(tool)

    tools_with_skill = ["Skill", *base_tools]
    tools_without_skill = base_tools

    # Base agent: common settings shared by both variants (drop list/None defaults)
    base_agent = {
        k: v
        for k, v in config.agent.model_dump(exclude_none=True, mode="json").items()
        if v != [] and k not in ("allowed_tools", "plugins")
    }

    return {
        "experiment_id": f"{plugin_name}-plugin-comparison",
        "description": f"Compare agent with vs without the {plugin_name} plugin loaded",
        "defaults": {"agent": base_agent},
        "variants": [
            {
                "variant_id": "with-plugin",
                "agent": {
                    "allowed_tools": tools_with_skill,
                    "plugins": [{"type": "local", "path": str(plugin_dir.resolve())}],
                },
            },
            {
                "variant_id": "without-plugin",
                "agent": {
                    "allowed_tools": tools_without_skill,
                    "plugins": [],
                },
            },
        ],
    }
