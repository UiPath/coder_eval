"""Disk-driven JUnit XML report generation from a finalized run directory.

Reads a run directory's on-disk contracts — ``run.json`` (required, the
``RunSummary`` spine), ``*/*/suite.json`` (optional suite gates), and per-failed
row ``task.json`` (optional, best-effort failure detail) — and produces a JUnit
XML string that CI test-report ingesters (GitHub ``mikepenz/action-junit-report``,
Azure DevOps ``PublishTestResults@2``, …) understand.

Single code path: ``coder-eval run --junit-xml`` and ``coder-eval report -f
junit`` both call :func:`generate_junit_xml`, so the two entry points can never
drift.

SECURITY: this module only *builds* an element tree from JSON-derived data and
serializes it — it never *parses* XML, so stdlib ``xml.etree.ElementTree`` is
safe here (XXE / billion-laughs are parser-side attacks on untrusted XML input,
which has no surface). Every string entering the tree is scrubbed of characters
outside the XML 1.0 legal set via :func:`_xml_safe`.
"""

from __future__ import annotations

import json
import logging
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from .evaluation.judge_context import truncate
from .models import FinalStatus, RunSummary, SuiteRollup
from .path_utils import replicate_subdir_name


logger = logging.getLogger(__name__)

# Characters outside XML 1.0's legal set. Kept as a plain (non-raw) ASCII-only
# string with doubled backslashes so this source file carries no literal astral
# or control characters. ``ElementTree`` will happily serialize control chars
# into *invalid* XML, so every agent-derived string is scrubbed before it enters
# the tree.
_ILLEGAL_XML = re.compile("[^\\x09\\x0A\\x0D\\x20-\\uD7FF\\uE000-\\uFFFD\\U00010000-\\U0010FFFF]")

# Per-testcase failure/error body cap (chars). Agent detail dumps can be huge.
_BODY_LIMIT = 10_000

# Serialized status values we recognize, for distinguishing a known status from a
# schema-skewed one when labelling a failure/error (classification itself goes
# through FinalStatus.category — see _category_of).
_KNOWN_STATUSES = frozenset(s.value for s in FinalStatus)


def _xml_safe(text: str) -> str:
    """Strip characters outside XML 1.0's legal set (ET handles ``<>&`` itself)."""
    return _ILLEGAL_XML.sub("", text)


def _category_of(status: str) -> Literal["succeeded", "failed", "error"]:
    """Map a serialized status string to a reporting category via the SSOT.

    Goes through ``FinalStatus(value).category`` (an explicit allowlist, CE018);
    an unknown status value (older/newer schema) falls to the error bucket
    explicitly rather than via a denylist.
    """
    try:
        return FinalStatus(status).category
    except ValueError:
        return "error"


def _set_counts(elem: ET.Element, cases: list[ET.Element]) -> None:
    """Set ``tests``/``failures``/``errors``/``skipped`` from the actual children.

    The invariant the whole writer is built around: count attributes always
    equal the emitted child elements (no separately-tracked counters that can
    drift).
    """
    elem.set("tests", str(len(cases)))
    elem.set("failures", str(sum(1 for c in cases if c.find("failure") is not None)))
    elem.set("errors", str(sum(1 for c in cases if c.find("error") is not None)))
    elem.set("skipped", str(sum(1 for c in cases if c.find("skipped") is not None)))


def _variant_of(row: dict[str, Any]) -> str:
    """Variant bucket for a row — ``str``-coerced, ``None``/empty → ``"default"``.

    Rows are untyped dicts, so a non-string ``variant_id`` must not reach a dict
    key or an XML attribute as a non-``str``.
    """
    return str(row.get("variant_id") or "default")


def _status_of(row: dict[str, Any]) -> str:
    """Row status as a string; an absent key reads as ``"<missing>"``, not ``"None"``."""
    raw = row.get("status")
    return str(raw) if raw is not None else "<missing>"


def _is_safe_component(value: str) -> bool:
    """True when ``value`` is usable as a single, contained path segment.

    ``run.json`` rows are untyped and may be blob-pulled from elsewhere, so a
    crafted ``variant_id`` must not steer the lookup outside the run directory
    (an absolute value would discard ``run_dir`` entirely, and ``..`` would walk
    up).
    """
    return bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value


