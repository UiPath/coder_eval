"""The ONE way a lint surface discovers task YAML under a directory tree.

Two consumers pointing at two trees — `TestCE052TemplateTasksLoad` over `templates/` and
`tests/test_task_yaml_discovery.py` over `tasks/` — asking the same question: which files here
are task definitions, and did we see all of them? A second copy would agree on ordinary input
and diverge exactly where either was written for, which is the `leak_detection.py` /
`markdown_tables.py` precedent this module follows (a shared reader under `tests/lint/`, not a
numbered rule).

Both halves of the discovery are load-bearing and both were got wrong independently before this
was shared:

- **Both extensions.** A `.yml` task is invisible to a `*.yaml`-only glob AND to the
  completeness assertion that would otherwise catch it — silently unloaded, which is the exact
  state these checks exist to prevent.
- **PARSED, not regex-matched.** `"task_id":`, `task_id :` and a flow mapping are all valid YAML
  spellings of a task that `line.startswith("task_id:")` misses. Those at least trip a
  completeness assertion loudly, but relying on that is relying on a second check to cover the
  first.

An unparseable file is reported as a task rather than skipped, so `load_task` produces the real
error instead of the file vanishing from the set.

`encoding="utf-8"` throughout, for the CE008/CE011 reason: task YAMLs carry arrows, ≤ and smart
quotes, and the Windows-default cp1252 raises `UnicodeDecodeError` on those bytes.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def all_yaml(root: Path) -> list[Path]:
    """Every YAML file under ``root``, both extensions, sorted."""
    return sorted([*root.rglob("*.yaml"), *root.rglob("*.yml")])


def task_yamls(root: Path) -> list[Path]:
    """Every YAML under ``root`` whose PARSED top level carries a ``task_id`` key."""
    found: list[Path] = []
    for path in all_yaml(root):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            found.append(path)  # unparseable: let `load_task` produce the real error
            continue
        if isinstance(document, dict) and "task_id" in document:
            found.append(path)
    return found
