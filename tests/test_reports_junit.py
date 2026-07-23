"""Unit tests for the disk-driven JUnit XML writer (``reports_junit``).

All tests are hermetic: the run directory is built by hand under ``tmp_path``
(no agents, no API). Test-side XML parsing uses ``defusedxml`` as
defense-in-depth even though the parsed strings are self-generated.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from defusedxml.ElementTree import fromstring

from coder_eval.models import SuiteRollup, ThresholdCheck
from coder_eval.reports_junit import generate_junit_xml, write_junit_xml


def _props(case: Any) -> dict[str, str]:
    """Map of a testcase's <property name=value> children."""
    properties = case.find("properties")
    return {p.get("name"): p.get("value") for p in properties.findall("property")} if properties is not None else {}


def _write_task_json(
    run_dir: Path,
    variant: str,
    task_id: str,
    replicate_index: int,
    criteria: list[dict[str, Any]],
) -> None:
    """Write a minimal task.json (plain dict) at the run-layout location."""
    task_dir = run_dir / variant / task_id / f"{replicate_index:02d}"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(json.dumps({"success_criteria_results": criteria}), encoding="utf-8")


def _row(
    task_id: str,
    status: str,
    *,
    variant_id: str | None = "default",
    replicate_index: int | None = 0,
    duration: float | None = 1.5,
    task_path: str | None = "tasks/sample.yaml",
    weighted_score: float | None = 0.5,
    model_used: str | None = "claude-haiku-4-5-20251001",
    total_tokens: int | None = 1234,
    total_cost_usd: float | None = 0.01,
    visible_turns: int | None = 3,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": status,
        "variant_id": variant_id,
        "replicate_index": replicate_index,
        "duration": duration,
        "task_path": task_path,
        "weighted_score": weighted_score,
        "model_used": model_used,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost_usd,
        "visible_turns": visible_turns,
    }


def _find_testsuite(root: Any, name: str) -> Any:
    for ts in root.findall("testsuite"):
        if ts.get("name") == name:
            return ts
    return None


