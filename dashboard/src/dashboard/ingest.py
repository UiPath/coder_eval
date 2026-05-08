"""Parse coder-eval results and ingest into ADX.

Pure parsing functions are separated from Kusto I/O for testability.
"""

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from . import adx

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def to_csv_row(fields: list[str]) -> str:
    """Convert a list of fields to a properly quoted CSV row.

    KQL inline ingest splits rows on raw newlines regardless of CSV quoting,
    so we must escape embedded newlines before writing.
    """
    safe = [
        (json.dumps(f) if isinstance(f, (dict, list)) else ("" if f is None else str(f)))
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        for f in fields
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(safe)
    return buf.getvalue().rstrip("\r\n")


def build_smoke_runs_row(data: dict, experiment_id: str) -> str:
    tasks_run = data["tasks_run"]
    tasks_succeeded = data["tasks_succeeded"]
    success_rate = tasks_succeeded / tasks_run if tasks_run > 0 else 0.0
    env_info = data.get("environment_info", {})
    git_commit = env_info.get("git_commit", "")

    fields = [
        data["run_id"],
        experiment_id,
        data["start_time"],
        data["end_time"],
        str(data["total_duration_seconds"]),
        str(tasks_run),
        str(tasks_succeeded),
        str(data["tasks_failed"]),
        str(data["tasks_error"]),
        str(success_rate),
        data.get("framework_version", ""),
        git_commit,
        json.dumps(env_info),
    ]
    return to_csv_row(fields)


def build_task_result_row(run_id: str, task: dict) -> str:
    tags = task.get("task_config", {}).get("resolved", {}).get("tags", [])
    cmd_stats = task.get("command_stats") or {}
    token_usage = task.get("total_token_usage") or {}
    turns = task.get("turns", [])
    total_assistant_turns = sum(t.get("assistant_turn_count", 0) for t in turns)

    review = task.get("_review") or {}
    review_tags = review.get("tags") or []
    review_summary = review.get("summary") or ""

    fields = [
        run_id,
        task.get("task_id", ""),
        task.get("variant_id", "default"),
        task.get("task_description", ""),
        task.get("final_status", ""),
        str(task.get("weighted_score") or 0.0),
        str(task.get("duration_seconds", 0.0)),
        str(task.get("iteration_count", 0)),
        task.get("model_used", ""),
        json.dumps(tags),
        str(token_usage.get("input_tokens", 0)),
        str(token_usage.get("output_tokens", 0)),
        str(token_usage.get("cache_creation_input_tokens", 0)),
        str(token_usage.get("cache_read_input_tokens", 0)),
        str(token_usage.get("input_tokens", 0) + token_usage.get("output_tokens", 0)),
        str(token_usage.get("total_cost_usd", 0.0)),
        str(len(turns)),
        str(total_assistant_turns),
        str(cmd_stats.get("total_commands", 0)),
        str(cmd_stats.get("successful_commands", 0)),
        str(cmd_stats.get("failed_commands", 0)),
        str(cmd_stats.get("success_rate", 0.0)),
        json.dumps(cmd_stats.get("commands_by_tool", {})),
        task.get("error_message") or "",
        task.get("started_at", ""),
        task.get("completed_at") or "",
        json.dumps(review_tags),
        review_summary,
    ]
    return to_csv_row(fields)


def build_criteria_rows(run_id: str, task: dict) -> list[str]:
    task_id = task.get("task_id", "")
    variant_id = task.get("variant_id", "default")
    rows = []

    for cr in task.get("success_criteria_results", []):
        fields = [
            run_id,
            task_id,
            variant_id,
            cr.get("criterion_type", ""),
            cr.get("description", ""),
            str(cr.get("score", 0.0)),
            cr.get("details", ""),
            cr.get("error") or "",
        ]
        rows.append(to_csv_row(fields))

    return rows


def _load_review(task_json_path: Path) -> dict:
    """Load review.json sitting next to the task.json. Returns {} if absent or malformed."""
    review_path = task_json_path.parent / "review.json"
    if not review_path.exists():
        return {}
    try:
        with open(review_path) as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: failed to load {review_path}: {exc}")
        return {}
    if not isinstance(payload, dict):
        print(f"WARNING: ignored non-object review payload at {review_path}: {type(payload).__name__}")
        return {}
    return payload


def build_analysis_row(run_id: str, experiment_id: str, analysis_path: Path) -> str:
    content = analysis_path.read_text()
    generated_at = datetime.now(UTC).isoformat()
    return to_csv_row([run_id, experiment_id, generated_at, content])


def find_task_jsons(run_path: Path) -> list[Path]:
    # <variant_id>/<task_id>/<replicate_index>/task.json — new layout only;
    # flat-layout runs uploaded before the replicate-index change are
    # intentionally skipped.
    return sorted(run_path.glob("*/*/*/task.json"))


def _read_experiment_id(run_path: Path) -> str:
    experiment_json_path = run_path / "experiment.json"
    if experiment_json_path.exists():
        with open(experiment_json_path) as f:
            return json.load(f).get("experiment_id", "")
    return ""


def _task_summary_to_task_dict(summary: dict) -> dict:
    """Convert a run.json task_summary into the shape expected by build_task_result_row."""
    return {
        "task_id": summary.get("task_id", ""),
        "variant_id": summary.get("variant_id", "default"),
        "task_description": "",
        "final_status": summary.get("status", ""),
        "weighted_score": summary.get("weighted_score", 0.0),
        "duration_seconds": summary.get("duration", 0.0),
        "iteration_count": summary.get("iteration_count", 0),
        "model_used": summary.get("model_used", ""),
        "task_config": {"resolved": {"tags": summary.get("tags", [])}},
        "total_token_usage": {
            "input_tokens": summary.get("input_tokens", 0),
            "output_tokens": summary.get("output_tokens", 0),
            "cache_creation_input_tokens": summary.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": summary.get("cache_read_input_tokens", 0),
            "total_cost_usd": summary.get("total_cost_usd", 0.0),
        },
        "command_stats": None,
        "turns": summary.get("turns", []),
        "error_message": None,
        "started_at": "",
        "completed_at": None,
    }


def parse_run(
    run_path: Path,
) -> tuple[str, str, str, list[str], list[str]]:
    """Parse a run directory into CSV rows.

    Returns (run_id, experiment_id, smoke_row, task_rows, criteria_rows).
    """
    run_json_path = run_path / "run.json"

    with open(run_json_path) as f:
        run_data = json.load(f)

    run_id = run_data["run_id"]
    experiment_id = _read_experiment_id(run_path)
    smoke_row = build_smoke_runs_row(run_data, experiment_id)

    task_rows: list[str] = []
    criteria_rows: list[str] = []

    for task_json_path in find_task_jsons(run_path):
        with open(task_json_path) as f:
            task_data = json.load(f)
        # Per-task review.json sits alongside task.json (written by the
        # /coder-eval-review skill). Absent for older runs — no-op.
        task_data["_review"] = _load_review(task_json_path)
        task_rows.append(build_task_result_row(run_id, task_data))
        criteria_rows.extend(build_criteria_rows(run_id, task_data))

    # Fallback: use run.json task_results if no task.json files found
    if not task_rows:
        for task_summary in run_data.get("task_results", []):
            task_rows.append(build_task_result_row(run_id, _task_summary_to_task_dict(task_summary)))

    return run_id, experiment_id, smoke_row, task_rows, criteria_rows


# ---------------------------------------------------------------------------
# ADX ingestion (side-effectful)
# ---------------------------------------------------------------------------


def ingest_run(run_dir: str, cluster_uri: str, database: str) -> None:
    run_path = Path(run_dir)
    run_id, experiment_id, smoke_row, task_rows, criteria_rows = parse_run(run_path)

    client = adx.get_client(cluster_uri)

    print(f"Ingesting into SmokeRuns: run_id={run_id}")
    client.execute_mgmt(database, f".ingest inline into table SmokeRuns <|\n{smoke_row}")
    print("  SmokeRuns OK")

    if task_rows:
        rows_block = "\n".join(task_rows)
        print(f"Ingesting {len(task_rows)} row(s) into TaskResults")
        client.execute_mgmt(database, f".ingest inline into table TaskResults <|\n{rows_block}")
        print("  TaskResults OK")

    if criteria_rows:
        rows_block = "\n".join(criteria_rows)
        print(f"Ingesting {len(criteria_rows)} row(s) into CriteriaResults")
        client.execute_mgmt(database, f".ingest inline into table CriteriaResults <|\n{rows_block}")
        print("  CriteriaResults OK")

    # Ingest analysis if present
    analysis_path = run_path / "analysis.md"
    if analysis_path.exists():
        row = build_analysis_row(run_id, experiment_id, analysis_path)
        print("Ingesting into RunAnalysis")
        client.execute_mgmt(database, f".ingest inline into table RunAnalysis <|\n{row}")
        print("  RunAnalysis OK")

    print(f"Done. Ingested run {run_id}: {len(task_rows)} tasks, {len(criteria_rows)} criteria.")