def _is_safe_relpath(value: str) -> bool:
    """True when ``value`` is a contained relative path — possibly *nested*.

    Unlike :func:`_is_safe_component`, an internal ``/`` is allowed: dataset
    expansion rewrites a row's ``task_id`` to ``"<suite>/<row_id>"``
    (``task_loader.expand_dataset``) and the on-disk layout is correspondingly
    nested (``run_dir/<variant>/<suite>/<row_id>/<NN>/task.json``, see
    ``path_utils.build_task_run_dir``). Rejecting the ``/`` would degrade every
    dataset-derived row (e.g. the activation suite) to a status-only body.

    Only an absolute path, a Windows drive/UNC prefix, or a ``.``/``..``
    component could escape ``run_dir``; all are rejected here, and the
    ``resolve()``-containment check in :func:`_load_task_json` is the
    belt-and-braces backstop (symlinks included).
    """
    # A Windows drive-qualified value (``C:/x``) reads as a plain relative path
    # on POSIX but is absolute on Windows, so reject it explicitly rather than
    # leaning on the resolve-containment backstop alone.
    if not value or value.startswith("/") or "\\" in value or PureWindowsPath(value).drive:
        return False
    parts = PurePosixPath(value).parts
    return bool(parts) and all(p not in {".", ".."} for p in parts)


def _time_attr(value: Any) -> str:
    """Serialize a duration as a JUnit ``time`` attribute string.

    The value must be a finite, non-negative number; NaN/inf/negative/non-numeric
    all fall back to ``"0.000"``. NaN/inf would otherwise serialize as
    ``"nan"``/``"inf"`` and make the document invalid for JUnit ingesters. Shared
    by the per-testcase time and the root ``<testsuites>`` time so both are
    guarded identically.

    Rows are untyped, so ``value`` may be a pathologically large JSON integer
    (hundreds of digits) that overflows on the ``float()`` conversion / ``.3f``
    format — the ``OverflowError`` is caught and degraded rather than aborting
    the whole report, matching this module's degrade-don't-crash contract.
    """
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return "0.000"
    try:
        as_float = float(value)
    except (OverflowError, ValueError):
        return "0.000"
    return f"{as_float:.3f}" if math.isfinite(as_float) else "0.000"


def _load_task_json(run_dir: Path, row: dict[str, Any], variant: str) -> dict[str, Any] | None:
    """Best-effort load of a failed row's ``task.json`` as a plain dict.

    Plain-dict access (not ``EvaluationResult.model_validate``) keeps the writer
    tolerant of schema skew in blob-pulled/older runs and avoids materializing
    the large ``turns`` array. Any problem — unsafe path component, missing
    dir/file, undecodable bytes, bad JSON, or an ambiguous replicate — yields
    ``None`` so the caller falls back to a status-only body.
    """
    task_id = str(row.get("task_id", "<unknown>"))
    if not _is_safe_component(variant) or not _is_safe_relpath(task_id):
        return None

    replicate_index = row.get("replicate_index")
    task_dir = run_dir / variant / task_id
    if isinstance(replicate_index, int) and not isinstance(replicate_index, bool):
        candidate = task_dir / replicate_subdir_name(replicate_index) / "task.json"
    else:
        matches = sorted(task_dir.glob("*/task.json"))
        # With no replicate index, picking one of several would misattribute
        # another replicate's failure detail to this row — degrade instead.
        if len(matches) > 1:
            return None
        candidate = matches[0] if matches else task_dir / "task.json"

    try:
        # Belt-and-braces containment check (catches symlink escapes too).
        if not candidate.resolve().is_relative_to(run_dir.resolve()):
            return None
        # ValueError covers both json.JSONDecodeError and UnicodeDecodeError
        # (undecodable bytes) — neither may abort report generation.
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _criterion_lines(criteria: list[Any], *, diagnostic: bool = False) -> list[str]:
    """Render criterion rows without treating unavailable evidence as a failed check."""
    lines: list[str] = []
    for crit in criteria:
        if not isinstance(crit, dict):
            continue
        ctype = str(crit.get("criterion_type", "unknown"))
        description = str(crit.get("description", ""))
        evaluation_status = crit.get("evaluation_status", "evaluated")
        detail = crit.get("details") or crit.get("error")
        if evaluation_status == "not_evaluated":
            lines.append(f"[NOT EVALUATED] {ctype}: {description}")
            if detail:
                lines.append(str(detail))
            continue

        score = crit.get("score")
        threshold = crit.get("pass_threshold")
        score_str = f"{score:.2f}" if isinstance(score, int | float) else str(score)
        passed = isinstance(score, int | float) and isinstance(threshold, int | float) and score >= threshold
        if diagnostic:
            label = "DIAGNOSTIC PASS" if passed else "DIAGNOSTIC FAIL"
            lines.append(f"[{label}] {ctype}: score {score_str} — {description}")
            if detail:
                lines.append(str(detail))
            continue

        # Only an explicit JSON ``false`` marks an informational criterion; a
        # missing key or a schema-skewed value fails safe to gating.
        informational = crit.get("gating", True) is False
        if informational:
            lines.append(f"[INFO] {ctype}: score {score_str} — {description}")
            continue
        if passed:
            lines.append(f"[PASS] {ctype}: {description}")
            continue
        threshold_str = f"{threshold:.2f}" if isinstance(threshold, int | float) else str(threshold)
        lines.append(f"[FAIL] {ctype}: score {score_str} < threshold {threshold_str} — {description}")
        if detail:
            lines.append(str(detail))
    return lines