def test_happy_path_grouping_and_counts(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [
        _row("t_pass", "SUCCESS", variant_id="v1"),
        _row("t_fail", "FAILURE", variant_id="v1"),
        _row("t_err", "ERROR", variant_id="v1"),
        _row("t_pass", "SUCCESS", variant_id="v2"),
        _row("t_fail", "FAILURE", variant_id="v2"),
        _row("t_err", "ERROR", variant_id="v2"),
    ]
    write_run_json(run_dir, rows)

    root = fromstring(generate_junit_xml(run_dir))
    assert root.tag == "testsuites"
    # Root counts match the emitted children.
    assert int(root.get("tests")) == 6
    assert int(root.get("failures")) == 2
    assert int(root.get("errors")) == 2
    assert int(root.get("skipped")) == 0

    for variant in ("v1", "v2"):
        ts = _find_testsuite(root, variant)
        assert ts is not None
        cases = ts.findall("testcase")
        assert int(ts.get("tests")) == len(cases) == 3
        assert int(ts.get("failures")) == 1
        assert int(ts.get("errors")) == 1
        # classname derives from task_path stem.
        assert all(c.get("classname") == "sample" for c in cases)
        # time is a float string.
        assert all(float(c.get("time")) == 1.5 for c in cases)


def test_root_counts_equal_summed_children(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("a", "SUCCESS"), _row("b", "FAILURE"), _row("c", "MAX_TURNS_EXHAUSTED")]
    write_run_json(run_dir, rows)
    root = fromstring(generate_junit_xml(run_dir))

    total_cases = 0
    total_failures = 0
    total_errors = 0
    for ts in root.findall("testsuite"):
        cases = ts.findall("testcase")
        total_cases += len(cases)
        total_failures += sum(1 for c in cases if c.find("failure") is not None)
        total_errors += sum(1 for c in cases if c.find("error") is not None)
    assert int(root.get("tests")) == total_cases
    assert int(root.get("failures")) == total_failures
    assert int(root.get("errors")) == total_errors
    # MAX_TURNS_EXHAUSTED is category "failed".
    assert total_failures == 2
    assert total_errors == 0


def test_failure_body_from_task_json(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("t_fail", "FAILURE", variant_id="v1", replicate_index=0)]
    write_run_json(run_dir, rows)
    _write_task_json(
        run_dir,
        "v1",
        "t_fail",
        0,
        [
            {
                "criterion_type": "file_exists",
                "description": "output.txt must exist",
                "score": 0.0,
                "pass_threshold": 0.9,
                "details": "file not found on disk",
                "error": None,
            },
            {
                "criterion_type": "file_contains",
                "description": "greeting present",
                "score": 1.0,
                "pass_threshold": 0.9,
                "details": None,
                "error": None,
            },
        ],
    )
    root = fromstring(generate_junit_xml(run_dir))
    ts = _find_testsuite(root, "v1")
    failure = ts.find("testcase").find("failure")
    body = failure.text or ""
    assert "file_exists" in body
    assert "0.00" in body and "0.90" in body  # score-vs-threshold line
    assert "file not found on disk" in body
    assert "output.txt must exist" in body
    # passed criterion appears as a one-liner.
    assert "file_contains" in body


def test_fallback_body_without_task_json(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("t_fail", "FAILURE", variant_id="v1", weighted_score=0.42)]
    write_run_json(run_dir, rows)
    # No task.json written.
    root = fromstring(generate_junit_xml(run_dir))
    ts = _find_testsuite(root, "v1")
    body = ts.find("testcase").find("failure").text or ""
    assert "FAILURE" in body
    assert "0.42" in body


def test_replicates_naming_and_per_replicate_task_json(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [
        _row("task", "FAILURE", variant_id="v1", replicate_index=0),
        _row("task", "FAILURE", variant_id="v1", replicate_index=1),
    ]
    write_run_json(run_dir, rows)
    _write_task_json(
        run_dir,
        "v1",
        "task",
        0,
        [
            {
                "criterion_type": "c",
                "description": "rep0",
                "score": 0.0,
                "pass_threshold": 0.9,
                "details": "REP0DETAIL",
                "error": None,
            }
        ],
    )
    _write_task_json(
        run_dir,
        "v1",
        "task",
        1,
        [
            {
                "criterion_type": "c",
                "description": "rep1",
                "score": 0.0,
                "pass_threshold": 0.9,
                "details": "REP1DETAIL",
                "error": None,
            }
        ],
    )
    root = fromstring(generate_junit_xml(run_dir))
    ts = _find_testsuite(root, "v1")
    names = {c.get("name") for c in ts.findall("testcase")}
    assert names == {"task[00]", "task[01]"}
    bodies = {c.get("name"): (c.find("failure").text or "") for c in ts.findall("testcase")}
    assert "REP0DETAIL" in bodies["task[00]"]
    assert "REP1DETAIL" in bodies["task[01]"]


def test_replicate_index_none_globs_task_json(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("task", "FAILURE", variant_id="v1", replicate_index=None)]
    write_run_json(run_dir, rows)
    _write_task_json(
        run_dir,
        "v1",
        "task",
        0,
        [
            {
                "criterion_type": "c",
                "description": "d",
                "score": 0.0,
                "pass_threshold": 0.9,
                "details": "GLOBBED",
                "error": None,
            }
        ],
    )
    root = fromstring(generate_junit_xml(run_dir))
    ts = _find_testsuite(root, "v1")
    case = ts.find("testcase")
    assert case.get("name") == "task"  # no [NN] suffix
    assert "GLOBBED" in (case.find("failure").text or "")


def test_variant_none_grouped_as_default(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("t", "SUCCESS", variant_id=None, task_path=None)]
    write_run_json(run_dir, rows)
    root = fromstring(generate_junit_xml(run_dir))
    ts = _find_testsuite(root, "default")
    assert ts is not None
    # task_path None → classname falls back to variant.
    assert ts.find("testcase").get("classname") == "default"


def test_duration_none_renders_zero(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("t", "SUCCESS", duration=None)]
    write_run_json(run_dir, rows)
    root = fromstring(generate_junit_xml(run_dir))
    case = root.find("testsuite").find("testcase")
    assert float(case.get("time")) == 0.0


def test_properties_omit_none_values(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [
        _row("t", "SUCCESS", total_cost_usd=None, total_tokens=None, weighted_score=None),
    ]
    write_run_json(run_dir, rows)
    xml = generate_junit_xml(run_dir)
    root = fromstring(xml)
    props = root.find("testsuite").find("testcase").find("properties")
    names = {p.get("name") for p in props.findall("property")} if props is not None else set()
    assert "total_cost_usd" not in names
    assert "total_tokens" not in names
    assert "weighted_score" not in names
    assert "model_used" in names
    # No literal "None" strings leaked into the tree.
    assert "None" not in xml


def test_skipped_tasks_suite(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("t", "SUCCESS")]
    write_run_json(
        run_dir,
        rows,
        skipped=[("tasks/broken.yaml", "ValueError: bad schema"), ("tasks/opt.yaml", "skip: true")],
    )
    root = fromstring(generate_junit_xml(run_dir))
    assert int(root.get("skipped")) == 2
    ts = _find_testsuite(root, "skipped")
    assert ts is not None
    skipped_cases = ts.findall("testcase")
    assert len(skipped_cases) == 2
    # Suffix-stripped full path (not just the stem) so same-basename tasks stay
    # distinct. Always '/'-separated: the same logical run must yield the same
    # testcase identity on Windows and Linux, or CI history splits in two.
    names = {c.get("name") for c in skipped_cases}
    assert names == {"tasks/broken", "tasks/opt"}
    assert not any("\\" in n for n in names)
    assert all(c.find("skipped") is not None for c in skipped_cases)


def test_suite_gates_failing_and_none_metric(tmp_path: Path, write_run_json: Callable[..., Path]) -> None:
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [_row("t", "SUCCESS", variant_id="v1")])
    rollup = SuiteRollup(
        suite_id="s1",
        variant_id="v1",
        rows_total=10,
        rows_passed=5,
        rows_failed=5,
        rows_error=0,
        pass_rate=0.5,
        passed=False,
    )
    # Inject two failed threshold checks via a criterion aggregate.
    from coder_eval.models import CriterionAggregate

    rollup.criterion_aggregates = [
        CriterionAggregate(
            criterion_type="classification_match",
            passed=False,
            threshold_checks=[
                ThresholdCheck(metric="accuracy", min_value=0.9, actual_value=0.5, passed=False),
                ThresholdCheck(metric="f1.macro", min_value=0.8, actual_value=None, passed=False),
            ],
        ),
        # A criterion that declared suite_thresholds but aggregate() returned nothing.
        CriterionAggregate(
            criterion_type="skill_triggered",
            passed=False,
            error="aggregate() returned no metrics",
        ),
    ]
    suite_dir = run_dir / "v1" / "s1"
    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / "suite.json").write_text(rollup.model_dump_json(indent=2), encoding="utf-8")

    root = fromstring(generate_junit_xml(run_dir))
    ts = _find_testsuite(root, "suite-gates")
    assert ts is not None
    case = ts.find("testcase")
    assert case.get("name") == "v1/s1"
    body = case.find("failure").text or ""
    assert "accuracy" in body and "0.5" in body and "0.9" in body
    assert "f1.macro" in body
    assert "metric not emitted" in body
    # CriterionAggregate.error is surfaced in the gate body.
    assert "skill_triggered" in body and "aggregate() returned no metrics" in body


def test_suite_gates_passing_is_plain_case(tmp_path: Path, write_run_json: Callable[..., Path]) -> None:
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [_row("t", "SUCCESS", variant_id="v1")])
    rollup = SuiteRollup(
        suite_id="s1",
        variant_id="v1",
        rows_total=2,
        rows_passed=2,
        rows_failed=0,
        rows_error=0,
        pass_rate=1.0,
        passed=True,
    )
    suite_dir = run_dir / "v1" / "s1"
    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / "suite.json").write_text(rollup.model_dump_json(indent=2), encoding="utf-8")
    root = fromstring(generate_junit_xml(run_dir))
    ts = _find_testsuite(root, "suite-gates")
    case = ts.find("testcase")
    assert case.find("failure") is None


