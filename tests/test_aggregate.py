"""Tests for the run-summary seam: build_run_summary / recover_task_results / `aggregate`."""

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.models import AgentKind, EvaluationResult, FinalStatus, TaskResult
from coder_eval.orchestration.batch import build_run_summary, recover_task_results, write_run_summary
from coder_eval.path_utils import build_task_run_dir


def _eval(task_id: str, *, variant_id: str = "default", status: FinalStatus = FinalStatus.SUCCESS) -> EvaluationResult:
    return EvaluationResult(
        task_id=task_id,
        task_description="d",
        variant_id=variant_id,
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1, 12, 0, 0),
        final_status=status,
        iteration_count=1,
        duration_seconds=2.0,
    )


def _task_result(task_id: str, status: FinalStatus, *, variant_id: str = "default") -> TaskResult:
    return TaskResult(
        task_id=task_id,
        variant_id=variant_id,
        result=_eval(task_id, variant_id=variant_id, status=status),
        duration=2.0,
    )


def _write_task_json(run_dir: Path, result: EvaluationResult, *, replicate_index: int = 0) -> Path:
    task_dir = build_task_run_dir(run_dir, result.variant_id, result.task_id, replicate_index)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "task.json"
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


# --- build_run_summary (pure aggregation) -------------------------------------


def test_build_run_summary_buckets_via_canonical_category() -> None:
    """Buckets follow FinalStatus.category — not a hand-rolled SUCCESS/ERROR/else map."""
    results = [
        _task_result("ok", FinalStatus.SUCCESS),
        _task_result("err", FinalStatus.ERROR),
        _task_result("fail", FinalStatus.FAILURE),
        _task_result("timeout", FinalStatus.TIMEOUT),
        _task_result("budget", FinalStatus.TOKEN_BUDGET_EXCEEDED),
    ]
    start, end = datetime(2026, 1, 1, 12, 0, 0), datetime(2026, 1, 1, 12, 5, 0)

    summary = build_run_summary("my-run", results, start, end, max_parallel=4)

    assert summary.run_id == "my-run"
    assert summary.tasks_run == 5
    assert summary.tasks_succeeded == 1
    assert summary.tasks_error == 1
    # FAILURE + TIMEOUT + TOKEN_BUDGET_EXCEEDED all map to "failed".
    assert summary.tasks_failed == 3
    assert summary.tasks_token_budget_exceeded == 1
    assert summary.max_parallel == 4
    assert summary.total_duration_seconds == 300.0
    assert {row["task_id"] for row in summary.task_results} == {"ok", "err", "fail", "timeout", "budget"}


def test_build_run_summary_buckets_every_status_by_category() -> None:
    """Every FinalStatus lands in the bucket its .category names — enumeration-driven, so a
    future status that isn't wired into a counter fails here automatically."""
    statuses = list(FinalStatus)
    results = [_task_result(s.name.lower(), s) for s in statuses]
    start = datetime(2026, 1, 1, 12, 0, 0)

    summary = build_run_summary("r", results, start, start)

    assert summary.tasks_run == len(statuses)
    assert summary.tasks_succeeded == sum(1 for s in statuses if s.category == "succeeded")
    assert summary.tasks_error == sum(1 for s in statuses if s.category == "error")
    assert summary.tasks_failed == sum(1 for s in statuses if s.category == "failed")
    # Budget sub-counters are a subset of "failed" — both previously unexercised.
    assert summary.tasks_token_budget_exceeded == 1
    assert summary.tasks_cost_budget_exceeded == 1
    # The two the hand-picked test omitted both classify as "failed".
    assert FinalStatus.COST_BUDGET_EXCEEDED.category == "failed"
    assert FinalStatus.MAX_TURNS_EXHAUSTED.category == "failed"


def test_build_run_summary_is_pure_no_disk_writes(tmp_path: Path) -> None:
    """build_run_summary writes nothing — it returns a RunSummary, that's all."""
    build_run_summary("r", [_task_result("ok", FinalStatus.SUCCESS)], datetime(2026, 1, 1), datetime(2026, 1, 1))
    assert list(tmp_path.iterdir()) == []


def test_build_run_summary_carries_tags_and_paths() -> None:
    summary = build_run_summary(
        "r",
        [_task_result("ok", FinalStatus.SUCCESS)],
        datetime(2026, 1, 1),
        datetime(2026, 1, 1),
        {"ok": ["windows", "smoke"]},
        task_paths={"ok": "tasks/ok.yaml"},
    )
    (row,) = summary.task_results
    assert row["tags"] == ["windows", "smoke"]
    assert row["task_path"] == "tasks/ok.yaml"


