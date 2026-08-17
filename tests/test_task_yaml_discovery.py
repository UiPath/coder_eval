"""Every task YAML under ``tasks/`` loads through the real ``load_task``.

CE052's shape, one directory over — and literally its reader: both share
``tests/lint/task_yaml_discovery.py`` rather than each spelling "which files here are tasks"
its own way. CE052 covers ``templates/``; nothing covered ``tasks/``, and the gap was not
theoretical — ``tasks/smoke_agent_judge.yaml`` had **no load coverage at all**
(``test_yaml_migration.py::test_migrated_smoke_tasks_load`` names two files and explicitly
disclaims the rest; ``grep -rn smoke_agent_judge tests/`` found only a docstring mention). It is
the one in-tree consumer of the ``agent_judge`` judge-``agent:`` block, whose schema was narrowed
from the four-way ``AgentConfig`` union to ``ClaudeCodeAgentConfig`` — a task-YAML schema change
landing on a file no test loaded.

The set is DISCOVERED, never enumerated, which is the property that makes it cover the next one
too, and there is deliberately **no exclusion list**: measured when this landed, every
discovered task YAML loads cleanly, and a future one that does not is this test doing its job.

Why loading matters more than it sounds: ``resolve_all_tasks`` isolates a malformed task into
``skipped_tasks`` rather than failing. The damage from a broken task YAML is therefore not a red
build but a GREEN one that silently ran fewer suites than the tree suggests.

This file lives apart from ``test_yaml_migration.py`` on purpose — that one is a dated one-shot
whose own docstring schedules it for deletion, and this guard must outlive it.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from coder_eval.orchestration.task_loader import load_task
from tests.lint.task_yaml_discovery import all_yaml, task_yamls


ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"

_TASK_YAMLS = task_yamls(TASKS)


def test_the_discovery_set_is_not_empty() -> None:
    """Anti-vacuity: a moved or renamed directory must report a GAP, not pass silently.

    The CE044/CE045 lesson — a derived scan set that quietly becomes empty is byte-identical to
    a clean tree, and the parametrized test below would collect zero cases and go green.
    """
    assert TASKS.is_dir(), f"{TASKS} moved — this file would be checking nothing"
    assert _TASK_YAMLS, f"no task YAML discovered under {TASKS} — the guard is vacuous"


def test_every_yaml_under_tasks_is_a_task() -> None:
    """The completeness half, and it is falsifiable rather than true by construction.

    ``tasks/`` currently holds tasks and NOTHING else, so the discovery set must equal the full
    YAML set. That makes the assertion do real work in both directions: a task YAML the content
    filter stops recognising drops out of the parametrization below and is caught HERE rather
    than silently unloaded, and a genuinely non-task file added later fails loudly and forces
    the author to name it (CE052 carries exactly such a named-exception set for ``templates/``,
    which holds an experiment file and a GitHub workflow).
    """
    unrecognized = sorted(p.relative_to(ROOT).as_posix() for p in set(all_yaml(TASKS)) - set(_TASK_YAMLS))
    assert not unrecognized, (
        "YAML under tasks/ not recognised as a task — either it is a genuine non-task (name it "
        f"here, as CE052 does for templates/) or discovery has broken: {unrecognized}"
    )


@pytest.mark.parametrize("path", _TASK_YAMLS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_task_yaml_loads(path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        task, _ = load_task(path)
    assert task.task_id, f"{path}: loaded but carries no task_id"
