"""``Orchestrator._seed_from_prior_result`` — the detached grade's fidelity contract.

A detached grade (``evaluate <run_dir>`` / ``run --resume``) recomputes the
verdict but must not recompute the RUN. Every field it carries over is a fact the
agent phase established and this pass cannot re-derive; every field it does not
carry is either recomputed from the trajectory or deliberately dropped.

The end-to-end tests in ``test_execute_evaluate_loop.py`` exercise this through a
fixture where most of these fields hold their defaults, so deleting a carry line
leaves them green. These tests set every field to a distinctive value instead, and
the partition below fails closed when a new field is added to ``EvaluationResult``
without a decision about it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.models import (
    AgentKind,
    CommandExecutedCriterion,
    CriterionResult,
    EarlyStopInfo,
    EarlyStopReason,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    PostRunResult,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator


# Every field on EvaluationResult, partitioned by what a detached grade does with
# it. No catch-all: a new field fails the parity test below until it is listed,
# which is the same fail-closed shape as _STATUS_CATEGORIES in models/enums.py.
CARRIED = {
    "started_at",
    "iterations",
    "iteration_count",
    "early_stop",
    "max_turns_exhausted",
    "error_message",
    "error_details",
    "error_log_tail",
    "sdk_options",
    "agent_config",
    "expected_commands",
    "simulation",
    "pre_run_results",
    "post_run_results",
    "sandbox_path",
    "environment_info",
}

# Recomputed by this pass — carrying them would defeat the point.
RECOMPUTED = {
    # The verdict itself: exactly what the grading pass produces.
    "final_status",
    "weighted_score",
    "success_criteria_results",
    "post_failure_criteria_results",
    # Derived from `iterations`, which IS carried — so seeding the trajectory
    # reproduces these exactly without copying them.
    "model_used",
    "command_stats",
    "total_token_usage",
    "total_assistant_turns",
    "actual_commands",
    "commands_efficiency",
    # Identity, supplied by the caller from the task being graded.
    "task_id",
    "task_description",
    "variant_id",
    "agent_type",
    "task_config",
    # Timing: `duration_seconds` is restored from the prior result in
    # _finalize_result (after its own timing write), and `completed_at` marks
    # when the row reached its final state, which the grade genuinely changes.
    "duration_seconds",
    "completed_at",
}


def _prior() -> EvaluationResult:
    """A prior result with a distinctive value in every carried field."""
    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2020, 1, 1, 0, 0, 0),
        final_status=FinalStatus.NOT_GRADED,
        iteration_count=7,
        max_turns_exhausted=True,
        error_message="prior message",
        error_details={"where": "prior"},
        error_log_tail="prior tail",
        sdk_options={"opt": "prior"},
        agent_config=parse_agent_config(type=AgentKind.CLAUDE_CODE, model="prior-model"),
        expected_commands=11,
        pre_run_results=[PostRunResult(command="prior-pre", exit_code=0)],
        post_run_results=[PostRunResult(command="prior-post", exit_code=0)],
        sandbox_path="/prior/workspace",
        environment_info={"installed_tools": "prior"},
        early_stop=EarlyStopInfo(
            reason=EarlyStopReason.CRITERION_FAILED,
            deciding_criterion_type="skill_triggered",
            deciding_criterion_description="the armed criterion",
            sdk_turn_index=0,
            tool_call_index=1,
            elapsed_seconds=1.0,
            gate_threshold=1.0,
        ),
    )


def _seeded(tmp_path: Path) -> tuple[Orchestrator, EvaluationResult]:
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
    )
    prior = _prior()
    orch = Orchestrator(task=task, run_dir=tmp_path, variant_id="v", prior_result=prior)
    orch.result = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2030, 1, 1, 0, 0, 0),
        final_status=FinalStatus.FAILURE,
        iteration_count=0,
        environment_info={"installed_tools": "grader"},
    )
    orch._seed_from_prior_result()
    assert orch.result is not None
    return orch, prior


def test_field_partition_covers_every_evaluation_result_field() -> None:
    """The sensor. A field added to EvaluationResult must be classified as
    carried or recomputed before this passes — otherwise it silently defaults on
    every detached grade, which is exactly how `agent_config` was being lost."""
    classified = CARRIED | RECOMPUTED
    fields = set(EvaluationResult.model_fields)
    assert not fields - classified, (
        f"Unclassified EvaluationResult field(s): {sorted(fields - classified)}. Decide whether "
        "_seed_from_prior_result must carry them, then add them to CARRIED or RECOMPUTED."
    )
    assert not classified - fields, f"Stale entries: {sorted(classified - fields)}"


@pytest.mark.parametrize("field", sorted(CARRIED - {"environment_info"}))
def test_every_carried_field_reaches_the_regrade(field: str, tmp_path: Path) -> None:
    orch, prior = _seeded(tmp_path)
    assert orch.result is not None
    assert getattr(orch.result, field) == getattr(prior, field), (
        f"_seed_from_prior_result dropped `{field}`; the graded row would report its default "
        "instead of what the run actually did."
    )


def test_early_stop_is_carried_because_it_selects_the_gate(tmp_path: Path) -> None:
    """Called out separately because it is the one carried field that changes the
    VERDICT: gate selection is FIRED-ONLY, so a dropped early_stop re-grades a
    truncated trajectory under the full-run strict-AND gate."""
    orch, _ = _seeded(tmp_path)
    assert orch.result is not None and orch.result.early_stop is not None
    assert orch.result.early_stop.reason is EarlyStopReason.CRITERION_FAILED


def test_grader_environment_is_kept_beside_the_run_s_not_over_it(tmp_path: Path) -> None:
    orch, _ = _seeded(tmp_path)
    assert orch.result is not None
    # The run's own capture wins: a report showing the grader's tool versions as
    # the run's is worse than one showing neither.
    assert orch.result.environment_info["installed_tools"] == "prior"
    assert orch.result.environment_info["graded_by"] == {"installed_tools": "grader"}


def test_the_evaluate_only_path_selects_the_same_gate_as_the_agent_path(tmp_path: Path) -> None:
    """C1: gate selection is FIRED-ONLY, and a detached grade reaches the verdict
    through the evaluate-only branch. That branch used to call
    ``all_criteria_passed`` unconditionally, so re-grading an early-stopped run
    applied the full-run strict-AND gate to a truncated trajectory and could flip
    SUCCESS into FAILURE. Both paths must go through ``_select_gate``."""
    import inspect

    source = inspect.getsource(Orchestrator._evaluation_loop)
    assert source.count("_select_gate()") == 2, (
        "Both the evaluate-only branch and the agent branch must select the gate through "
        "_select_gate(); a second hand-written selection is how the seeded early_stop "
        "came to be carried but never read."
    )
    assert "all_criteria_passed" not in source, "gate selection belongs in _select_gate, not inline"

    # A truncated run: the ARMED criterion passed, the unarmed one never had the
    # chance to. The armed gate says SUCCESS; strict-AND says FAILURE. That
    # difference IS the flipped verdict, and it is decided purely by early_stop.
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        success_criteria=[
            CommandExecutedCriterion(
                description="armed",
                tool_name="Read",
                require_success=True,
                stop_early={"on_pass": "stop"},  # type: ignore[arg-type]
            ),
            FileExistsCriterion(path="x.txt", description="unarmed"),
        ],
    )
    orch = Orchestrator(task=task, run_dir=tmp_path, variant_id="v", prior_result=_prior())
    orch.result = _prior()
    orch.result.success_criteria_results = [
        CriterionResult(criterion_type="command_executed", description="armed", score=1.0),
        CriterionResult(criterion_type="file_exists", description="unarmed", score=0.0),
    ]

    assert orch._select_gate() is True, "an early-stopped run gates on the armed subset"
    orch.result.early_stop = None
    assert orch._select_gate() is False, "a run that completed naturally gates strict-AND"


def test_grading_cannot_overturn_an_execution_fact() -> None:
    """H5: a detached grade re-runs the criteria over a trajectory it did not
    produce. It may move NOT_GRADED to a verdict; it must not turn a run that
    timed out or crashed into a pass."""
    assert not FinalStatus.NOT_GRADED.is_execution_fact
    assert not FinalStatus.SUCCESS.is_execution_fact
    assert not FinalStatus.FAILURE.is_execution_fact
    for status in (
        FinalStatus.ERROR,
        FinalStatus.TIMEOUT,
        FinalStatus.BUILD_FAILED,
        FinalStatus.MAX_TURNS_EXHAUSTED,
        FinalStatus.TOKEN_BUDGET_EXCEEDED,
        FinalStatus.COST_BUDGET_EXCEEDED,
    ):
        assert status.is_execution_fact, f"{status} describes the run, so grading must preserve it"


def test_seeding_is_a_no_op_without_a_prior_result(tmp_path: Path) -> None:
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
    )
    orch = Orchestrator(task=task, run_dir=tmp_path, variant_id="v")
    orch.result = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2030, 1, 1),
        final_status=FinalStatus.FAILURE,
        iteration_count=0,
    )
    orch._seed_from_prior_result()
    assert orch.result.started_at == datetime(2030, 1, 1)
    assert orch.result.iterations == []
