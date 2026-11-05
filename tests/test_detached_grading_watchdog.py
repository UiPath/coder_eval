"""A real grading-attempt watchdog cannot revise an already completed agent run."""

import asyncio

import pytest

from coder_eval.errors import GradingError
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import FinalStatus, PreservationMode, RunLimits, TaskDefinition
from coder_eval.orchestration import regrade
from coder_eval.orchestrator import Orchestrator
from coder_eval.sandbox import Sandbox
from tests.test_grading_container_failure import _prepare_measured_prior


@pytest.mark.parametrize(
    "status",
    [FinalStatus.SUCCESS, FinalStatus.ERROR, FinalStatus.TOKEN_BUDGET_EXCEEDED, FinalStatus.COST_BUDGET_EXCEEDED],
)
async def test_actual_detached_watchdog_is_a_failed_grade(tmp_path, monkeypatch, status):
    prior, row, _, workspace = _prepare_measured_prior(tmp_path, status)
    original_files = {name: (row / name).read_bytes() for name in ("task.json", "task.html", "judge-0.yaml")}
    task = TaskDefinition(
        task_id="t",
        description="Short deterministic grading watchdog",
        agent={"type": "none"},
        sandbox={"driver": "tempdir", "python": None, "node": None},
        success_criteria=[{"type": "file_exists", "path": ".", "description": "Workspace exists"}],
        run_limits=RunLimits.model_construct(task_timeout=0.05),
    )
    sandbox = Sandbox(task.sandbox, task.task_id)
    await asyncio.to_thread(sandbox.adopt, workspace)
    entered = []

    async def slow_checker(self, *args, **kwargs):
        entered.append(True)
        await asyncio.sleep(10)

    monkeypatch.setattr(SuccessChecker, "check_all_async", slow_checker)
    with pytest.raises(GradingError) as failure:
        await Orchestrator(
            task=task,
            prior_result=prior,
            run_dir=row,
            sandbox=sandbox,
            variant_id="v",
            preservation_mode=PreservationMode.NONE,
        ).run()
    assert entered == [True], "The real checker must run before the actual watchdog fires"
    assert "timed out" in str(failure.value).lower()
    assert {name: (row / name).read_bytes() for name in original_files} == original_files
    selected = regrade.write_grading_incident(row, prior, failure.value)
    assert selected.model_dump(exclude={"error_history"}) == prior.model_dump(exclude={"error_history"})
    assert len(selected.error_history) == len(prior.error_history) + 1