# --- recover_task_results (disk recovery) -------------------------------------


def test_recover_task_results_round_trip(tmp_path: Path) -> None:
    _write_task_json(tmp_path, _eval("a", status=FinalStatus.SUCCESS))
    _write_task_json(tmp_path, _eval("b", status=FinalStatus.ERROR))
    _write_task_json(tmp_path, _eval("c", variant_id="other", status=FinalStatus.FAILURE))

    recovered = recover_task_results(tmp_path)

    assert [(r.variant_id, r.task_id, r.result.final_status) for r in recovered] == [
        ("default", "a", FinalStatus.SUCCESS),
        ("default", "b", FinalStatus.ERROR),
        ("other", "c", FinalStatus.FAILURE),
    ]


def test_recover_task_results_recovers_replicate_index(tmp_path: Path) -> None:
    _write_task_json(tmp_path, _eval("a"), replicate_index=0)
    _write_task_json(tmp_path, _eval("a"), replicate_index=2)
    recovered = recover_task_results(tmp_path)
    assert sorted(r.replicate_index for r in recovered) == [0, 2]


def test_recover_task_results_skips_unparseable_and_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_task_json(tmp_path, _eval("good"))
    bad = build_task_run_dir(tmp_path, "default", "bad", 0)
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "task.json").write_text("{ not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        recovered = recover_task_results(tmp_path)
    assert [r.task_id for r in recovered] == ["good"]
    # A corrupt file that exists must not vanish silently — it shrinks tasks_run.
    assert any("task.json" in m for m in caplog.messages)


def test_recover_task_results_empty_when_none(tmp_path: Path) -> None:
    assert recover_task_results(tmp_path) == []


def test_recover_task_results_excludes_nested_sub_run(tmp_path: Path) -> None:
    """task.json under a nested run dir (its own run.json) belongs to that sub-run."""
    _write_task_json(tmp_path, _eval("top"))
    # A nested sub-run: <tmp>/activation/ with its own run.json + task.json.
    nested = tmp_path / "activation"
    nested.mkdir()
    (nested / "run.json").write_text("{}", encoding="utf-8")
    _write_task_json(nested, _eval("nested"))

    recovered = recover_task_results(tmp_path)

    assert [r.task_id for r in recovered] == ["top"]
    # The nested sub-run summarises independently to its own tasks.
    assert [r.task_id for r in recover_task_results(nested)] == ["nested"]


# --- the seam composes: recover -> build reproduces a live summary ------------


def test_recover_then_build_matches_buckets(tmp_path: Path) -> None:
    """Re-aggregating from disk yields the same counts a live run would have produced."""
    for tid, status in [("a", FinalStatus.SUCCESS), ("b", FinalStatus.SUCCESS), ("c", FinalStatus.ERROR)]:
        _write_task_json(tmp_path, _eval(tid, status=status))

    summary = build_run_summary("r", recover_task_results(tmp_path), datetime(2026, 1, 1), datetime(2026, 1, 1))

    assert (summary.tasks_run, summary.tasks_succeeded, summary.tasks_error, summary.tasks_failed) == (3, 2, 1, 0)


# --- write_run_summary --------------------------------------------------------


def test_write_run_summary_emits_run_json_and_md(tmp_path: Path) -> None:
    summary = build_run_summary(
        "r", [_task_result("ok", FinalStatus.SUCCESS)], datetime(2026, 1, 1), datetime(2026, 1, 1)
    )
    write_run_summary(summary, tmp_path)
    assert (tmp_path / "run.json").exists()
    assert (tmp_path / "run.md").exists()
    on_disk = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert on_disk["tasks_succeeded"] == 1


# --- `coder-eval aggregate` CLI -----------------------------------------------


def test_aggregate_cli_rebuilds_run_json(tmp_path: Path) -> None:
    _write_task_json(tmp_path, _eval("a", status=FinalStatus.SUCCESS))
    _write_task_json(tmp_path, _eval("b", status=FinalStatus.ERROR))

    result = CliRunner().invoke(app, ["aggregate", str(tmp_path)])

    assert result.exit_code == 0, result.output
    run_json = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert run_json["tasks_run"] == 2
    assert run_json["tasks_succeeded"] == 1
    assert run_json["tasks_error"] == 1
    assert (tmp_path / "run.md").exists()


def test_aggregate_cli_carries_prior_tags(tmp_path: Path) -> None:
    """tags/source-path are static metadata — carried from an existing run.json."""
    _write_task_json(tmp_path, _eval("a", status=FinalStatus.SUCCESS))
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "max_parallel": 8,
                "task_results": [{"task_id": "a", "tags": ["windows"], "task_path": "tasks/a.yaml"}],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["aggregate", str(tmp_path)])

    assert result.exit_code == 0, result.output
    run_json = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert run_json["max_parallel"] == 8
    (row,) = run_json["task_results"]
    assert row["tags"] == ["windows"]
    assert row["task_path"] == "tasks/a.yaml"


