"""Tests for `coder-eval run --resume`.

Covers the three pieces of the feature:
  1. partition_for_resume() — splits resolved tasks into already-finalized
     (reloaded from task.json) vs still-to-run.
  2. run_batch(prior_results=...) — folds reloaded results into run.json so the
     summary describes the whole run, not just the resumed batch.
  3. run-config fingerprint — stamped on every run; a resume whose config
     differs (e.g. a different --model) prints an informational warning (resume
     still proceeds) so the resulting mixed-config run.json isn't silent.
"""

import json
import re
from datetime import datetime

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.cli.run_command import _run_with_experiment
from coder_eval.models import AgentKind, EvaluationResult, FinalStatus, PreservationMode, ResolvedTask, TaskDefinition
from coder_eval.orchestration.batch import (
    compute_run_fingerprint,
    fingerprint_diff,
    partition_for_resume,
    read_run_fingerprint,
    run_batch,
    write_run_fingerprint,
)
from coder_eval.orchestration.config import BatchRunConfig


runner = CliRunner()


def _task(task_id: str) -> TaskDefinition:
    return TaskDefinition(
        task_id=task_id,
        description=f"desc {task_id}",
        initial_prompt="prompt",
        agent={"type": "claude-code"},
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "x.txt", "description": "x"}],
        tags=["smoke", "golden"],
    )


def _resolved(run_root, task_id: str) -> ResolvedTask:
    return ResolvedTask(
        task=_task(task_id),
        task_file=run_root / f"{task_id}.yaml",
        run_dir=run_root / "default" / task_id / "00",
        variant_id="default",
        original_task_id=task_id,
    )


def _write_task_json(rt: ResolvedTask, status: FinalStatus) -> None:
    """Write a finalized task.json into rt.run_dir, like the orchestrator does."""
    rt.run_dir.mkdir(parents=True, exist_ok=True)
    result = EvaluationResult(
        task_id=rt.task.task_id,
        task_description=rt.task.description,
        variant_id=rt.variant_id,
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=status,
        weighted_score=1.0 if status == FinalStatus.SUCCESS else 0.0,
        duration_seconds=12.5,
        iteration_count=1,
        environment_info={},
    )
    (rt.run_dir / "task.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")


def test_partition_splits_finalized_from_pending(tmp_path):
    """Finalized task.json → prior; missing or status-less → to_run."""
    done = _resolved(tmp_path, "done_task")
    pending = _resolved(tmp_path, "pending_task")  # no task.json at all
    partial = _resolved(tmp_path, "partial_task")  # task.json exists but no final_status

    _write_task_json(done, FinalStatus.SUCCESS)
    partial.run_dir.mkdir(parents=True, exist_ok=True)
    (partial.run_dir / "task.json").write_text(json.dumps({"task_id": "partial_task"}), encoding="utf-8")

    to_run, prior_results, prior_resolved = partition_for_resume([done, pending, partial])

    assert {rt.task.task_id for rt in to_run} == {"pending_task", "partial_task"}
    assert [tr.task_id for tr in prior_results] == ["done_task"]
    assert [rt.task.task_id for rt in prior_resolved] == ["done_task"]
    # Reloaded result carries the on-disk status + duration.
    assert prior_results[0].result.final_status == FinalStatus.SUCCESS
    assert prior_results[0].duration == 12.5


def test_partition_no_run_dir_yields_all_pending(tmp_path):
    """--resume on a fresh dir degrades to a normal run (everything to_run)."""
    tasks = [_resolved(tmp_path, f"t{i}") for i in range(3)]
    to_run, prior_results, prior_resolved = partition_for_resume(tasks)
    assert len(to_run) == 3
    assert prior_results == []
    assert prior_resolved == []


@pytest.mark.asyncio
async def test_run_batch_folds_prior_into_run_json(tmp_path):
    """run_batch with no new tasks but prior_results writes a consistent run.json."""
    run_dir = tmp_path / "run"
    config = BatchRunConfig(run_dir=run_dir, max_parallel=1, preservation_mode=PreservationMode.NONE)

    p_success = _resolved(tmp_path, "prior_ok")
    p_fail = _resolved(tmp_path, "prior_bad")
    _write_task_json(p_success, FinalStatus.SUCCESS)
    _write_task_json(p_fail, FinalStatus.FAILURE)

    _, prior_results, prior_resolved = partition_for_resume([p_success, p_fail])
    assert len(prior_results) == 2

    # Nothing left to run — exercises the merge + run.json write in isolation.
    summary, results = await run_batch(
        resolved_tasks=[],
        config=config,
        prior_results=prior_results,
        prior_resolved=prior_resolved,
    )

    # Summary counts cover the prior results, not the (empty) batch.
    assert summary.tasks_run == 2
    assert summary.tasks_succeeded == 1
    assert summary.tasks_failed == 1
    assert len(results) == 2

    # run.json on disk is consistent with the summary and carries per-task tags.
    run_json = json.loads((run_dir / "run.json").read_text())
    assert run_json["tasks_run"] == 2
    assert run_json["tasks_succeeded"] == 1
    assert run_json["tasks_failed"] == 1
    by_id = {t["task_id"]: t for t in run_json["task_results"]}
    assert set(by_id) == {"prior_ok", "prior_bad"}
    assert "smoke" in by_id["prior_ok"]["tags"]