def test_corrupt_suite_json_skipped_with_report_still_produced(
    tmp_path: Path, write_run_json: Callable[..., Path], caplog: pytest.LogCaptureFixture
) -> None:
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [_row("t", "SUCCESS", variant_id="v1")])
    suite_dir = run_dir / "v1" / "s1"
    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / "suite.json").write_text("{not valid json", encoding="utf-8")
    # Report still generated; no suite-gates suite (the only rollup was corrupt).
    root = fromstring(generate_junit_xml(run_dir))
    assert _find_testsuite(root, "suite-gates") is None


def test_xml_safety_control_chars_and_markup(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("t_fail", "FAILURE", variant_id="v1", replicate_index=0)]
    write_run_json(run_dir, rows)
    nasty = "\x1b[31mboom\x00 <tag> & 'quote'"
    _write_task_json(
        run_dir,
        "v1",
        "t_fail",
        0,
        [
            {
                "criterion_type": "c",
                "description": "d",
                "score": 0.0,
                "pass_threshold": 0.9,
                "details": nasty,
                "error": None,
            }
        ],
    )
    xml = generate_junit_xml(run_dir)
    root = fromstring(xml)  # must re-parse
    body = _find_testsuite(root, "v1").find("testcase").find("failure").text or ""
    assert "boom" in body
    assert "\x00" not in body  # scrubbed
    assert "\x1b" not in body