def test_aggregate_cli_to_separate_output_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    out = tmp_path / "out"
    out.mkdir()
    _write_task_json(src, _eval("a", status=FinalStatus.SUCCESS))

    result = CliRunner().invoke(app, ["aggregate", str(src), "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert (out / "run.json").exists()
    assert not (src / "run.json").exists()
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))["run_id"] == "out"


def test_aggregate_cli_errors_on_empty_dir(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["aggregate", str(tmp_path)])
    assert result.exit_code == 1
    assert "no finalized task.json" in result.output


def _write_prior(run_dir: Path, **fields: object) -> None:
    """Write a minimal prior run.json carrying the given run-level fields."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"task_results": [{"task_id": "a"}], **fields}), encoding="utf-8")


def test_aggregate_cli_uses_prior_window_timestamps(tmp_path: Path) -> None:
    """When the prior run.json carries start/end, total_duration uses that real wall-clock."""
    _write_task_json(tmp_path, _eval("a"))
    _write_prior(tmp_path, start_time="2026-01-01T12:00:00", end_time="2026-01-01T12:10:00")

    result = CliRunner().invoke(app, ["aggregate", str(tmp_path)])

    assert result.exit_code == 0, result.output
    run_json = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert run_json["total_duration_seconds"] == 600.0


def test_aggregate_cli_malformed_prior_timestamps_fall_back_to_results(tmp_path: Path) -> None:
    """A bad timestamp string falls back to the results-derived window (started_at + duration)."""
    _write_task_json(tmp_path, _eval("a"))  # started_at 12:00:00, duration_seconds 2.0
    _write_prior(tmp_path, start_time="not-a-date", end_time="also-bad")

    result = CliRunner().invoke(app, ["aggregate", str(tmp_path)])

    assert result.exit_code == 0, result.output
    run_json = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert run_json["total_duration_seconds"] == 2.0


def test_aggregate_cli_carries_skipped_tasks_and_drops_malformed(tmp_path: Path) -> None:
    """Valid skipped_tasks carry over; a non-dict and a schema-invalid dict drop without crashing."""
    _write_task_json(tmp_path, _eval("a"))
    _write_prior(
        tmp_path,
        skipped_tasks=[
            {"path": "tasks/skip_me.yaml", "reason": "skip: true"},  # valid
            "not-a-dict",  # malformed entry → dropped
            {"reason": "missing required path"},  # schema-invalid dict → dropped, no ValidationError crash
        ],
    )

    result = CliRunner().invoke(app, ["aggregate", str(tmp_path)])

    assert result.exit_code == 0, result.output  # must NOT abort on the bad entries
    run_json = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert len(run_json["skipped_tasks"]) == 1
    assert run_json["skipped_tasks"][0]["path"] == "tasks/skip_me.yaml"


def test_aggregate_cli_coerces_missing_or_zero_max_parallel_to_one(tmp_path: Path) -> None:
    """RunSummary requires max_parallel >= 1; a missing or falsy prior value coerces to 1."""
    _write_task_json(tmp_path, _eval("a"))

    _write_prior(tmp_path)  # no max_parallel key
    assert CliRunner().invoke(app, ["aggregate", str(tmp_path)]).exit_code == 0
    assert json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["max_parallel"] == 1

    _write_prior(tmp_path, max_parallel=0)  # falsy → still coerced to 1
    assert CliRunner().invoke(app, ["aggregate", str(tmp_path)]).exit_code == 0
    assert json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["max_parallel"] == 1


def test_aggregate_cli_output_rejects_a_file(tmp_path: Path) -> None:
    """`-o` is a directory (file_okay=False) — pointing it at a file is a clean usage error."""
    _write_task_json(tmp_path, _eval("a"))
    a_file = tmp_path / "not_a_dir.txt"
    a_file.write_text("x", encoding="utf-8")

    result = CliRunner().invoke(app, ["aggregate", str(tmp_path), "-o", str(a_file)])

    assert result.exit_code != 0  # Typer rejects before our code runs (no raw OSError traceback)