def _criteria_body(row: dict[str, Any], run_dir: Path, variant: str) -> str:
    """Build the failure/error body for a non-succeeded row.

    Prefers per-criterion lines from the row's ``task.json``; falls back to a
    status + weighted-score line when task.json is missing/corrupt. Capped via
    the shared ``truncate``.
    """
    status = _status_of(row)
    data = _load_task_json(run_dir, row, variant)
    criteria = data.get("success_criteria_results") if isinstance(data, dict) else None
    post_failure = data.get("post_failure_criteria_results") if isinstance(data, dict) else None

    lines: list[str] = []
    if isinstance(criteria, list) and criteria:
        lines.extend(_criterion_lines(criteria))
    else:
        weighted = row.get("weighted_score")
        weighted_str = f"{weighted:.2f}" if isinstance(weighted, int | float) else str(weighted)
        lines.append(f"status={status} weighted_score={weighted_str}")

    if isinstance(post_failure, list) and post_failure:
        lines.extend(
            [
                "",
                "Post-failure artifact evidence (diagnostic only; does not affect status or weighted score):",
                *_criterion_lines(post_failure, diagnostic=True),
            ]
        )

    return _xml_safe(truncate("\n".join(lines), _BODY_LIMIT))


def _task_case(row: dict[str, Any], run_dir: Path) -> ET.Element:
    """Build one ``<testcase>`` for a task row."""
    variant = _variant_of(row)
    task_id = str(row.get("task_id", "<unknown>"))

    # `bool` is an int subclass — exclude it so True never renders as "[01]".
    #
    # The guard is spelled inline rather than through a `has_replicate` flag so it NARROWS: the
    # row is an untyped dict, and `replicate_subdir_name` takes an `int`. Behind a bool variable
    # pyright still sees `Any | bool | int | None` at the call and rejects it — the same widening
    # the old hand-rolled f-string hid by accepting anything at all.
    replicate_index = row.get("replicate_index")
    name = (
        f"{task_id}[{replicate_subdir_name(replicate_index)}]"
        if isinstance(replicate_index, int) and not isinstance(replicate_index, bool)
        else task_id
    )

    task_path = row.get("task_path")
    classname = (Path(task_path).stem or variant) if isinstance(task_path, str) and task_path else variant

    time_str = _time_attr(row.get("duration"))

    case = ET.Element(
        "testcase",
        {"name": _xml_safe(name), "classname": _xml_safe(classname), "time": time_str},
    )

    # Properties — emit only non-None values (no "None" strings in the tree).
    prop_specs = [
        ("model_used", row.get("model_used")),
        ("weighted_score", row.get("weighted_score")),
        ("total_cost_usd", row.get("total_cost_usd")),
        ("total_tokens", row.get("total_tokens")),
        ("visible_turns", row.get("visible_turns")),
    ]
    props = [(k, v) for k, v in prop_specs if v is not None]
    if props:
        properties = ET.SubElement(case, "properties")
        for key, value in props:
            ET.SubElement(properties, "property", {"name": key, "value": _xml_safe(str(value))})

    status = _status_of(row)
    category = _category_of(status)
    if category == "succeeded":
        return case

    message = status if status in _KNOWN_STATUSES else f"unknown status: {status}"
    tag = "failure" if category == "failed" else "error"
    child = ET.SubElement(case, tag, {"message": _xml_safe(message)})
    child.text = _criteria_body(row, run_dir, variant)
    return case


def _skipped_name(path: str) -> str:
    """Stable testcase name for a skipped task: suffix-stripped, ``/``-separated.

    Uses the whole path rather than just the stem, because two skipped tasks
    sharing a basename (``suiteA/task.yaml``, ``suiteB/task.yaml``) would
    otherwise collapse into one identity that some JUnit ingesters merge.

    Separators are normalized to ``/`` and the path is parsed with
    ``PurePosixPath`` so the emitted name does not depend on the OS that
    generated the report — the same logical run must produce the same testcase
    identity on Windows and Linux, or CI history/flake tracking splits in two.
    """
    return str(PurePosixPath(path.replace("\\", "/")).with_suffix(""))


