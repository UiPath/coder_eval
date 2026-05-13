"""Unit tests for CE007 (NoTopLevelRunLimitsAccess).

Targets positive and negative cases listed in c/2026-05-12-unify-run-limits.md
Phase 7. Uses the same harness as the integration parametrize in
test_custom_lint.py, but on synthetic in-memory source instead of the whole
src/ tree, so individual patterns can be exercised in isolation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.lint.rules.no_top_level_run_limits_access import NoTopLevelRunLimitsAccess


def _violations(source: str, filepath: str = "src/coder_eval/orchestrator.py") -> list:
    tree = ast.parse(source)
    return NoTopLevelRunLimitsAccess(filepath).check(tree)


def test_flags_task_max_turns() -> None:
    assert _violations("x = task.max_turns")


def test_flags_self_task_max_turns() -> None:
    assert _violations("class C:\n    def m(self): x = self.task.max_turns")


def test_flags_variant_task_timeout() -> None:
    assert _violations("x = variant.task_timeout")


def test_flags_resolved_task_turn_timeout() -> None:
    assert _violations("x = resolved_task.turn_timeout")


def test_does_not_flag_simulation_max_turns() -> None:
    assert not _violations("x = sim_config.max_turns")


def test_does_not_flag_task_simulation_max_turns() -> None:
    assert not _violations("x = self.task.simulation.max_turns")


def test_does_not_flag_criterion_max_turns_in_agent_judge_file() -> None:
    src = "x = criterion.max_turns"
    assert not _violations(src, filepath="src/coder_eval/criteria/agent_judge.py")


def test_does_not_flag_config_max_turns_in_experiment_resolver() -> None:
    src = "x = config.max_turns"
    assert not _violations(src, filepath="src/coder_eval/orchestration/experiment.py")


def test_does_not_flag_run_limits_max_turns_definition_in_limits_file() -> None:
    src = "class C:\n    max_turns: int | None = None"
    assert not _violations(src, filepath="src/coder_eval/models/limits.py")


def test_does_not_flag_run_limits_max_turns_anywhere() -> None:
    assert not _violations("x = run_limits.max_turns")
    assert not _violations("x = self.task.run_limits.max_turns")
    assert not _violations("x = rl.task_timeout")


def test_does_not_flag_turn_max_turns_exhausted() -> None:
    """`max_turns_exhausted` is a distinct TurnRecord field — must not collide."""
    assert not _violations("x = turn.max_turns_exhausted")


def test_synthetic_regression_in_orchestrator_fires() -> None:
    """Revert one orchestrator read-site rewrite, expect CE007 to fire."""
    src = "x = self.task.task_timeout"
    vs = _violations(src, filepath="src/coder_eval/orchestrator.py")
    assert len(vs) == 1
    assert "task_timeout" in vs[0].message


@pytest.mark.lint
def test_repo_clean(tmp_path: Path) -> None:
    """Integration check: src/ tree has zero CE007 violations post-migration."""
    from tests.lint.runner import check_paths

    src_root = Path(__file__).parent.parent / "src"
    violations = check_paths([src_root], rules=[NoTopLevelRunLimitsAccess])
    assert not violations, "\n".join(str(v) for v in violations)
