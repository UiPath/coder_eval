"""Phase 3: the local Pi smoke task loads, resolves to PiAgentConfig, validates.

The task is intentionally excluded from the CI E2E bucket (no ``smoke-pass``
tag — the CI runners have no ``pi`` CLI and no OpenRouter credentials); it is a
local smoke. This test only proves the YAML parses and resolves, offline.
"""

from __future__ import annotations

from pathlib import Path

from coder_eval.models import PiAgentConfig
from coder_eval.orchestration.task_loader import load_task


_TASK = Path("tasks/pi_smoke_test.yaml")


def test_task_loads_and_resolves_to_pi_agent_config():
    task, _raw = load_task(_TASK)
    assert task.task_id == "pi_smoke_test"
    assert isinstance(task.agent, PiAgentConfig)
    assert task.agent.type == "pi"
    assert task.agent.model == "openrouter/moonshotai/kimi-k3"


def test_criteria_validate():
    task, _raw = load_task(_TASK)
    types = [c.type for c in task.success_criteria]
    assert types == ["file_exists", "file_contains", "run_command"]


def test_not_in_ci_e2e_bucket():
    """No `smoke-pass` tag: the task must not route into the CI E2E bucket, which
    has no `pi` CLI or OpenRouter credentials."""
    task, _raw = load_task(_TASK)
    assert "pi" in task.tags
    assert "smoke-pass" not in task.tags