def _skipped_suite(summary: RunSummary) -> ET.Element | None:
    """Build the synthetic ``skipped`` testsuite from ``RunSummary.skipped_tasks``."""
    if not summary.skipped_tasks:
        return None
    suite = ET.Element("testsuite", {"name": "skipped"})
    cases: list[ET.Element] = []
    for entry in summary.skipped_tasks:
        case = ET.SubElement(
            suite,
            "testcase",
            {"name": _xml_safe(_skipped_name(entry.path)), "classname": "skipped"},
        )
        ET.SubElement(case, "skipped", {"message": _xml_safe(entry.reason)})
        cases.append(case)
    _set_counts(suite, cases)
    return suite


def _suite_gate_suite(run_dir: Path) -> ET.Element | None:
    """Build the synthetic ``suite-gates`` testsuite from ``*/*/suite.json``.

    A suite.json that fails ``SuiteRollup`` validation is skipped with a warning
    (schema skew must not kill report generation). Returns ``None`` when no valid
    rollup is found.
    """
    rollups: list[SuiteRollup] = []
    for path in sorted(run_dir.glob("*/*/suite.json")):
        try:
            rollups.append(SuiteRollup.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            logger.warning("Skipping unparseable suite rollup %s: %s", path, e)
    if not rollups:
        return None

    suite = ET.Element("testsuite", {"name": "suite-gates"})
    cases: list[ET.Element] = []
    for rollup in rollups:
        case = ET.SubElement(
            suite,
            "testcase",
            {"name": _xml_safe(f"{rollup.variant_id}/{rollup.suite_id}"), "classname": "suite-gates"},
        )
        cases.append(case)
        if rollup.passed:
            continue
        lines: list[str] = []
        for agg in rollup.criterion_aggregates:
            for check in agg.threshold_checks:
                if check.passed:
                    continue
                if check.actual_value is None:
                    lines.append(f"{check.metric}: metric not emitted (min {check.min_value})")
                else:
                    lines.append(f"{check.metric}: {check.actual_value} < {check.min_value}")
            if agg.error:
                lines.append(f"{agg.criterion_type}: {agg.error}")
        failure = ET.SubElement(case, "failure", {"message": "suite thresholds not met"})
        failure.text = _xml_safe(truncate("\n".join(lines), _BODY_LIMIT))
    _set_counts(suite, cases)
    return suite


def generate_junit_xml(run_dir: Path) -> str:
    """Read ``run_dir`` and return a JUnit XML string.

    Args:
        run_dir: A finalized run directory containing ``run.json``.

    Returns:
        JUnit XML with an ``<?xml ...?>`` declaration.

    Raises:
        FileNotFoundError: When ``run.json`` is absent (not a finalized run dir).
        pydantic.ValidationError: When ``run.json`` fails ``RunSummary`` validation
            (the spine contract is broken; a fabricated report would be worse).
    """
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        raise FileNotFoundError(f"No run.json in {run_dir} — not a finalized run directory (see 'coder-eval run')")
    summary = RunSummary.model_validate_json(run_json.read_text(encoding="utf-8"))

    root = ET.Element("testsuites", {"name": _xml_safe(summary.run_id)})
    # Guard the root time identically to per-testcase time: a corrupt/blob-pulled
    # run.json can carry a NaN/inf total_duration_seconds (RunSummary has no
    # finite validator), which would emit an invalid time="nan".
    root.set("time", _time_attr(summary.total_duration_seconds))

    # Group task rows by variant, preserving first-seen order.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in summary.task_results:
        grouped.setdefault(_variant_of(row), []).append(row)

    all_cases: list[ET.Element] = []
    for variant, rows in grouped.items():
        suite = ET.SubElement(root, "testsuite", {"name": _xml_safe(variant)})
        cases = [_task_case(row, run_dir) for row in rows]
        for case in cases:
            suite.append(case)
        _set_counts(suite, cases)
        all_cases.extend(cases)

    skipped_suite = _skipped_suite(summary)
    if skipped_suite is not None:
        root.append(skipped_suite)
        all_cases.extend(skipped_suite.findall("testcase"))

    gate_suite = _suite_gate_suite(run_dir)
    if gate_suite is not None:
        root.append(gate_suite)
        all_cases.extend(gate_suite.findall("testcase"))

    _set_counts(root, all_cases)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def write_junit_xml(run_dir: Path, output_path: Path) -> Path:
    """Generate JUnit XML for ``run_dir`` and write it to ``output_path``.

    Thin persist wrapper mirroring the ``build_run_summary``/``write_run_summary``
    seam. Creates parent directories as needed.

    Returns:
        The path written.
    """
    xml = generate_junit_xml(run_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(xml, encoding="utf-8")
    return output_path
