"""Fixed in-container path for the agent-phase task.yaml a Harbor export bind-mounts in.

Shared by ``packager.py`` (the writer — bakes a criteria-free copy of the task into
``environment/task.yaml`` and bind-mounts it read-only here via
``environment/docker-compose.yaml``, see ``_write_docker_compose_mounts``) and
``agent.py``'s ``CoderEvalAgent`` (the reader — the Harbor agent that runs
``coder-eval execute --format harbor`` against this exact path), so the two
sides can never independently drift on where the file lives. Fixed rather than
discovered: a Harbor agent has no way to ask the export what path it chose, so
the path itself is the contract (tmp/harborframework.md's "Gap 1" resolution —
the agent can always execute a task.yaml at a fixed path; it's up to
docker-compose.yaml to put it there).
"""

from __future__ import annotations


AGENT_TASK_YAML_PATH = "/opt/coder-eval-task/task.yaml"

__all__ = ["AGENT_TASK_YAML_PATH"]