def test_unknown_status_lands_in_errors(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rows = [_row("t", "SOME_FUTURE_STATUS", variant_id="v1")]
    write_run_json(run_dir, rows)
    root = fromstring(generate_junit_xml(run_dir))
    assert int(root.get("errors")) == 1
    err = _find_testsuite(root, "v1").find("testcase").find("error")
    assert err is not None
    assert "unknown status" in (err.get("message") or "")


def test_empty_run_all_skipped(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [], skipped=[("tasks/a.yaml", "skip: true")])
    root = fromstring(generate_junit_xml(run_dir))
    assert int(root.get("tests")) == 1  # only the skipped case
    assert int(root.get("skipped")) == 1
    assert int(root.get("failures")) == 0
    assert int(root.get("errors")) == 0


def test_missing_run_json_raises(tmp_path: Path) -> None:
    run_dir = tmp_path / "empty"
    run_dir.mkdir()
    with pytest.raises(FileNotFoundError, match=r"run\.json"):
        generate_junit_xml(run_dir)


def test_write_junit_xml_creates_parents_and_returns_path(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [_row("t", "SUCCESS")])
    out = tmp_path / "nested" / "deeper" / "junit.xml"
    written = write_junit_xml(run_dir, out)
    assert written == out
    assert out.is_file()
    fromstring(out.read_text(encoding="utf-8"))  # parses


# --------------------------------------------------------------------------
# Robustness against schema-skewed / crafted run.json rows (final-review findings)
# --------------------------------------------------------------------------


def test_malformed_row_types_degrade_gracefully(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """Loose `task_results` dicts are untyped; a skewed row must not abort the report.

    Covers: bool replicate_index (bool is an int subclass), non-finite duration
    (would emit an invalid time="nan"), non-str variant_id / task_path, and a
    row with no `status` key at all.
    """
    run_dir = tmp_path / "run"
    rows: list[dict[str, Any]] = [
        {"task_id": "t_bool", "status": "SUCCESS", "replicate_index": True, "duration": 1.0},
        {"task_id": "t_nan", "status": "SUCCESS", "duration": float("nan")},
        {"task_id": "t_inf", "status": "SUCCESS", "duration": float("inf")},
        {"task_id": "t_neg", "status": "SUCCESS", "duration": -5.0},
        {"task_id": "t_variant", "status": "SUCCESS", "variant_id": 17},
        {"task_id": "t_path", "status": "SUCCESS", "task_path": 42},
        {"task_id": "t_nostatus"},
    ]
    write_run_json(run_dir, rows)

    xml = generate_junit_xml(run_dir)
    root = fromstring(xml)  # must still be well-formed

    cases = [c for ts in root.findall("testsuite") for c in ts.findall("testcase")]
    assert len(cases) == len(rows)
    # Every time attribute must be a finite, non-negative float (JUnit requirement).
    for case in cases:
        t = float(case.get("time"))
        assert math.isfinite(t) and t >= 0.0  # finite (not NaN/inf), non-negative
    # A bool replicate_index must NOT be formatted as a [NN] replicate suffix.
    names = {c.get("name") for c in cases}
    assert "t_bool" in names
    # The missing-status row lands in the error bucket, not silently as a pass.
    assert int(root.get("errors")) == 1


def test_task_json_invalid_utf8_falls_back(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """A task.json with undecodable bytes must degrade, not raise (UnicodeDecodeError
    is a ValueError but NOT a json.JSONDecodeError)."""
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [_row("t_fail", "FAILURE", variant_id="v1", replicate_index=0)])
    task_dir = run_dir / "v1" / "t_fail" / "00"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_bytes(b"\xff\xfe\x00 not utf-8")

    root = fromstring(generate_junit_xml(run_dir))
    body = _find_testsuite(root, "v1").find("testcase").find("failure").text or ""
    assert "FAILURE" in body  # status-only fallback body


def test_task_json_lookup_cannot_escape_run_dir(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """A crafted variant_id/task_id must not make the writer read outside run_dir."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "task.json").write_text(
        json.dumps(
            {
                "success_criteria_results": [
                    {
                        "criterion_type": "leaked",
                        "description": "SECRET",
                        "score": 0.0,
                        "pass_threshold": 0.9,
                        "details": "LEAKED_SECRET_DETAIL",
                        "error": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [{"task_id": "..", "status": "FAILURE", "variant_id": "../outside"}])

    root = fromstring(generate_junit_xml(run_dir))
    xml_text = generate_junit_xml(run_dir)
    assert "LEAKED_SECRET_DETAIL" not in xml_text
    body = root.find("testsuite").find("testcase").find("failure").text or ""
    assert "FAILURE" in body  # fell back, did not read the outside file


def test_ambiguous_replicate_does_not_misattribute(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """replicate_index=None with MULTIPLE replicate dirs must fall back rather than
    attribute replicate 00's failure detail to the row."""
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [_row("task", "FAILURE", variant_id="v1", replicate_index=None)])
    for idx, marker in ((0, "REP0ONLY"), (1, "REP1ONLY")):
        _write_task_json(
            run_dir,
            "v1",
            "task",
            idx,
            [
                {
                    "criterion_type": "c",
                    "description": "d",
                    "score": 0.0,
                    "pass_threshold": 0.9,
                    "details": marker,
                    "error": None,
                }
            ],
        )
    root = fromstring(generate_junit_xml(run_dir))
    body = _find_testsuite(root, "v1").find("testcase").find("failure").text or ""
    assert "REP0ONLY" not in body and "REP1ONLY" not in body
    assert "FAILURE" in body


def test_skipped_names_unique_for_same_stem(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """Two skipped tasks sharing a basename must get distinct testcase identities."""
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [], skipped=[("suiteA/task.yaml", "skip: true"), ("suiteB/task.yaml", "skip: true")])
    root = fromstring(generate_junit_xml(run_dir))
    ts = _find_testsuite(root, "skipped")
    names = {c.get("name") for c in ts.findall("testcase")}
    assert len(names) == 2, f"skipped testcase names collided: {names}"


def test_skipped_name_is_platform_independent(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """A Windows-style path recorded in run.json must still emit a '/'-separated
    name, so a report generated on Windows matches one generated on Linux."""
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [], skipped=[(r"tasks\win\task.yaml", "skip: true")])
    root = fromstring(generate_junit_xml(run_dir))
    name = _find_testsuite(root, "skipped").find("testcase").get("name")
    assert name == "tasks/win/task"


def test_dataset_nested_task_id_loads_task_json(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """A dataset-derived task_id ('<suite>/<row_id>') maps to a real nested dir,
    so task.json must still load and per-criterion detail must be emitted rather
    than degrading to the status-only fallback body."""
    run_dir = tmp_path / "run"
    rows = [_row("activation/row_0", "FAILURE", variant_id="v1", replicate_index=0)]
    write_run_json(run_dir, rows)
    _write_task_json(
        run_dir,
        "v1",
        "activation/row_0",
        0,
        [
            {
                "criterion_type": "skill_triggered",
                "description": "must engage the target skill",
                "score": 0.0,
                "pass_threshold": 0.9,
                "details": "NESTED_DETAIL_LOADED",
                "error": None,
            }
        ],
    )
    root = fromstring(generate_junit_xml(run_dir))
    body = _find_testsuite(root, "v1").find("testcase").find("failure").text or ""
    assert "skill_triggered" in body
    assert "NESTED_DETAIL_LOADED" in body  # nested task.json was read, not skipped


def test_nested_task_id_with_dotdot_still_cannot_escape(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """Allowing an internal '/' must not open a '..' traversal: a nested task_id
    containing a '..' component is rejected before any read."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "task.json").write_text(
        json.dumps(
            {
                "success_criteria_results": [
                    {
                        "criterion_type": "leaked",
                        "description": "SECRET",
                        "score": 0.0,
                        "pass_threshold": 0.9,
                        "details": "LEAKED_VIA_NESTED_DOTDOT",
                        "error": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    write_run_json(
        run_dir,
        [{"task_id": "a/../../outside", "status": "FAILURE", "variant_id": "v1", "replicate_index": 0}],
    )
    xml_text = generate_junit_xml(run_dir)
    assert "LEAKED_VIA_NESTED_DOTDOT" not in xml_text
    root = fromstring(xml_text)
    body = _find_testsuite(root, "v1").find("testcase").find("failure").text or ""
    assert "FAILURE" in body  # fell back to status-only body


def test_informational_criterion_not_rendered_as_failure(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """A non-gating (informational) criterion below its threshold must render as
    [INFO], never [FAIL] — it is excluded from the score/gate, so it cannot be
    the failure cause (mirrors reports.py's `if not cr.gating`)."""
    run_dir = tmp_path / "run"
    rows = [_row("t_fail", "FAILURE", variant_id="v1", replicate_index=0)]
    write_run_json(run_dir, rows)
    _write_task_json(
        run_dir,
        "v1",
        "t_fail",
        0,
        [
            {
                "criterion_type": "commands_efficiency",
                "description": "informational efficiency metric",
                "score": 0.1,
                "pass_threshold": 0.9,
                "gating": False,
                "details": None,
                "error": None,
            }
        ],
    )
    body = _find_testsuite(fromstring(generate_junit_xml(run_dir)), "v1").find("testcase").find("failure").text or ""
    assert "[INFO]" in body
    assert "[FAIL]" not in body  # informational criterion is not the failure cause
    assert "commands_efficiency" in body


def test_root_time_finite_for_nonfinite_total_duration(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """A corrupt/blob-pulled run.json with a NaN total_duration_seconds must not
    emit an invalid root time='nan' — the root time is guarded like per-testcase
    time and falls back to '0.000'.

    RunSummary has no finite validator and pydantic's JSON parser accepts a bare
    ``NaN`` literal, so a foreign run.json can carry one; we inject it directly
    since ``model_dump_json`` would coerce a NaN float to ``null``.
    """
    run_dir = tmp_path / "run"
    run_json = write_run_json(run_dir, [_row("t", "SUCCESS")])
    text = run_json.read_text(encoding="utf-8")
    assert '"total_duration_seconds": 300.0' in text
    run_json.write_text(text.replace('"total_duration_seconds": 300.0', '"total_duration_seconds": NaN'), "utf-8")

    root = fromstring(generate_junit_xml(run_dir))
    assert math.isfinite(float(root.get("time")))
    assert root.get("time") == "0.000"


def test_parity_real_producer_output_through_writer(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """Feed REAL ``eval_result_to_task_dict`` output through ``generate_junit_xml``.

    Every other test builds rows via the synthetic ``_row`` helper, which
    hand-copies the keys the writer reads. This one runs the actual producer
    (``reports_experiment.eval_result_to_task_dict``, the batch.py path) so a
    producer-side rename of ``status`` / ``task_path`` / ``total_cost_usd`` /
    ``model_used`` / ``total_tokens`` / ``visible_turns`` / ``weighted_score``
    (RunSummary.task_results is an untyped ``list[dict[str, Any]]``) can no longer
    silently drop a property/classname or mis-bucket a row with zero failing
    assertions.
    """
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult, FinalStatus, TokenUsage
    from coder_eval.reports_experiment import eval_result_to_task_dict

    result = EvaluationResult(
        task_id="mytask",
        task_description="d",
        variant_id="v1",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 7, 21, 12, 0, 0),
        final_status=FinalStatus.FAILURE,
        iteration_count=1,
        model_used="claude-haiku-4-5-20251001",
        weighted_score=0.375,
        duration_seconds=2.5,
        total_token_usage=TokenUsage(uncached_input_tokens=100, output_tokens=50, total_cost_usd=0.02),
    )
    row = eval_result_to_task_dict(
        result,
        variant_id="v1",
        task_path="tasks/suiteA/mytask.yaml",
        replicate_index=0,
    )

    run_dir = tmp_path / "run"
    write_run_json(run_dir, [row])
    root = fromstring(generate_junit_xml(run_dir))

    # Grouped under the producer's variant_id, not "default".
    ts = _find_testsuite(root, "v1")
    assert ts is not None
    case = ts.find("testcase")

    # classname derives from the producer's task_path stem.
    assert case.get("classname") == "mytask"
    # name carries the producer's replicate_index.
    assert case.get("name") == "mytask[00]"
    # time is the producer's duration, formatted.
    assert case.get("time") == "2.500"

    # FAILURE (a "failed"-category status) becomes a <failure>, not <error>/pass.
    assert case.find("failure") is not None

    # Properties mirror the producer's values exactly (assert against the real
    # dict, not hand-copied literals — this is the key-coupling contract).
    props = _props(case)
    assert props["model_used"] == str(row["model_used"])
    assert props["weighted_score"] == str(row["weighted_score"])
    assert props["total_cost_usd"] == str(row["total_cost_usd"])
    assert props["total_tokens"] == str(row["total_tokens"])
    assert props["visible_turns"] == str(row["visible_turns"])


def test_huge_int_duration_degrades_not_crashes(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """An untyped row whose ``duration`` is a pathologically large JSON integer
    must degrade to time="0.000", not crash report generation.

    ``math.isfinite(10**400)`` and ``f"{10**400:.3f}"`` both raise OverflowError;
    a 400-digit integer survives JSON parsing into ``task_results`` (untyped
    ``list[dict[str, Any]]``), so the writer must guard the conversion.
    """
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [{"task_id": "t", "status": "SUCCESS", "duration": 10**400}])
    root = fromstring(generate_junit_xml(run_dir))  # must not raise
    case = root.find("testsuite").find("testcase")
    assert case.get("time") == "0.000"


def test_null_gating_fails_safe_as_gating(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """A schema-skewed ``"gating": null`` must be treated as gating ([FAIL]),
    not silently rendered informational — only an explicit ``false`` is INFO."""
    run_dir = tmp_path / "run"
    rows = [_row("t_fail", "FAILURE", variant_id="v1", replicate_index=0)]
    write_run_json(run_dir, rows)
    _write_task_json(
        run_dir,
        "v1",
        "t_fail",
        0,
        [
            {
                "criterion_type": "file_exists",
                "description": "must exist",
                "score": 0.0,
                "pass_threshold": 0.9,
                "gating": None,  # schema skew — must NOT read as informational
                "details": None,
                "error": None,
            }
        ],
    )
    body = _find_testsuite(fromstring(generate_junit_xml(run_dir)), "v1").find("testcase").find("failure").text or ""
    assert "[FAIL]" in body
    assert "[INFO]" not in body


def test_passing_informational_criterion_is_info_not_pass(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """An informational criterion that meets its threshold is still labelled
    [INFO] (its nature), never [PASS] (which implies it participated in the
    gate) — the label reflects gating, not the score."""
    run_dir = tmp_path / "run"
    rows = [_row("t_fail", "FAILURE", variant_id="v1", replicate_index=0)]
    write_run_json(run_dir, rows)
    _write_task_json(
        run_dir,
        "v1",
        "t_fail",
        0,
        [
            {
                "criterion_type": "commands_efficiency",
                "description": "informational, passing",
                "score": 1.0,
                "pass_threshold": 0.9,
                "gating": False,
                "details": None,
                "error": None,
            }
        ],
    )
    body = _find_testsuite(fromstring(generate_junit_xml(run_dir)), "v1").find("testcase").find("failure").text or ""
    assert "[INFO]" in body
    assert "[PASS]" not in body


def test_windows_drive_task_id_degrades_to_status_body(write_run_json: Callable[..., Path], tmp_path: Path) -> None:
    """A Windows drive-qualified task_id (``C:/x``) passes the nested-relpath shape
    on POSIX but is rejected by the drive guard, so it degrades to a status body
    instead of being resolved."""
    run_dir = tmp_path / "run"
    write_run_json(run_dir, [{"task_id": "C:/evil", "status": "FAILURE", "variant_id": "v1", "replicate_index": 0}])
    body = _find_testsuite(fromstring(generate_junit_xml(run_dir)), "v1").find("testcase").find("failure").text or ""
    assert "FAILURE" in body  # status-only fallback, no crash
