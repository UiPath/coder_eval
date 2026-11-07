"""Actual regrade orchestration with a source-signature Docker transport double."""

import json
from datetime import datetime

import pytest

from coder_eval.errors import GradingError
from coder_eval.models import CriterionResult, FinalStatus, GradingErrorInfo, TokenUsage, TurnRecord
from coder_eval.orchestration import regrade
from tests.test_regrade import _docker_task, _result, _RunnerDouble


def _prepare_measured_prior(tmp_path, status):
    prior = _result(
        final_status=status,
        weighted_score=1.0,
        error_phase=None if status is FinalStatus.SUCCESS else "execution",
        error_message="Original execution cause" if status is not FinalStatus.SUCCESS else None,
        iterations=[
            TurnRecord(
                iteration=1,
                user_input="Original question",
                agent_output="Original answer",
                token_usage=TokenUsage(uncached_input_tokens=2, output_tokens=1, total_cost_usd=0.2),
            )
        ],
        success_criteria_results=[
            CriterionResult(criterion_type="file_exists", description="Original grade", score=1.0)
        ],
    )
    row = tmp_path / "row"
    row.mkdir()
    (row / "task.json").write_text(prior.model_dump_json(indent=2))
    (row / "judge-0.yaml").write_text("original judge artifact")
    (row / "task.html").write_text("original report")
    task_file = tmp_path / "task.yaml"
    task_file.write_text("task_id: t\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return prior, row, task_file, workspace


@pytest.mark.parametrize(
    "status",
    [FinalStatus.SUCCESS, FinalStatus.ERROR, FinalStatus.TOKEN_BUDGET_EXCEEDED, FinalStatus.COST_BUDGET_EXCEEDED],
)
async def test_failed_container_grade_never_folds_inherited_execution_status(tmp_path, monkeypatch, status):
    from coder_eval.isolation import docker_runner

    prior, row, task_file, workspace = _prepare_measured_prior(tmp_path, status)
    before = {p.name: p.read_bytes() for p in row.iterdir()}

    class FailedRunner(_RunnerDouble):
        async def run(self):
            error = GradingError(RuntimeError("Intentional container grading failure"), component="fixture.grading")
            transport = regrade.container_grading_failure_record(prior, error, {})
            self.rt.run_dir.mkdir(parents=True, exist_ok=True)
            (self.rt.run_dir / "task.json").write_text(transport.model_dump_json(indent=2))
            (self.rt.run_dir / "judge-0.yaml").write_text("failed attempt artifact")
            return transport

    monkeypatch.setattr(docker_runner, "DockerRunner", FailedRunner)
    with pytest.raises(GradingError) as failure:
        await regrade.regrade_in_place(
            task=_docker_task(),
            prior=prior,
            workspace=workspace,
            run_dir=row,
            task_file=task_file,
            source_yaml="",
            variant_id="v",
        )
    assert {p.name: p.read_bytes() for p in row.iterdir()} == before
    assert regrade.CONTAINER_GRADING_FAILURE_KEY not in failure.value.incident.details
    writes = []
    original = regrade.write_text_atomic

    def write(path_arg, text_arg):
        if path_arg == row / "task.json":
            writes.append(json.loads(text_arg))
        return original(path_arg, text_arg)

    monkeypatch.setattr(regrade, "write_text_atomic", write)
    selected = regrade.write_grading_incident(row, prior, failure.value)
    assert len(writes) == 1
    assert selected.model_dump(exclude={"error_history"}) == prior.model_dump(exclude={"error_history"})
    assert len(selected.error_history) == len(prior.error_history) + 1
    assert regrade.CONTAINER_GRADING_FAILURE_KEY not in selected.model_dump_json()
    assert (row / "judge-0.yaml").read_bytes() == before["judge-0.yaml"]
    assert (row / "task.html").read_bytes() == before["task.html"]


async def test_successful_container_repair_is_not_a_failure_just_because_history_grows(tmp_path, monkeypatch):
    from coder_eval.isolation import docker_runner

    prior, row, task_file, workspace = _prepare_measured_prior(tmp_path, FinalStatus.ERROR)
    prior.error_phase = "grading"
    prior.error_message = "Old grading failure"
    graded = prior.model_copy(deep=True)
    graded.final_status = FinalStatus.SUCCESS
    graded.error_phase = None
    graded.error_message = None
    graded.error_history.append(
        GradingErrorInfo(
            message="Old grading failure", details={"component": "grading"}, recorded_at=datetime(2020, 1, 1)
        )
    )

    class SuccessfulRunner(_RunnerDouble):
        async def run(self):
            self.rt.run_dir.mkdir(parents=True, exist_ok=True)
            (self.rt.run_dir / "task.json").write_text(graded.model_dump_json(indent=2))
            return graded

    monkeypatch.setattr(docker_runner, "DockerRunner", SuccessfulRunner)
    result = await regrade.regrade_in_place(
        task=_docker_task(),
        prior=prior,
        workspace=workspace,
        run_dir=row,
        task_file=task_file,
        source_yaml="",
        variant_id="v",
    )
    assert result.final_status is FinalStatus.SUCCESS
    assert json.loads((row / "task.json").read_text())["final_status"] == "SUCCESS"
    assert len(result.error_history) == 1
