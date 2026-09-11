"""Fixed in-container path for the agent-phase task.yaml a Harbor export bakes into its image.

Shared by ``packager.py`` (the writer — bakes a criteria-free copy of the task
into ``environment/task.yaml`` and ``COPY``s it here) and ``agent.py``'s
``CoderEvalAgent`` (the reader — the Harbor agent that runs
``coder-eval execute --format harbor`` against this exact path), so the two
sides can never independently drift on where the file lives. Fixed rather than
discovered: a Harbor agent has no way to ask the export what path it chose, so
the path itself is the contract (tmp/harborframework.md's "Gap 1" resolution —
the agent can always execute a task.yaml at a fixed path; it's up to the
Dockerfile to put it there).
"""

from __future__ import annotations


AGENT_TASK_YAML_PATH = "/opt/coder-eval-task/task.yaml"

AGENT_TASK_TEMPLATES_DIR = "/opt/coder-eval-task/templates"
"""Sibling of :data:`AGENT_TASK_YAML_PATH` for ``TemplateDirSource`` copies. A
``TemplateDirSource.path`` is resolved to an absolute HOST path at task-load
time (``task_loader.resolve_template_source_paths``) — that path does not
exist inside the container, so ``packager.py`` copies each source's directory
under here (``environment/templates/<n>-<name>/`` on the export side, ``COPY``
'd into the image at build time) and rewrites ``environment/task.yaml``'s
``template_sources[].path`` to point at the in-container copy instead.
"""

__all__ = ["AGENT_TASK_TEMPLATES_DIR", "AGENT_TASK_YAML_PATH"]
