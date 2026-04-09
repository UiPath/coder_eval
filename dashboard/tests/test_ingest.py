"""Tests for the ingest module's pure parsing functions."""

import json
from pathlib import Path

from dashboard.ingest import (
    build_analysis_row,
    build_criteria_rows,
    build_smoke_runs_row,
    build_task_result_row,
    find_task_jsons,
    parse_run,
    to_csv_row,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_to_csv_row_simple():
    assert to_csv_row(["a", "b", "c"]) == "a,b,c"


def test_to_csv_row_quoting():
    row = to_csv_row(["has,comma", 'has"quote', "plain"])
    assert '"has,comma"' in row
    assert '"has""quote"' in row


def test_build_smoke_runs_row():
    with open(FIXTURES / "run.json") as f:
        data = json.load(f)

    row = build_smoke_runs_row(data, "exp-001")
    fields = row.split(",")
    assert fields[0] == "run-20260312-060000"
    assert fields[1] == "exp-001"
    # success_rate = 2/3
    assert float(fields[9]) > 0.66
    assert float(fields[9]) < 0.67


def test_build_task_result_row():
    with open(FIXTURES / "default" / "task_001" / "task.json") as f:
        task = json.load(f)

    row = build_task_result_row("run-001", task)
    assert "run-001" in row
    assert "task_001" in row
    assert "succeeded" in row


def test_build_criteria_rows():
    with open(FIXTURES / "default" / "task_001" / "task.json") as f:
        task = json.load(f)

    rows = build_criteria_rows("run-001", task)
    assert len(rows) == 2
    assert "file_content" in rows[0]
    assert "test_pass" in rows[1]


def test_build_criteria_rows_empty():
    rows = build_criteria_rows("run-001", {"success_criteria_results": []})
    assert rows == []


def test_find_task_jsons():
    found = find_task_jsons(FIXTURES)
    assert len(found) == 1
    assert found[0].name == "task.json"


def test_parse_run():
    run_id, experiment_id, smoke_row, task_rows, criteria_rows = parse_run(FIXTURES)
    assert run_id == "run-20260312-060000"
    assert experiment_id == "exp-smoke-20260312"
    assert "exp-smoke-20260312" in smoke_row
    assert len(task_rows) == 1
    assert len(criteria_rows) == 2


def test_to_csv_row_escapes_newlines():
    """KQL inline ingest splits on raw newlines; verify they're escaped."""
    row = to_csv_row(["line1\nline2", "ok"])
    assert "\n" not in row
    assert "line1\\nline2" in row


def test_to_csv_row_escapes_crlf():
    row = to_csv_row(["a\r\nb", "c\rd"])
    assert "\r" not in row
    assert "\n" not in row


def test_parse_run_fallback_path(tmp_path):
    """When no task.json files exist, parse_run falls back to run.json task_results."""
    run_data = {
        "run_id": "run-fallback",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:01:00Z",
        "total_duration_seconds": 60,
        "tasks_run": 1,
        "tasks_succeeded": 1,
        "tasks_failed": 0,
        "tasks_error": 0,
        "task_results": [
            {
                "task_id": "t1",
                "status": "succeeded",
                "weighted_score": 1.0,
                "duration": 30.0,
                "iteration_count": 1,
                "model_used": "test-model",
                "tags": ["smoke"],
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "total_tokens": 150,
                "total_cost_usd": 0.01,
                "turns": [{"assistant_turn_count": 1}],
            }
        ],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_data))

    run_id, experiment_id, _smoke_row, task_rows, criteria_rows = parse_run(tmp_path)
    assert run_id == "run-fallback"
    assert experiment_id == ""
    assert len(task_rows) == 1
    assert "t1" in task_rows[0]
    assert criteria_rows == []


def test_build_criteria_rows_non_string_details():
    """Criteria details/error can be dict/list/None; must not crash to_csv_row."""
    task = {
        "task_id": "t1",
        "variant_id": "default",
        "success_criteria_results": [
            {
                "criterion_type": "file_check",
                "description": "check",
                "score": 1.0,
                "details": {"matched": True, "lines": [1, 2]},
                "error": None,
            },
            {
                "criterion_type": "pytest",
                "description": "tests",
                "score": 0.5,
                "details": ["test1 passed", "test2 failed"],
                "error": {"code": 1, "message": "failures"},
            },
        ],
    }
    rows = build_criteria_rows("run-001", task)
    assert len(rows) == 2
    # Should not crash and should contain serialized data
    assert "file_check" in rows[0]
    assert "pytest" in rows[1]


def test_build_analysis_row(tmp_path):
    md = tmp_path / "analysis.md"
    md.write_text("# Analysis\n\nAll 3 tasks passed.\nCost: $0.05")
    row = build_analysis_row("run-001", "exp-001", md)
    assert "run-001" in row
    assert "exp-001" in row
    assert "All 3 tasks passed" in row


def test_build_analysis_row_special_chars(tmp_path):
    md = tmp_path / "analysis.md"
    md.write_text('Has "quotes", commas, and\nnewlines')
    row = build_analysis_row("run-002", "", md)
    # CSV quoting should handle special characters
    assert "run-002" in row
    assert "quotes" in row