def test_fingerprint_roundtrip_and_no_diff_when_unchanged(tmp_path):
    """Stamp round-trips through disk and matches an identical recompute."""
    config = BatchRunConfig(run_dir=tmp_path, overrides={"agent.model": "sonnet", "run_limits.max_turns": 30})
    fp = compute_run_fingerprint(config, "exp1", "bedrock", "model-x")
    write_run_fingerprint(tmp_path, fp)

    prior = read_run_fingerprint(tmp_path)
    assert prior == fp
    same = compute_run_fingerprint(config, "exp1", "bedrock", "model-x")
    assert fingerprint_diff(prior, same) == {}


def test_fingerprint_flags_result_affecting_changes(tmp_path):
    """A changed model or backend shows up in the diff (the resume guard's signal)."""
    base = BatchRunConfig(run_dir=tmp_path, overrides={"agent.model": "sonnet"})
    write_run_fingerprint(tmp_path, compute_run_fingerprint(base, "exp1", "bedrock", "m"))
    prior = read_run_fingerprint(tmp_path)

    changed = BatchRunConfig(run_dir=tmp_path, overrides={"agent.model": "opus"})
    diffs = fingerprint_diff(prior, compute_run_fingerprint(changed, "exp1", "direct", "m"))
    assert diffs["overrides"] == ({"agent.model": "sonnet"}, {"agent.model": "opus"})
    assert diffs["backend"] == ("bedrock", "direct")


def test_fingerprint_read_missing_returns_none(tmp_path):
    """A run dir with no stamp (predates the feature) is tolerated, not an error."""
    assert read_run_fingerprint(tmp_path / "no-such-dir") is None


def test_fingerprint_diff_ignores_keys_absent_in_prior():
    """Fields added in a later version must not false-flag a resume of an older run."""
    prior = {"agent_model": "sonnet"}
    current = {"agent_model": "sonnet", "new_field_added_later": "x"}
    assert fingerprint_diff(prior, current) == {}


def test_fingerprint_covers_whole_config(tmp_path):
    """Dumping the whole config means any override (incl. tools/SDK) surfaces as a diff."""
    base = BatchRunConfig(
        run_dir=tmp_path, overrides={"agent.allowed_tools": ["Bash"], "agent.sdk_options": {"k": "v1"}}
    )
    write_run_fingerprint(tmp_path, compute_run_fingerprint(base, "exp1", "bedrock", "m"))
    prior = read_run_fingerprint(tmp_path)

    changed = BatchRunConfig(
        run_dir=tmp_path, overrides={"agent.allowed_tools": ["Bash", "Read"], "agent.sdk_options": {"k": "v2"}}
    )
    diffs = fingerprint_diff(prior, compute_run_fingerprint(changed, "exp1", "bedrock", "m"))
    assert diffs["overrides"] == (
        {"agent.allowed_tools": ["Bash"], "agent.sdk_options": {"k": "v1"}},
        {"agent.allowed_tools": ["Bash", "Read"], "agent.sdk_options": {"k": "v2"}},
    )


def test_read_fingerprint_non_dict_returns_none(tmp_path):
    """A stamp that parses to a non-object (external corruption) is tolerated like a missing one."""
    from coder_eval.orchestration.batch import RESUME_FINGERPRINT_FILE

    (tmp_path / RESUME_FINGERPRINT_FILE).write_text("42", encoding="utf-8")
    assert read_run_fingerprint(tmp_path) is None
    (tmp_path / RESUME_FINGERPRINT_FILE).write_text('"a string"', encoding="utf-8")
    assert read_run_fingerprint(tmp_path) is None


def test_resume_requires_run_dir():
    """--resume without --run-dir is refused at the CLI layer (auto dirs are always fresh)."""
    result = runner.invoke(app, ["run", "no-such-task.yaml", "--resume"])
    assert result.exit_code != 0
    out = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "run-dir" in out.lower()


@pytest.mark.asyncio
async def test_resume_warns_on_config_drift_but_proceeds(tmp_path):
    """A resume whose config drifts from the stamp warns and proceeds — it does NOT refuse.

    Stamps a minimal prior fingerprint ({overrides: {agent.model: sonnet}}); fingerprint_diff
    only compares keys present in both, so the freshly-computed opus config drifts on exactly
    overrides. With no tasks left to run, _run_with_experiment completes a no-op run
    rather than raising — proving drift is informational, not fatal.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_run_fingerprint(run_dir, {"overrides": {"agent.model": "sonnet"}})

    config = BatchRunConfig(run_dir=run_dir, overrides={"agent.model": "opus"}, preservation_mode=PreservationMode.NONE)
    summary, _ = await _run_with_experiment([], config, None, None, 1, resume=True)
    assert summary.tasks_run == 0
