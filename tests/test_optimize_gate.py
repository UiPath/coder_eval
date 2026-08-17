"""Unit tests for the activation track's promotion gate (`coder_eval.optimize_gate`).

Fixtures write real ``task.json`` files into a ``tmp_path`` run-dir tree, so the loader is
exercised against the on-disk contract rather than against a mock of it.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import random
import re
import shutil
import textwrap
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from unittest import mock

import pytest
from pydantic import ValidationError

from coder_eval import optimize_gate
from coder_eval.leak_detection import LEAK_MIN_CHARS
from coder_eval.models import (
    ActivationGateVerdict,
    ArmRowScores,
    ClassificationCriterionResult,
    CriterionResult,
    EvaluationResult,
    ExecutionGateVerdict,
    ExperimentResult,
    FileCheckCriterion,
    FileExistsCriterion,
    FinalStatus,
    GuardrailCheck,
    NoiseFloor,
    OptimizeMeasurements,
    RegressionRow,
    ResolvedTask,
    RoundScores,
    RowSelection,
    RunSummary,
    SkillTriggeredCriterion,
    TaskDefinition,
    TokenUsage,
    copy_with,
)
from coder_eval.optimize_gate import (
    _NOTE_OUTSIDE_FAMILY,
    GATE_MAX_FAMILY,
    GATE_P_PRECISION,
    GATE_RESAMPLES,
    MATERIALITY_FLOOR,
    TASK_JSON_GLOB,
    CostQualityPoint,
    SearchComparison,
    SplitProvenance,
    _activation_preflight,
    _balance_pair,
    _discreteness_floor,
    _execution_diagnostics,
    _holm_family,
    _holm_threshold,
    _label_pairs,
    _load_and_pair,
    _median,
    _note_holm_family,
    _note_ordinary_negative,
    _refusal_message,
    _row_cost_levels,
    _row_costs,
    _sibling_checks,
    _wrong_path_reason,
    activation_gate,
    arm_row_scores,
    candidate_leaks,
    cost_latency_guardrails,
    cost_quality_front,
    cost_quality_points,
    derive_sibling_indices,
    execution_gate,
    holm_promote,
    holm_promote_execution,
    instance_best_front,
    lineage_head_scores,
    load_arm_rows,
    load_suite_rows,
    measure_execution_noise_floor,
    measure_noise_floor,
    min_discordant_rows,
    noise_floor_mde,
    pareto_front,
    read_split_provenance,
    regression_check,
    resolve_model,
    search_compare,
)
from coder_eval.optimize_store import UNRECORDED_SPLIT, record_noise_floor
from coder_eval.reports_optimize import (
    COST_FRONT_ADVISORY,
    _front_summary,
    _render_checks,
    render_cost_quality,
    render_execution_markdown,
    render_markdown,
    render_row_matrix,
    render_search_comparison,
)
from coder_eval.reports_stats import (
    BOOTSTRAP_RESAMPLES,
    DEFAULT_ALPHA,
    PairedComparison,
    bootstrap_p_floor,
    holm_rejections,
)
from tests.lint.import_resolution import resolved_module


SUITE = "my-skill-activation"


def _eval_result(row_id: str, labels: list[tuple[str, str]], *, extra_basic: bool = False) -> EvaluationResult:
    """One replicate's result carrying one classification result per (expected, observed) pair.

    Descriptions interpolate the row id, exactly as both bundled templates do — which is what
    makes a description-keyed implementation impossible and the positional one necessary.
    """
    results: list[CriterionResult] = [
        ClassificationCriterionResult(
            criterion_type="skill_triggered",
            description=f"criterion {i} for row {row_id}",
            score=1.0 if expected == observed else 0.0,
            expected_label=expected,
            observed_label=observed,
        )
        for i, (expected, observed) in enumerate(labels)
    ]
    if extra_basic:
        results.append(CriterionResult(criterion_type="file_check", description=f"file for row {row_id}", score=1.0))
    return EvaluationResult(
        task_id=f"{SUITE}/{row_id}",
        task_description="row",
        agent_type="claude-code",
        started_at=datetime(2026, 8, 13, 12, 0, 0),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        success_criteria_results=results,  # type: ignore[arg-type]
    )


# The run.json key the gate reads, taken from the model that declares it rather than typed again.
_RUN_SELECTION_KEY = next(name for name in RunSummary.model_fields if name == "row_selection")


def _write_run_provenance(run_dir: Path, split: str | None = None) -> None:
    """Stamp a minimal ``run.json`` carrying the run's row selection.

    Every arm builder goes through ``_write_row``, which calls this, so fixtures model a run
    directory written by a CURRENT coder-eval rather than a pre-provenance one. Without it every
    fixture takes the "unrecorded" path: the gate would note it on every block (churning six
    pinned renders) and — the part that actually bites — ``record_noise_floor`` would refuse to
    cache any floor, since a floor measured over runs whose row sets are unknown is a floor for
    no particular row set. The unrecorded and mismatched paths get their own explicit tests
    instead of contaminating every other one.

    Only the keys the gate reads are written; ``RunSummary`` has many more, and a fixture that
    tracked all of them would be a second implementation of the writer.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "run.json"
    if path.exists():
        return
    # Built from the REAL models rather than hand-typed keys. `read_split_provenance` hand-writes
    # its READER, and this hand-wrote its WRITER — so renaming `RunSummary.row_selection` or
    # `RowSelection.split` would have moved both in lockstep, leaving every test green while
    # production went 100% "unrecorded". Deriving the payload here is what breaks that symmetry.
    payload = {_RUN_SELECTION_KEY: RowSelection(split=split).model_dump(mode="json"), "task_results": []}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _record_task_result(run_dir: Path, variant: str, task_id: str, replicate: int) -> None:
    """Append one executed row to ``run.json``'s ``task_results``, as a real run does.

    The tree-reconciliation preflight asks whether run.json describes the rows on disk, so a
    fixture that writes rows without recording them models a CONTAMINATED run dir. Every builder
    goes through ``_write_row``, which calls this, so the ordinary fixture is a clean one and the
    contaminated case is built deliberately (``_write_row(..., record=False)``).

    Only the three keys the reconciliation reads, on the same "do not re-implement the writer"
    grounds as ``_write_run_provenance`` above. ``replicate_index`` is load-bearing: the
    reconciliation keys on ``(row, replicate)``, so a fixture omitting it would send every entry
    down the permissive whole-row path and leave the replicate half of the check untested.
    """
    path = run_dir / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("task_results", []).append(
        {"task_id": task_id, "variant_id": variant, "replicate_index": replicate}
    )
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_row(
    run_dir: Path,
    variant: str,
    row_id: str,
    result: EvaluationResult,
    replicate: int = 0,
    *,
    record: bool = True,
) -> Path:
    """Write one replicate's ``task.json``, recording it in ``run.json`` unless told not to.

    ``record=False`` writes the row to disk WITHOUT recording it — a row left behind by an earlier
    invocation of a re-used ``--run-dir``, which is exactly what the tree-reconciliation preflight
    refuses on.
    """
    task_dir = run_dir / variant / SUITE / row_id / f"{replicate:02d}"
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "task.json"
    path.write_text(result.model_dump_json(), encoding="utf-8")
    _write_run_provenance(run_dir)
    if record:
        _record_task_result(run_dir, variant, f"{SUITE}/{row_id}", replicate)
    return path


def _write_arm(
    tmp_path: Path,
    variant: str,
    per_row_labels: dict[str, list[tuple[str, str]]],
    *,
    invocations: int = 3,
    prefix: str = "",
) -> list[Path]:
    """One arm across ``invocations`` separate run directories — Stage B's three invocations."""
    run_dirs: list[Path] = []
    for i in range(invocations):
        run_dir = tmp_path / f"{prefix}run-{i}"
        for row_id, labels in per_row_labels.items():
            _write_row(run_dir, variant, row_id, _eval_result(row_id, labels))
        run_dirs.append(run_dir)
    return run_dirs


def _shared_dirs(
    tmp_path: Path,
    incumbent: dict[str, list[tuple[str, str]]],
    candidate: dict[str, list[tuple[str, str]]],
    *,
    invocations: int = 3,
) -> list[Path]:
    """Both arms written into the SAME run directories, as a real experiment produces them."""
    run_dirs: list[Path] = []
    for i in range(invocations):
        run_dir = tmp_path / f"run-{i}"
        for row_id, labels in incumbent.items():
            _write_row(run_dir, "incumbent", row_id, _eval_result(row_id, labels))
        for row_id, labels in candidate.items():
            _write_row(run_dir, "candidate", row_id, _eval_result(row_id, labels))
        run_dirs.append(run_dir)
    return run_dirs


# The gate's real default is GATE_RESAMPLES (20,000), which is what a promotion decision needs and
# what ~80 tests in this file do NOT: at four bootstraps per call it would take the file from ~3s to
# ~35s. So the helper passes a small count, resample-sensitive tests pass their own, and
# `test_gate_defaults_to_the_gate_resample_count` is the one test that exercises the signature.
_FAST_RESAMPLES = 400


def _gate(run_dirs: list[Path], **kwargs) -> ActivationGateVerdict:
    return activation_gate(
        incumbent_run_dirs=run_dirs,
        candidate_run_dirs=run_dirs,
        incumbent_variant="incumbent",
        candidate_variant="candidate",
        suite_id=SUITE,
        **{"criterion_index": 0, "n_resamples": _FAST_RESAMPLES, **kwargs},
    )


class TestLoadSuiteRows:
    def test_reads_all_replicate_dirs(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]}, invocations=3)
        assert [len(load_suite_rows(d, "incumbent", SUITE)["r1"]) for d in run_dirs] == [1, 1, 1]
        # Pooled across the three invocations, the row carries three replicates.
        assert len(load_arm_rows(run_dirs, "incumbent", SUITE)["r1"]) == 3

    def test_skips_malformed_task_json(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dir = tmp_path / "run-0"
        _write_row(run_dir, "incumbent", "good", _eval_result("good", [("yes", "yes")]))
        bad = _write_row(run_dir, "incumbent", "bad", _eval_result("bad", [("yes", "yes")]))
        bad.write_text('{"task_id": "truncated"', encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            rows = load_suite_rows(run_dir, "incumbent", SUITE)
        assert set(rows) == {"good"}
        assert "Failed to load" in caplog.text

    def test_missing_variant_dir_returns_empty(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-0"
        _write_row(run_dir, "incumbent", "r1", _eval_result("r1", [("yes", "yes")]))
        assert load_suite_rows(run_dir, "typo-variant", SUITE) == {}
        assert load_suite_rows(run_dir, "incumbent", "typo-suite") == {}

    def test_the_loader_reads_a_differently_padded_replicate_dir(self, tmp_path: Path) -> None:
        """The day `replicate_subdir_name` widens to NNN, this loader must still find the rows.

        The pre-CE042 glob pinned `[0-9][0-9]`, so it would have matched NOTHING — both gates load
        zero rows and the zero-row note blames a wrong variant id, a wrong suite id or a wrong run
        directory, which are the three things that would be correct.
        """
        run_dir = tmp_path / "run-0"
        task_dir = run_dir / "incumbent" / SUITE / "r1" / "000"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(_eval_result("r1", [("yes", "yes")]).model_dump_json(), encoding="utf-8")
        assert list(load_suite_rows(run_dir, "incumbent", SUITE)) == ["r1"]


class TestEveryWrongPathMessageDerivesFromTheGlob:
    """The four messages that tell a reader what did not match are built from `TASK_JSON_GLOB`.

    They used to spell the pattern as a literal `*/NN/task.json`, in four places, none of which
    was the glob the loader actually ran. Changing the glob left all four describing a tree the
    code no longer searched — and these are the messages a reader consults precisely when the
    path is what is wrong.
    """

    def test_the_activation_zero_row_note_derives(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        verdict = activation_gate(
            incumbent_run_dirs=run_dirs,
            candidate_run_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variant="typo",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=_FAST_RESAMPLES,
        )
        note = next(n for n in verdict.notes if "loaded ZERO rows" in n)
        assert TASK_JSON_GLOB in note

    def test_the_execution_zero_row_refusal_derives(self, tmp_path: Path) -> None:
        # Spelled with the literal `<variant>` because this one message names BOTH arms.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        shutil.rmtree(run_dir / "incumbent")
        verdict = _exec_gate(run_dir)
        assert verdict.gate_refusal is not None
        assert f"<run>/<variant>/{EXEC_SUITE}/{TASK_JSON_GLOB}" in verdict.gate_refusal

    def test_the_activation_floor_reason_derives(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with caplog.at_level(logging.WARNING):
            assert (
                measure_noise_floor(run_dirs=run_dirs, variant_id="typo", suite_id=SUITE, criterion_index=0, model="m")
                is None
            )
        assert TASK_JSON_GLOB in caplog.text

    def test_the_execution_floor_reason_derives(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with caplog.at_level(logging.WARNING):
            assert (
                measure_execution_noise_floor(run_dirs=run_dirs, variant_id="typo", suite_id=SUITE, model="m") is None
            )
        assert TASK_JSON_GLOB in caplog.text

    def test_no_message_spells_the_padding_itself(self) -> None:
        # The whole point of the seam: a message may name the glob, never the padding it replaced.
        source = (Path(__file__).parent.parent / "src" / "coder_eval" / "optimize_gate.py").read_text(encoding="utf-8")
        assert "*/NN/task.json" not in source
        assert TASK_JSON_GLOB == "*/*/task.json"


class TestBalancePair:
    """The per-row replicate trim, which was spelled three times in three shapes."""

    def test_equal_lengths_pass_through(self) -> None:
        assert _balance_pair([1.0, 2.0], [3.0, 4.0]) == ([1.0, 2.0], [3.0, 4.0])

    def test_a_longer_incumbent_trims_to_the_candidate(self) -> None:
        assert _balance_pair([1.0, 2.0, 3.0], [4.0]) == ([1.0], [4.0])

    def test_a_longer_candidate_trims_to_the_incumbent(self) -> None:
        assert _balance_pair([1.0], [4.0, 5.0, 6.0]) == ([1.0], [4.0])

    def test_an_empty_side_yields_two_empty_lists(self) -> None:
        # The row is then dropped by whichever caller's own emptiness rule applies — a different
        # question from balancing, and deliberately not this function's to answer.
        assert _balance_pair([], [1.0, 2.0]) == ([], [])
        assert _balance_pair([1.0, 2.0], []) == ([], [])

    def test_it_is_generic_over_the_element_type(self) -> None:
        # Both real element types: the guardrail trims floats, the F1 and sibling paths trim
        # label pairs. A signature that only served one would have left two of the three sites.
        pairs = [("yes", "yes"), ("no", "no"), ("yes", "no")]
        assert _balance_pair(pairs, pairs[:1]) == ([("yes", "yes")], [("yes", "yes")])
        assert _balance_pair([0.1, 0.2, 0.3], [0.4, 0.5]) == ([0.1, 0.2], [0.4, 0.5])


def test_the_trim_is_declared_once() -> None:
    """`min(len(a), len(b))` may appear in exactly one function: `_balance_pair`.

    Counts call NODES, mirroring `TestHolmRejectionsIsConfined`. The trim was spelled three times
    in three shapes and only one of the three surfaced the dropped observations to the user, which
    is precisely how a re-weighted comparison stays invisible.

    `measure_execution_noise_floor`'s `min(len(values) for values in replicated)` is explicitly
    allowed: a GENERATOR argument rather than two `len` arguments, and a minimum ACROSS rows rather
    than between two arms of one row — a different computation that stays separate.
    """
    source = (Path(__file__).parent.parent / "src" / "coder_eval" / "optimize_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "min"):
                continue
            two_lens = len(node.args) == 2 and all(
                isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "len" for a in node.args
            )
            if two_lens and function.name != "_balance_pair":
                offenders.append(f"{function.name}:{node.lineno}")
    assert not offenders, f"a per-arm observation trim outside _balance_pair: {offenders}"


class TestBothTracksEmitTheSameHolmNotes:
    """The four notes both wrappers emit are one declaration, not two byte-identical copies.

    They lived 600 lines apart, so a wording fix applied to one would have left the two tracks
    describing the same decision differently in a ledger read back weeks later.
    """

    def test_the_ordinary_negative_note_is_identical_across_tracks(self, tmp_path: Path) -> None:
        # Same p, same family size, same alpha on both tracks — so any difference in the produced
        # string is a difference in the DECLARATION, which is what this pins.
        activation = _note_ordinary_negative(0.4321, 3, DEFAULT_ALPHA)
        assert activation == _note_ordinary_negative(0.4321, 3, DEFAULT_ALPHA)
        assert "0.4321" in activation and "family of 3" in activation

    def test_both_wrappers_emit_the_shared_declarations(self, tmp_path: Path) -> None:
        # Through the real wrappers, not the constants: a call site that kept its own copy would
        # still produce an equal string today, so the source scan below is what makes this tight.
        incumbent = {f"r{i}": [("yes", "no" if i else "yes")] for i in range(6)}
        activation = holm_promote([_gate(_shared_dirs(tmp_path, incumbent, incumbent))])[0]
        execution = holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path, **_WINNER))])[0]
        for verdict in (activation, execution):
            assert _note_holm_family(1, DEFAULT_ALPHA) in verdict.notes

    def test_neither_wrapper_respells_a_shared_note(self) -> None:
        source = (Path(__file__).parent.parent / "src" / "coder_eval" / "optimize_gate.py").read_text(encoding="utf-8")
        for fragment in (
            "the sample could not support a p-value",
            "the Holm-corrected test rejects but the confidence",
            "did not clear the Holm threshold for its rank",
            "Holm applied across a family of",
        ):
            assert source.count(fragment) == 1, f"{fragment!r} is declared more than once"


class TestLabelPairs:
    def test_selects_by_position(self, tmp_path: Path) -> None:
        # Two stacked skill_triggered criteria whose descriptions BOTH interpolate the row id,
        # mirroring the shipped templates. A description-keyed implementation fails this.
        results = [_eval_result("r1", [("yes", "yes"), ("no", "yes")])]
        assert _label_pairs(results, 0) == [("yes", "yes")]
        assert _label_pairs(results, 1) == [("no", "yes")]

    def test_skips_rows_with_too_few_results(self) -> None:
        results = [_eval_result("r1", [("yes", "yes")])]
        assert _label_pairs(results, 5) == []

    def test_skips_non_classification_results(self) -> None:
        results = [_eval_result("r1", [("yes", "yes")], extra_basic=True)]
        assert _label_pairs(results, 1) == []


class TestActivationGate:
    def _clear_win(self) -> tuple[dict, dict]:
        # 12 positive rows: the incumbent engages on 3, the candidate on all 12.
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}
        return incumbent, candidate

    def test_promotes_a_clearly_better_candidate(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.rows_paired == 12
        assert verdict.ci_low is not None and verdict.ci_low > 0.0
        assert holm_promote([verdict])[0].promoted is True

    def test_leaves_promoted_none_before_holm(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        assert _gate(_shared_dirs(tmp_path, incumbent, candidate)).promoted is None

    def test_refuses_a_tied_candidate(self, tmp_path: Path) -> None:
        rows = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(12)}
        verdict = _gate(_shared_dirs(tmp_path, rows, dict(rows)))
        assert verdict.mean_diff == 0.0
        assert verdict.ci_low is not None and verdict.ci_high is not None
        assert verdict.ci_low <= 0.0 <= verdict.ci_high
        assert holm_promote([verdict])[0].promoted is False

    def test_reports_range_non_overlap_as_diagnostic_only(self, tmp_path: Path) -> None:
        # 4 rows, candidate strictly ahead on every invocation (ranges do not overlap) but too
        # few clusters for the interval to exclude zero.
        incumbent = {f"r{i}": [("yes", "no")] for i in range(4)}
        candidate = {"r0": [("yes", "yes")], "r1": [("yes", "no")], "r2": [("yes", "no")], "r3": [("yes", "no")]}
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.range_non_overlap is True
        assert verdict.ci_low is not None and verdict.ci_low <= 0.0
        assert holm_promote([verdict])[0].promoted is False

    def test_excludes_unpaired_rows_and_counts_them(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "yes")] for i in range(5)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(4)}  # r4 missing on the candidate
        candidate["extra"] = [("yes", "yes")]
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.rows_paired == 4
        assert verdict.rows_excluded == 2
        assert any("only one arm" in note for note in verdict.notes)

    def test_a_row_scored_on_only_one_arm_is_excluded_from_both(self, tmp_path: Path) -> None:
        """The asymmetry that promotes a candidate for CRASHING on the rows it would have missed.

        A row that times out or errors is written with an empty ``success_criteria_results`` —
        the row directory exists, so it pairs, but it contributes a pair to only one arm. Left
        in, the two arms' F1s are computed over different row sets, and the bias runs toward
        the candidate: here the candidate "wins" 1.000 vs 0.667 purely by producing nothing on
        the six rows it was failing.
        """
        incumbent = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(12)}
        candidate = {f"r{i}": ([("yes", "yes")] if i % 2 else []) for i in range(12)}
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate))

        assert verdict.rows_paired == 6
        assert verdict.rows_excluded == 6
        assert verdict.incumbent_f1 == verdict.candidate_f1 == 1.0
        assert verdict.mean_diff == 0.0
        assert holm_promote([verdict])[0].promoted is False
        assert any("scored on only one arm" in note for note in verdict.notes)

    def test_with_fewer_than_two_paired_rows_returns_none_stats(self, tmp_path: Path) -> None:
        verdict = _gate(_shared_dirs(tmp_path, {"r0": [("yes", "yes")]}, {"r0": [("yes", "yes")]}))
        assert verdict.rows_paired == 1
        assert (verdict.mean_diff, verdict.ci_low, verdict.ci_high, verdict.p_value) == (None, None, None, None)
        assert verdict.promoted is False
        assert any("fewer than the 2" in note for note in verdict.notes)

    def test_a_wrong_path_yields_zero_rows_and_never_raises(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        run_dirs = _shared_dirs(tmp_path, incumbent, candidate)
        verdict = activation_gate(
            incumbent_run_dirs=run_dirs,
            candidate_run_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id="a-suite-that-does-not-exist",
            criterion_index=0,
        )
        assert verdict.rows_paired == 0
        assert verdict.promoted is False
        # A mistyped path must be LOUD in the verdict: "0 rows" alone reads identically to a
        # genuinely tiny suite, which is the silent-zero conflation this note exists to break.
        note = " ".join(verdict.notes)
        assert "loaded ZERO rows" in note
        assert "a-suite-that-does-not-exist" in note
        assert "not a result" in note

    def test_a_wrong_variant_id_names_the_arm_that_found_nothing(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        run_dirs = _shared_dirs(tmp_path, incumbent, candidate)
        verdict = activation_gate(
            incumbent_run_dirs=run_dirs,
            candidate_run_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variant="typo-variant",
            suite_id=SUITE,
            criterion_index=0,
        )
        note = " ".join(verdict.notes)
        assert "the candidate arm loaded ZERO rows" in note
        assert "the incumbent arm loaded ZERO rows" not in note

    def test_bad_criterion_index_is_noted_not_raised(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate), criterion_index=7)
        # Every row exists but none is scored at that position, so nothing is comparable.
        assert (verdict.rows_paired, verdict.rows_excluded) == (0, 12)
        note = " ".join(verdict.notes)
        assert "criterion_index=7" in note
        assert "POSITION" in note
        # Distinguishable from "the skill never fired", which yields pairs with observed='no'.
        assert "never firing" in note
        assert holm_promote([verdict])[0].promoted is False

    def test_negative_criterion_index_raises_rather_than_grading_the_last_criterion(self, tmp_path: Path) -> None:
        # The lower bound the internal `>= len(...)` guards cannot see. Selection is POSITIONAL,
        # so -1 does not fail — it grades the LAST criterion on every row and returns a confident
        # number for a criterion nobody asked about.
        incumbent, candidate = self._clear_win()
        with pytest.raises(ValueError, match="criterion_index must be >= 0, got -1"):
            _gate(_shared_dirs(tmp_path, incumbent, candidate), criterion_index=-1)

    def test_criterion_index_zero_is_legal(self, tmp_path: Path) -> None:
        # The boundary the guard must NOT reject.
        incumbent, candidate = self._clear_win()
        assert _gate(_shared_dirs(tmp_path, incumbent, candidate), criterion_index=0).rows_paired > 0

    def test_sibling_recall_drop_blocks_promotion(self, tmp_path: Path) -> None:
        # Criterion 0 is the target (candidate wins outright); criterion 1 is a sibling the
        # candidate annexes on half the rows — a false negative there, so recall.yes drops.
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no"), ("yes", "yes")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes"), ("yes", "yes" if i % 2 else "no")] for i in range(12)}
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate), sibling_indices=[1])
        assert verdict.ci_low is not None and verdict.ci_low > 0.0
        assert [c.passed for c in verdict.sibling_checks] == [False]

        decided = holm_promote([verdict])[0]
        assert decided.promoted is False
        assert any("moved the failure" in note for note in decided.notes)

    def test_sibling_with_no_true_instances_is_not_a_regression(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no"), ("no", "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes"), ("no", "no")] for i in range(12)}
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate), sibling_indices=[1])
        assert [c.passed for c in verdict.sibling_checks] == [True]
        assert verdict.sibling_checks[0].note is not None
        assert holm_promote([verdict])[0].promoted is True

    def test_a_sibling_index_that_selects_nothing_is_noted_not_scored(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate), sibling_indices=[4])
        check = verdict.sibling_checks[0]
        assert check.passed is True
        assert check.incumbent is None and check.candidate is None
        assert check.note is not None and "no classification results on either arm" in check.note

    def test_a_sibling_present_on_one_arm_only_is_not_blamed_on_the_candidate(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no"), ("yes", "yes")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}  # no sibling criterion at all
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate), sibling_indices=[1])
        check = verdict.sibling_checks[0]
        assert check.passed is True
        assert check.note is not None and "one arm only" in check.note

    def test_the_verdict_records_the_interval_width_it_used(self, tmp_path: Path) -> None:
        # A 90% interval labelled 95% is the failure this pins: the renderer reads the width off
        # the verdict rather than assuming one.
        incumbent, candidate = self._clear_win()
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate), confidence=0.90)
        assert verdict.confidence == 0.90
        assert verdict.n_resamples == _FAST_RESAMPLES
        assert "90% CI" in render_markdown(verdict)

    def test_is_deterministic_for_a_seed(self, tmp_path: Path) -> None:
        incumbent, candidate = self._clear_win()
        run_dirs = _shared_dirs(tmp_path, incumbent, candidate)
        assert _gate(run_dirs, seed=3) == _gate(run_dirs, seed=3)


class TestTheVerdictCopySeamWritesOnlyDeclaredFields:
    """Both wrappers write their decision through `copy_with`, whose keys pydantic never validated.

    `model_copy(update=)` sets an unknown key as a bare instance attribute: absent from
    `model_dump()`, with the field it was meant to set left at its default and nothing raised. On
    these two models — the ones that say what a promotion decision rests on — a `mean_dif` typo
    renders a block reporting no difference at all.

    So a decided verdict's INSTANCE STATE is pinned, not only its values: no attribute outside the
    declared fields, and the fields the copy writes carrying what the wrapper decided.

    Be precise about what that catches. `copy_with` raises on a bad key, so while these wrappers go
    through it the stray-attribute check cannot fire — and a call site reverted to
    `model_copy(update=)` with CORRECT keys does not trip it either (CE048 is what catches that).
    It fires on the conjunction: a reverted call site AND a wrong key, which is the state that
    ships a verdict silently reporting no difference. The value assertions below are what carry
    these tests day to day.

    Read `__dict__`, never `model_dump()`. Under `extra="forbid"` an undeclared key set by
    `model_copy(update=)` lands in `__dict__` and is never serialized, so
    `set(model_dump()) == set(model_fields)` is true **even when the defect is present** — and
    `model_extra` is `None`, because that is only populated under `extra="allow"`. A sensor built
    on either would assert something that cannot be false, which is the CE039 failure mode this
    repo already has a rule class for.
    """

    @staticmethod
    def _assert_no_attribute_outside_the_model(verdict) -> None:
        stray = sorted(set(verdict.__dict__) - set(type(verdict).model_fields))
        assert not stray, (
            f"{stray} sit on the verdict as bare instance attributes. That is what an unvalidated "
            "model_copy(update=) does with a mistyped key: the field it was meant to set is left "
            "at its default, and model_dump() never mentions either."
        )

    def test_the_assertion_bites_on_the_defect_it_describes(self, tmp_path: Path) -> None:
        """Anti-vacuity. The first draft of this class asserted on `model_dump()` and could not fail.

        Built by doing to a real verdict exactly what a mistyped `model_copy(update=)` would.
        """
        lone = {"r0": [("yes", "yes")]}
        verdict = _gate(_shared_dirs(tmp_path, lone, lone))
        typo = verdict.model_copy(update={"promotd": True})  # CE048 scans src/ only; this IS the defect

        assert set(typo.model_dump()) == set(type(typo).model_fields), (
            "the model_dump() check the first draft used still passes here — that is why this "
            "class reads __dict__ instead"
        )
        with pytest.raises(AssertionError, match="promotd"):
            self._assert_no_attribute_outside_the_model(typo)

    def test_a_promoted_activation_verdict_carries_no_stray_attribute(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}
        decided = holm_promote([_gate(_shared_dirs(tmp_path, incumbent, candidate))])[0]

        self._assert_no_attribute_outside_the_model(decided)
        assert decided.promoted is True
        assert decided.holm_alpha == DEFAULT_ALPHA
        assert decided.gate_refusal is None
        assert decided.notes and _note_holm_family(1, DEFAULT_ALPHA) in decided.notes

    def test_an_unfamilied_activation_verdict_carries_no_stray_attribute(self, tmp_path: Path) -> None:
        # The `p_value is None` branch, which writes three fields rather than four.
        lone = {"r0": [("yes", "yes")]}
        decided = holm_promote([_gate(_shared_dirs(tmp_path, lone, lone))])[0]

        self._assert_no_attribute_outside_the_model(decided)
        assert decided.p_value is None
        assert decided.promoted is False
        assert decided.holm_alpha == DEFAULT_ALPHA

    def test_a_promoted_execution_verdict_carries_no_stray_attribute(self, tmp_path: Path) -> None:
        decided = holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path, **_WINNER))])[0]

        self._assert_no_attribute_outside_the_model(decided)
        assert decided.promoted is True
        assert decided.holm_alpha == DEFAULT_ALPHA
        assert decided.mean_diff is not None and decided.mean_diff > 0.0

    def test_a_refused_execution_verdict_carries_no_stray_attribute(self, tmp_path: Path) -> None:
        one_row = {"incumbent": {"r1": [0.2, 0.3]}, "candidate": {"r1": [0.7, 0.8]}}
        decided = holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path, **one_row))])[0]

        self._assert_no_attribute_outside_the_model(decided)
        assert decided.gate_refusal is not None
        assert decided.promoted is False


class TestHolmPromote:
    def _verdict(self, name: str, p: float | None, diff: float | None = 0.2) -> ActivationGateVerdict:
        return ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant=name,
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=BOOTSTRAP_RESAMPLES,
            rows_paired=12,
            rows_excluded=0,
            incumbent_f1=0.4,
            candidate_f1=0.6,
            mean_diff=diff,
            ci_low=0.05,
            ci_high=0.35,
            p_value=p,
        )

    def test_step_down_across_a_family_is_not_bonferroni(self) -> None:
        # Bonferroni (p <= alpha/3) would promote only the first; Holm's step-down promotes two.
        verdicts = [self._verdict("a", 0.001), self._verdict("b", 0.02), self._verdict("c", 0.9)]
        assert [v.promoted for v in holm_promote(verdicts)] == [True, True, False]

    def test_single_verdict_reduces_to_plain_alpha(self) -> None:
        assert holm_promote([self._verdict("a", 0.049)])[0].promoted is True
        assert holm_promote([self._verdict("a", 0.051)])[0].promoted is False

    def test_empty_list_returns_empty(self) -> None:
        assert holm_promote([]) == []

    def test_a_family_of_only_undecidable_arms_returns_them_all_unpromoted(self) -> None:
        decided = holm_promote([self._verdict("a", None), self._verdict("b", None)])
        assert [v.promoted for v in decided] == [False, False]

    def test_excludes_none_p_values_from_the_family(self) -> None:
        # The undecidable arm must not tighten the correction for its siblings: with it counted,
        # the family size would be 3 and `b` (p=0.03 > 0.05/2) would fail the second step.
        verdicts = [self._verdict("a", 0.001), self._verdict("b", 0.03), self._verdict("c", None)]
        decided = holm_promote(verdicts)
        assert [v.promoted for v in decided] == [True, True, False]
        assert any("outside the family" in note for note in decided[2].notes)

    def test_records_the_alpha_it_applied(self) -> None:
        assert holm_promote([self._verdict("a", 0.001)], alpha=0.01)[0].holm_alpha == 0.01

    def test_a_difference_favouring_the_incumbent_never_promotes(self) -> None:
        decided = holm_promote([self._verdict("a", 0.001, diff=-0.3)])[0]
        assert decided.promoted is False
        assert any("incumbent's favour" in note for note in decided.notes)


class TestRenderMarkdown:
    def _verdict(self, **overrides) -> ActivationGateVerdict:
        base = {
            "incumbent_variant": "incumbent",
            "candidate_variant": "cand-a",
            "suite_id": SUITE,
            "criterion_index": 0,
            "confidence": 0.95,
            "n_resamples": BOOTSTRAP_RESAMPLES,
            "rows_paired": 12,
            "rows_excluded": 1,
            "incumbent_f1": 0.4,
            "candidate_f1": 0.9,
            "mean_diff": 0.5,
            "ci_low": 0.2,
            "ci_high": 0.75,
            "p_value": 0.002,
            "range_non_overlap": True,
        }
        return ActivationGateVerdict(**{**base, **overrides})

    def test_says_undecided_for_a_none_promotion(self) -> None:
        text = render_markdown(self._verdict())
        assert "UNDECIDED" in text
        assert "holm_promote has not been applied" in text
        assert "NOT PROMOTED" not in text

    def test_contains_the_ci_and_the_diagnostic(self) -> None:
        text = render_markdown(holm_promote([self._verdict()])[0])
        assert "PROMOTED" in text
        assert "[0.200, 0.750]" in text
        assert "0.500" in text
        assert "DIAGNOSTIC, not the gate" in text
        assert "Rows paired: 12" in text and "excluded: 1" in text

    def test_prints_every_check_with_its_note(self) -> None:
        verdict = self._verdict(
            sibling_checks=[
                GuardrailCheck(
                    name="sibling recall.yes [criterion 1]",
                    incumbent=0.0,
                    candidate=0.0,
                    relative_change=None,
                    tolerance=0.0,
                    passed=True,
                    note="recall.yes is 0.0 on both arms — nothing to regress",
                )
            ],
            notes=["a note the reader needs"],
        )
        text = render_markdown(verdict)
        assert "sibling recall.yes [criterion 1]" in text
        assert "nothing to regress" in text
        assert "a note the reader needs" in text


# The three modules the optimize gate was split into. Named once: two module-scoped assertions and
# the layering tests below all reason over the same surface, and a list that drifted would leave a
# new module silently unchecked by whichever one forgot it.
_OPTIMIZE_MODULES = ("optimize_gate", "optimize_store", "reports_optimize")


def _module_source(module: str) -> str:
    return (Path(__file__).parent.parent / "src" / "coder_eval" / f"{module}.py").read_text(encoding="utf-8")


def _coder_eval_imports(module: str, *, inside_type_checking: bool | None = None) -> dict[str, set[str]]:
    """`coder_eval` imports in a module, keyed by module path.

    AST-based, not a substring scan: these modules are heavily docstringed and a raw-text check
    over them is exactly the fragile presence sensor CE039 exists to discourage.

    ``inside_type_checking`` filters to imports inside (True) or outside (False) an
    ``if TYPE_CHECKING:`` body; ``None`` takes both.

    Routed through ``resolved_module`` (CE051). Matching ``node.module`` alone made this blind to
    the RELATIVE spelling, which is how most of ``src/`` imports — and these three modules are
    siblings, so ``from .optimize_gate import CostQualityPoint`` in ``reports_optimize.py`` is the
    natural way to write the very import this pins. ``node.module`` would then be
    ``"optimize_gate"``, the ``startswith("coder_eval")`` test would be False, and both layering
    tests would pass over a broken boundary — the same fail-open shape, on what CLAUDE.md calls
    out as pinned "by a test, not by this sentence".
    """
    path = str((Path(__file__).parent.parent / "src" / "coder_eval" / f"{module}.py").resolve())
    tree = ast.parse(_module_source(module))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            guarded |= {id(child) for stmt in node.body for child in ast.walk(stmt)}

    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        resolved = resolved_module(node, path)
        if not (resolved or "").startswith("coder_eval"):
            continue
        if inside_type_checking is not None and (id(node) in guarded) != inside_type_checking:
            continue
        found.setdefault(resolved or "", set()).update(alias.name for alias in node.names)
    return found


def test_the_presentation_module_makes_no_decisions_and_reads_no_disk() -> None:
    """What makes the split real rather than cosmetic.

    A renderer that reaches back for a run directory or an estimator is a gate with a table on it,
    and the module boundary then documents a separation that does not exist. The two NamedTuples it
    needs are allowed only under `if TYPE_CHECKING`, so the runtime dependency on the decision
    layer is exactly zero.
    """
    runtime = _coder_eval_imports("reports_optimize", inside_type_checking=False)
    assert set(runtime) <= {"coder_eval.models", "coder_eval.reports_stats"}, (
        f"reports_optimize gained a runtime coder_eval dependency: {sorted(set(runtime))}"
    )
    # One display value, and CE040 requires it be DERIVED there rather than respelled.
    assert runtime.get("coder_eval.reports_stats", set()) == {"bootstrap_p_floor"}

    deferred = _coder_eval_imports("reports_optimize", inside_type_checking=True)
    assert deferred.get("coder_eval.optimize_gate") == {"CostQualityPoint", "SearchComparison"}

    # No filesystem call anywhere in the module.
    tree = ast.parse(_module_source("reports_optimize"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    filesystem = {"open", "read_text", "write_text", "glob", "rglob", "mkdir", "replace", "Path"}
    assert not called & filesystem, f"reports_optimize touches the filesystem: {sorted(called & filesystem)}"


def test_the_gate_never_imports_its_presentation() -> None:
    # The mirror direction. If anything drives you to add this edge, the split boundary is wrong.
    assert "coder_eval.reports_optimize" not in _coder_eval_imports("optimize_gate")


def test_the_store_module_imports_only_models() -> None:
    # `optimize_gate` -> `optimize_store` is the only edge between the three non-model modules, so
    # anything else here would close a cycle.
    assert set(_coder_eval_imports("optimize_store")) <= {"coder_eval.models"}


def test_a_moved_name_is_gone_from_the_gate() -> None:
    """No name the two new modules define survives on `optimize_gate` except a deliberate re-import.

    DERIVED on both sides, never a hand-written list — a list here would be a second declaration of
    each module's contents, which is the exact defect this plan removed three of. The module's own
    contents come from enumerating it (with a `__module__` filter, so a name it merely IMPORTS does
    not count as one it defines); the permitted survivors come from parsing the gate's OWN import
    statements. A leftover copy is therefore caught, while the two names the gate genuinely imports
    back are not mistaken for one.

    `ruff` keeps that permitted set minimal for free: an unused back-import is F401.
    """
    import coder_eval.optimize_gate as gate
    import coder_eval.optimize_store as store
    import coder_eval.reports_optimize as presentation

    gate_imports = _coder_eval_imports("optimize_gate")
    for module in (store, presentation):
        defined = [
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        ]
        assert defined, f"{module.__name__} defines nothing — the enumeration is not doing its job"
        back_imported = gate_imports.get(module.__name__, set())
        leftovers = [name for name in defined if hasattr(gate, name) and name not in back_imported]
        assert not leftovers, f"{module.__name__} names still defined on optimize_gate: {leftovers}"

    # The one edge between the three non-model modules. THREE names now: `UNRECORDED_SPLIT` joined
    # the two the split originally left, and for the same reason `UNRESOLVED_MODEL` is over there —
    # it is a cache-key sentinel the STORE refuses to write, so declaring it in the gate would make
    # the store import the gate and close a cycle.
    assert gate_imports.get("coder_eval.optimize_store") == {
        "UNRECORDED_SPLIT",
        "UNRESOLVED_MODEL",
        "lookup_noise_floor",
    }
    assert "coder_eval.reports_optimize" not in gate_imports


def test_module_imports_no_cli_machinery() -> None:
    """A core library the skill drives from a snippet — not a command.

    CE004 does NOT cover this: its _CORE_DIRS regex matches only the models/criteria/... SUBDIRS,
    so a top-level module is out of scope, and it bans importing `coder_eval.cli` rather than
    typer/rich at all. Hence a real assertion here.
    """
    # All THREE modules: the claim is about the whole surface the skill's snippets import, and the
    # gate's presentation and sidecar halves are exactly as reachable from a snippet as it is.
    for module in _OPTIMIZE_MODULES:
        source = _module_source(module)
        for banned in ("import typer", "import rich", "from typer", "from rich", "coder_eval.cli"):
            assert banned not in source, f"{module}.py imports {banned!r} — it is a library, not a CLI surface"


def _costed_result(
    row_id: str, labels: list[tuple[str, str]], *, cost: float | None, duration: float
) -> EvaluationResult:
    """A row result carrying cost and duration, for the guardrail tests."""
    result = _eval_result(row_id, labels)
    return result.model_copy(
        update={
            "duration_seconds": duration,
            "total_token_usage": TokenUsage(total_cost_usd=cost) if cost is not None else None,
        }
    )


def _cost_rows(per_row: dict[str, list[float]], *, duration: float = 10.0) -> dict[str, list[EvaluationResult]]:
    return {
        rid: [_costed_result(rid, [("yes", "yes")], cost=c, duration=duration) for c in costs]
        for rid, costs in per_row.items()
    }


def _duration_rows(per_row: dict[str, list[float]]) -> dict[str, list[EvaluationResult]]:
    return {
        rid: [_costed_result(rid, [("yes", "yes")], cost=1.0, duration=d) for d in durations]
        for rid, durations in per_row.items()
    }


def _cost_check(checks: list[GuardrailCheck]) -> GuardrailCheck:
    return next(c for c in checks if c.name.startswith("cost"))


class TestNoiseFloorMde:
    def test_returns_a_positive_half_width(self, tmp_path: Path) -> None:
        # A noisy incumbent: the same rows flip between invocations, so the null comparison has
        # a real spread and the MDE is above zero.
        run_dirs = []
        for i in range(4):
            run_dir = tmp_path / f"run-{i}"
            for row in range(10):
                observed = "yes" if (row + i) % 3 else "no"
                _write_row(run_dir, "incumbent", f"r{row}", _eval_result(f"r{row}", [("yes", observed)]))
            run_dirs.append(run_dir)

        mde = noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0)
        assert mde is not None and mde > 0.0

    def test_none_with_a_single_invocation(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {f"r{i}": [("yes", "yes")] for i in range(6)}, invocations=1)
        assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) is None

    def test_none_with_fewer_than_two_rows(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"only": [("yes", "yes")]}, invocations=3)
        assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) is None

    def test_an_odd_invocation_count_splits_two_one(self, tmp_path: Path) -> None:
        # 3 invocations must still produce a floor (a 2/1 split), not None.
        run_dirs = _write_arm(tmp_path, "incumbent", {f"r{i}": [("yes", "yes")] for i in range(6)}, invocations=3)
        assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) == 0.0


class TestResolveModel:
    def test_returns_the_single_model_used(self, tmp_path: Path) -> None:
        rows = {"r0": [_eval_result("r0", [("yes", "yes")]).model_copy(update={"model_used": "claude-haiku-4-5"})]}
        assert resolve_model(rows) == "claude-haiku-4-5"

    def test_returns_none_when_rows_disagree(self, tmp_path: Path) -> None:
        rows = {
            "r0": [_eval_result("r0", [("yes", "yes")]).model_copy(update={"model_used": "claude-haiku-4-5"})],
            "r1": [_eval_result("r1", [("yes", "yes")]).model_copy(update={"model_used": "claude-sonnet-5"})],
        }
        assert resolve_model(rows) is None

    def test_returns_none_when_unset(self) -> None:
        assert resolve_model({"r0": [_eval_result("r0", [("yes", "yes")])]}) is None


class TestCostLatencyGuardrails:
    def test_fails_on_a_large_consistent_increase(self) -> None:
        incumbent = _cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = _cost_rows({f"r{i}": [2.0] for i in range(12)})
        check = _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.passed is False
        assert check.ci_low is not None and check.ci_low > MATERIALITY_FLOOR * 1.0

    def test_passes_on_a_noisy_wash(self) -> None:
        """The regression test for the measured false-positive mode.

        12 rows whose per-row costs are drawn from the SAME distribution (mean 1.0, CV 0.25), so the
        true difference is zero. This seed is chosen because the sample medians still land 19.3%
        apart (0.983 vs 1.173) purely by noise: a fixed 15% tolerance on the median VETOES this
        candidate, which is exactly the false positive the measured CVs predicted.

        The second assertion is what makes the test attributable, and it is the reason this seed was
        chosen over others that also clear 15%: with the materiality floor set to ZERO the check
        still passes, so it is the bootstrap interval — which contains zero — absorbing the noise,
        not the floor suppressing it. A seed where only the floor saves the candidate would pass
        this test while proving nothing about the redesign.
        """
        rng = random.Random(13)
        incumbent = _cost_rows({f"r{i}": [max(0.05, rng.gauss(1.0, 0.25))] for i in range(12)})
        candidate = _cost_rows({f"r{i}": [max(0.05, rng.gauss(1.0, 0.25))] for i in range(12)})

        check = _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.relative_change is not None and check.relative_change > 0.15  # a fixed rule fires
        assert check.passed is True
        assert check.ci_low is not None and check.ci_low < 0.0  # the interval contains zero

        floorless = _cost_check(
            cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate, materiality=0.0)
        )
        assert floorless.passed is True, "the interval must absorb this, not the materiality floor"

    def test_reports_the_interval_not_just_the_verdict(self) -> None:
        incumbent = _cost_rows({f"r{i}": [1.0, 1.1] for i in range(8)})
        candidate = _cost_rows({f"r{i}": [1.2, 1.3] for i in range(8)})
        check = _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.ci_low is not None and check.ci_high is not None
        assert check.ci_low <= check.ci_high

    def test_materiality_floor_only_suppresses(self) -> None:
        # A statistically real but small increase passes; the SAME increase scaled past the floor
        # fails. Driven off MATERIALITY_FLOOR, never a hardcoded number.
        small = MATERIALITY_FLOOR / 2.0
        large = MATERIALITY_FLOOR * 2.0
        incumbent = _cost_rows({f"r{i}": [1.0] for i in range(12)})
        assert _cost_check(
            cost_latency_guardrails(
                incumbent_rows=incumbent, candidate_rows=_cost_rows({f"r{i}": [1.0 + small] for i in range(12)})
            )
        ).passed
        assert not _cost_check(
            cost_latency_guardrails(
                incumbent_rows=incumbent, candidate_rows=_cost_rows({f"r{i}": [1.0 + large] for i in range(12)})
            )
        ).passed

    def test_with_no_recorded_cost_passes_with_a_note(self) -> None:
        incumbent = _cost_rows({f"r{i}": [None] for i in range(6)})  # type: ignore[arg-type]
        candidate = _cost_rows({f"r{i}": [None] for i in range(6)})  # type: ignore[arg-type]
        check = _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.passed is True
        assert check.incumbent is None
        assert check.note is not None and "not evaluated" in check.note

    def test_a_zero_incumbent_does_not_divide(self) -> None:
        incumbent = _cost_rows({f"r{i}": [0.0] for i in range(12)})
        candidate = _cost_rows({f"r{i}": [0.5] for i in range(12)})
        check = _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.relative_change is None
        assert check.passed is True
        assert check.note is not None and "the incumbent measured zero" in check.note

    def test_notes_an_asymmetric_measurement_count(self) -> None:
        incumbent = _cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = _cost_rows({f"r{i}": ([1.0] if i else [None]) for i in range(12)})  # type: ignore[arg-type]
        check = _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.note is not None and "incumbent row(s) vs" in check.note

    def test_latency_is_guarded_too(self) -> None:
        incumbent = _duration_rows({f"r{i}": [10.0] for i in range(12)})
        candidate = _duration_rows({f"r{i}": [30.0] for i in range(12)})
        latency = next(
            c
            for c in cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate)
            if c.name.startswith("latency")
        )
        assert latency.passed is False


class TestMdeAndGuardrailsInTheVerdict:
    def test_gate_notes_a_difference_below_the_mde(self, tmp_path: Path) -> None:
        # Both arms noisy in the same way: the difference is tiny, and the incumbent's own
        # invocation-to-invocation spread is larger than it.
        run_dirs = []
        for i in range(4):
            run_dir = tmp_path / f"run-{i}"
            for row in range(10):
                incumbent_observed = "yes" if (row + i) % 3 else "no"
                candidate_observed = "yes" if (row + i + 1) % 3 else "no"
                _write_row(run_dir, "incumbent", f"r{row}", _eval_result(f"r{row}", [("yes", incumbent_observed)]))
                _write_row(run_dir, "candidate", f"r{row}", _eval_result(f"r{row}", [("yes", candidate_observed)]))
            run_dirs.append(run_dir)

        verdict = _gate(run_dirs)
        assert verdict.mde is not None and verdict.mde > 0.0
        assert verdict.mean_diff is not None and abs(verdict.mean_diff) < verdict.mde
        assert any("minimum detectable effect" in note for note in verdict.notes)

    def test_gate_notes_when_the_mde_cannot_be_computed(self, tmp_path: Path) -> None:
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate, invocations=1))
        assert verdict.mde is None
        assert any("could not be computed" in note for note in verdict.notes)

    def test_gate_fills_guardrails_from_the_scored_rows(self, tmp_path: Path) -> None:
        incumbent, candidate = ({f"r{i}": [("yes", "yes")] for i in range(12)} for _ in range(2))
        run_dirs = _shared_dirs(tmp_path, incumbent, candidate)
        verdict = _gate(run_dirs)
        assert [c.name for c in verdict.guardrails] == ["cost (USD/row)", "latency (seconds/row)"]
        # These fixtures record no cost, so the cost guardrail must pass WITH A NOTE, never bare.
        assert all(c.passed for c in verdict.guardrails)
        assert _cost_check(verdict.guardrails).note is not None

    def test_render_markdown_prints_the_mde_and_every_guardrail_note(self, tmp_path: Path) -> None:
        incumbent, candidate = ({f"r{i}": [("yes", "yes")] for i in range(12)} for _ in range(2))
        text = render_markdown(holm_promote([_gate(_shared_dirs(tmp_path, incumbent, candidate))])[0])
        assert "Minimum detectable effect: 0.000" in text
        assert "cost (USD/row)" in text
        assert "not evaluated" in text
        assert "latency (seconds/row)" in text

    def test_render_markdown_shows_the_guardrail_interval(self) -> None:
        incumbent = _cost_rows({f"r{i}": [1.0] for i in range(12)})
        candidate = _cost_rows({f"r{i}": [2.0] for i in range(12)})
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=BOOTSTRAP_RESAMPLES,
            rows_paired=12,
            rows_excluded=0,
            incumbent_f1=0.4,
            candidate_f1=0.9,
            mean_diff=0.5,
            ci_low=0.2,
            ci_high=0.75,
            p_value=0.002,
            guardrails=cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate),
        )
        text = render_markdown(verdict)
        assert "FAIL · cost (USD/row)" in text
        assert "diff CI [" in text
        assert "x incumbent" in text


def _headline(text: str) -> str:
    """The rendered block's headline, unwrapped from its bold markers.

    Reading the headline LINE rather than asserting a substring is absent from the whole block:
    "PROMOTED" is a substring of "NOT PROMOTED", so a `not in` over the block is either wrong or a
    no-op depending on the fixture, and both failure modes already exist in this file.

    It got sharper once the failed-check note began quoting the headline's own words ("the rendered
    headline reports it as BLOCKED BY A GUARDRAIL"): `"BLOCKED" not in block` now matches that
    SENTENCE and passes on a block whose headline is something else entirely. Assert on the
    discriminating line, never on a substring of the whole page.
    """
    return next(line for line in text.splitlines() if line.startswith("**")).strip("*")


def _failing_cost_check() -> GuardrailCheck:
    """A measured, material cost breach — the check both tracks must now veto on."""
    return GuardrailCheck(
        name="cost (USD/row)",
        incumbent=1.0,
        candidate=2.0,
        relative_change=1.0,
        tolerance=MATERIALITY_FLOOR,
        ci_low=0.6,
        ci_high=1.4,
        passed=False,
    )


def _parity_activation(**overrides) -> ActivationGateVerdict:
    """A separating activation verdict, ready for `holm_promote`."""
    base = {
        "incumbent_variant": "incumbent",
        "candidate_variant": "cand",
        "suite_id": SUITE,
        "criterion_index": 0,
        "confidence": 0.95,
        "n_resamples": BOOTSTRAP_RESAMPLES,
        "rows_paired": 12,
        "rows_excluded": 0,
        "incumbent_f1": 0.4,
        "candidate_f1": 0.9,
        "mean_diff": 0.5,
        "ci_low": 0.2,
        "ci_high": 0.75,
        "p_value": 0.001,
    }
    return ActivationGateVerdict(**{**base, **overrides})


def _parity_execution(**overrides) -> ExecutionGateVerdict:
    """The execution twin of `_parity_activation`, on the same numbers."""
    base = {
        "incumbent_variant": "incumbent",
        "candidate_variant": "cand",
        "suite_id": EXEC_SUITE,
        "confidence": 0.95,
        "n_resamples": BOOTSTRAP_RESAMPLES,
        "rows_paired": 12,
        "rows_excluded": 0,
        "mean_diff": 0.5,
        "ci_low": 0.2,
        "ci_high": 0.75,
        "effect_size": 1.1,
        "p_value": 0.001,
    }
    return ExecutionGateVerdict(**{**base, **overrides})


# The two tracks' (gate, verdict factory) pairs, so a parity claim is asserted over both rather
# than written twice and allowed to drift — which is the defect this whole phase closes.
_TRACKS = [
    pytest.param(holm_promote, _parity_activation, id="activation"),
    pytest.param(holm_promote_execution, _parity_execution, id="execution"),
]


class TestPromotionIsNotOverstated:
    """Two ways the rendered block could claim more than the tool decided."""

    # The same separating verdict the cross-track parity class builds. It was a byte-identical
    # second copy of that base dict, which is the duplication `_TRACKS` exists to remove.
    _verdict = staticmethod(_parity_activation)

    def test_an_interval_containing_zero_never_promotes(self) -> None:
        # Holm can reject at a corrected alpha while the reported interval still contains zero.
        # The method file states the rule as "the interval excludes zero", so the code must make
        # that literally true rather than approximately true.
        decided = holm_promote([self._verdict(ci_low=-0.05, ci_high=0.6)])[0]
        assert decided.promoted is False
        assert any("still contains zero" in note for note in decided.notes)

    def test_a_failed_guardrail_never_renders_as_promoted(self) -> None:
        decided = holm_promote([self._verdict(guardrails=[_failing_cost_check()])])[0]
        # INVERTED, deliberately, and kept rather than deleted because it is the REACHABILITY
        # PROOF for the BLOCKED rung: it is the one test that builds a verdict which separates,
        # clears Holm, and carries a failing guardrail. `promoted` used to read True here — the
        # guardrails gated in the skill's prose and not in the field — so a caller reading the
        # field could ship a candidate the rendered block said was blocked. The veto now lives in
        # the decision, and the headline still has to tell "it won and was vetoed" apart from
        # "it lost", which is what `holm_rejected and separated` keys it on.
        assert decided.promoted is False
        assert decided.holm_rejected is True
        assert decided.separated is True
        text = render_markdown(decided)
        # On the HEADLINE, not merely somewhere in the block — the notes quote the headline's own
        # words, so a whole-page substring test would pass on the wrong rung.
        assert _headline(text).startswith("BLOCKED BY A GUARDRAIL —")
        assert "cost (USD/row)" in text
        assert "Do not promote on this block" in text
        # And the block names WHICH check vetoed, so the reader is not left to diff the lists.
        assert any("cost (USD/row) FAILED" in note for note in decided.notes)

    def test_an_empty_guardrail_list_does_not_block(self) -> None:
        # `any(...)` over `[]` is False, so a suite with no cost telemetry at all still promotes.
        # Worth pinning: the veto was added by folding a list into `promoted`, and an empty list
        # is the commonest shape on a suite whose turns recorded no cost.
        decided = holm_promote([self._verdict(guardrails=[])])[0]
        assert decided.promoted is True
        assert _headline(render_markdown(decided)) == "PROMOTED"

    def test_a_separated_blocked_candidate_holm_never_rejected_reads_not_promoted(self) -> None:
        """The BLOCKED rung must not OVER-fire — the trap on the other side of `promoted`.

        `separated` is a property of one verdict and deliberately excludes the FAMILY decision, so
        at `m > 1` a p between `alpha/m` and `alpha` leaves `ci_low > 0` while Holm rejects
        nothing. Keying BLOCKED on `separated` alone then sends the reader to fix cost when the
        real problem is power — measured on the execution track with two candidates at p = 0.03 in
        a family of two. `holm_rejected` is the conjunct that closes it.
        """
        failing = GuardrailCheck(
            name="cost (USD/row)",
            incumbent=1.0,
            candidate=2.0,
            relative_change=1.0,
            tolerance=MATERIALITY_FLOOR,
            ci_low=0.6,
            ci_high=1.4,
            passed=False,
        )
        # Two identical candidates at p = 0.03: alpha/2 = 0.025, so Holm rejects neither.
        decided = holm_promote([self._verdict(p_value=0.03, guardrails=[failing]) for _ in range(2)])
        for verdict in decided:
            assert verdict.separated is True, "the statistic did separate"
            assert verdict.holm_rejected is False, "but the family correction rejected nothing"
            assert _headline(render_markdown(verdict)) == "NOT PROMOTED"

    def test_a_passing_guardrail_still_reads_promoted(self) -> None:
        passing = GuardrailCheck(
            name="cost (USD/row)",
            incumbent=1.0,
            candidate=1.02,
            relative_change=0.02,
            tolerance=MATERIALITY_FLOOR,
            ci_low=-0.1,
            ci_high=0.2,
            passed=True,
        )
        text = render_markdown(holm_promote([self._verdict(guardrails=[passing])])[0])
        assert _headline(text) == "PROMOTED"


class TestBothTracksMeanTheSameThingByPromoted:
    """`promoted` is ONE contract now, asserted over both tracks rather than stated twice.

    The activation track used to fold its `sibling_checks` into `promoted` while leaving the
    cost/latency `guardrails` advisory for the skill's prose to gate — so a candidate that
    materially raised what a row costs read `promoted=True`, and a caller reading the field could
    ship what the rendered block called BLOCKED. The execution track already vetoed. These are the
    sensors that stop the two drifting apart again.
    """

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_a_failing_guardrail_forces_promoted_false(self, gate, build) -> None:
        decided = gate([build(guardrails=[_failing_cost_check()])])[0]
        assert decided.holm_rejected is True, "Holm did reject it"
        assert decided.separated is True, "and the statistic did separate"
        assert decided.promoted is False, "so the guardrail is what vetoed — on BOTH tracks"

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_blocked_block_names_the_failing_check(self, gate, build) -> None:
        decided = gate([build(guardrails=[_failing_cost_check()])])[0]
        assert any("cost (USD/row) FAILED" in note for note in decided.notes)

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_failed_check_note_stays_off_a_candidate_that_simply_lost(self, gate, build) -> None:
        """The note claims a veto and cites the BLOCKED headline — both false on a plain loss.

        `promoted` is already False for a candidate Holm never rejected, so a failing guardrail
        forced nothing, and the headline is NOT PROMOTED. Printing the note there is the same
        misdirection the BLOCKED rung's `holm_rejected` conjunct exists to remove: it sends the
        reader to fix cost when the real problem is power. The failing check is still visible in
        the rendered Guardrails list on this path — only the CLAIM is withheld.
        """
        decided = gate([build(p_value=0.9, guardrails=[_failing_cost_check()])])[0]
        assert decided.holm_rejected is False
        assert decided.promoted is False
        assert not any("FAILED" in note for note in decided.notes), decided.notes
        # And the ordinary negative result is what the block DOES say.
        assert any("did not clear the Holm threshold" in note for note in decided.notes)

    @pytest.mark.parametrize(
        ("gate", "build", "refusing"),
        [
            # Each track refuses through its OWN mechanism, and they are not interchangeable.
            # `holm_promote` RECOMPUTES `gate_refusal` from the discreteness floor and overwrites
            # whatever the verdict arrived with, so setting the field directly is unreachable
            # there — a suite whose floor exceeds its Holm threshold is the reachable state.
            # `execution_gate` sets the field itself and `holm_promote_execution` only reads it.
            pytest.param(holm_promote, _parity_activation, {"p_floor": 0.9, "n_discordant": 1}, id="activation"),
            pytest.param(
                holm_promote_execution, _parity_execution, {"gate_refusal": "no comparison was made"}, id="execution"
            ),
        ],
    )
    def test_the_failed_check_note_stays_off_a_refused_verdict(self, gate, build, refusing) -> None:
        # Under a refusal the headline is not a decision at all, so a note asserting the block
        # "reports it as BLOCKED BY A GUARDRAIL" contradicts the line above it.
        decided = gate([build(guardrails=[_failing_cost_check()], **refusing)])[0]
        assert decided.gate_refusal is not None, "the fixture must actually refuse"
        assert decided.promoted is False
        assert not any("FAILED" in note for note in decided.notes), decided.notes

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_an_empty_guardrail_list_does_not_block(self, gate, build) -> None:
        assert gate([build()])[0].promoted is True

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_holm_rejected_is_recorded_on_the_measured_branch(self, gate, build) -> None:
        # A p that clears alpha/1 and one that does not, in a family of one each.
        assert gate([build(p_value=0.001)])[0].holm_rejected is True
        assert gate([build(p_value=0.9)])[0].holm_rejected is False

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_holm_rejected_is_false_not_none_outside_the_family(self, gate, build) -> None:
        """`None` would read as "Holm has not run" on a verdict it has."""
        decided = gate([build(p_value=None, mean_diff=None, ci_low=None, ci_high=None)])[0]
        assert decided.holm_rejected is False
        assert decided.promoted is False

    # Deliberately NOT parametrized over `_TRACKS`: `separated` is a property of the VERDICT and
    # no gate is involved, so pairing it with a gate would run every case twice for nothing.
    @pytest.mark.parametrize("build", [_parity_activation, _parity_execution], ids=["activation", "execution"])
    @pytest.mark.parametrize(
        ("mean_diff", "ci_low", "expected"),
        [
            (0.5, 0.2, True),
            # The two boundaries. `> 0.0` on both, so a bound sitting exactly ON zero has not
            # excluded it and a zero difference does not favour the candidate.
            (0.5, 0.0, False),
            (0.0, 0.2, False),
            (-0.5, -0.2, False),
        ],
    )
    def test_separated_agrees_across_both_verdict_types(self, build, mean_diff, ci_low, expected) -> None:
        assert build(mean_diff=mean_diff, ci_low=ci_low).separated is expected


def _scored_result(row_id: str, score: float) -> EvaluationResult:
    """A row result carrying a weighted_score plus one criterion scoring the same."""
    return _eval_result(row_id, [("yes", "yes" if score >= 0.5 else "no")]).model_copy(update={"weighted_score": score})


def _write_scored_arm(tmp_path: Path, variant: str, per_row: dict[str, list[float]]) -> list[Path]:
    """One arm whose row scores differ per run dir, so the replicate reduction is exercised."""
    invocations = max(len(v) for v in per_row.values())
    run_dirs = []
    for i in range(invocations):
        run_dir = tmp_path / f"run-{i}"
        for row_id, scores in per_row.items():
            if i < len(scores):
                _write_row(run_dir, variant, row_id, _scored_result(row_id, scores[i]))
        run_dirs.append(run_dir)
    return run_dirs


class TestRegressionCheck:
    """The corpus finally has a reader, and a hole is not a pass."""

    _CORPUS: ClassVar[list[RegressionRow]] = [
        RegressionRow(row_id="pos-1", promoted_in_round=1, reason="oblique phrasing"),
        RegressionRow(row_id="pos-2", promoted_in_round=1, reason="symptom vocabulary"),
        RegressionRow(row_id="pos-3", promoted_in_round=2, reason="negated request"),
    ]

    def test_names_the_lost_row_and_the_hole_but_not_the_kept_one(self) -> None:
        arm = ArmRowScores(variant_id="cand", row_scores={"pos-1": 1.0, "pos-2": 0.5})
        found = regression_check(self._CORPUS, arm)
        assert [(row.row_id, score) for row, score in found] == [("pos-2", 0.5), ("pos-3", None)]

    def test_an_empty_corpus_is_an_empty_result(self) -> None:
        assert regression_check([], ArmRowScores(variant_id="cand", row_scores={"pos-1": 1.0})) == []

    def test_an_arm_that_scored_nothing_reports_every_row_as_a_hole(self) -> None:
        found = regression_check(self._CORPUS, ArmRowScores(variant_id="cand"))
        assert [score for _row, score in found] == [None, None, None]

    def test_the_threshold_reclassifies_a_partial_score(self) -> None:
        # 2 of 3 replicates reads 0.667: a loss at the binary default, a pass on a fractional suite.
        arm = ArmRowScores(variant_id="cand", row_scores={r.row_id: 2 / 3 for r in self._CORPUS})
        assert len(regression_check(self._CORPUS, arm)) == 3
        assert regression_check(self._CORPUS, arm, threshold=0.6) == []

    def test_results_come_back_in_corpus_order(self) -> None:
        arm = ArmRowScores(variant_id="cand", row_scores={"pos-2": 0.0, "pos-1": 0.0, "pos-3": 0.0})
        assert [row.row_id for row, _score in regression_check(self._CORPUS, arm)] == ["pos-1", "pos-2", "pos-3"]


class TestCriterionIndexIsBoundedBelow:
    """The lower bound. The internal guards bound only ABOVE (``criterion_index >= len(...)``),
    which is right for the overflow case — rows legitimately differ in criteria count, so an
    over-long index degrades to "skip the row" — and blind to a negative one. Python's positional
    indexing then silently grades ``success_criteria_results[-1]``: the LAST criterion on every
    row, reported as a confident number for the criterion the caller named. The skill drives all of
    this from an inline ``python`` snippet, so a wrong index is an authoring error that has to be
    loud rather than coerced into a different measurement.
    """

    def test_activation_gate_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            activation_gate(
                incumbent_run_dirs=run_dirs,
                candidate_run_dirs=run_dirs,
                incumbent_variant="incumbent",
                candidate_variant="candidate",
                suite_id=SUITE,
                criterion_index=-1,
                n_resamples=_FAST_RESAMPLES,
            )

    def test_execution_gate_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -2"):
            _exec_gate(run_dir, engagement_criterion_index=-2)

    def test_arm_row_scores_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=-1)

    def test_cost_quality_points_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            cost_quality_points(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=-1)

    def test_noise_floor_mde_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=-1)

    def test_measure_noise_floor_rejects_a_negative_index(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            measure_noise_floor(
                run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=-1, model="m"
            )

    def test_none_stays_legal_on_the_index_optional_entry_points(self, tmp_path: Path) -> None:
        # `None` is the documented "use the row's weighted_score" sentinel, not a missing value.
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        assert arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE) != []
        assert cost_quality_points(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE) is not None

    def test_an_over_long_index_still_degrades_rather_than_raising(self, tmp_path: Path) -> None:
        # The anti-over-fix pin: only the LOWER bound became an error. Rows legitimately differ in
        # criteria count, so an index past the end must keep skipping the row.
        run_dirs = _write_arm(tmp_path, "incumbent", {"r1": [("yes", "yes")]})
        scores = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=9)
        assert scores[0].row_scores == {}

    def test_minus_one_used_to_grade_the_last_criterion_of_each_row(self, tmp_path: Path) -> None:
        """The defect witnessed, not merely asserted — on rows whose criteria lists DIFFER.

        Every other test here uses single-criterion rows, where `-1` and `0` select the same
        result and the bug is invisible. Here row `r1` carries two criteria and `r2` one, so
        `success_criteria_results[-1]` is a DIFFERENT criterion on the two rows — and on `r1` it is
        a `file_check`, not the `skill_triggered` the caller named. `_label_pairs` keeps only
        `ClassificationCriterionResult`s, so before the guard this returned a confident F1
        computed over a silently different, silently smaller set of rows.
        """
        run_dir = tmp_path / "run-0"
        _write_row(run_dir, "incumbent", "r1", _eval_result("r1", [("yes", "no")], extra_basic=True))
        _write_row(run_dir, "incumbent", "r2", _eval_result("r2", [("yes", "yes")]))

        rows = load_suite_rows(run_dir, "incumbent", SUITE)
        # What `-1` would have selected: `file_check` on r1 (dropped by _label_pairs, so the row
        # vanishes from the sample) and the row's only classification result on r2.
        assert [type(rows[rid][0].success_criteria_results[-1]).__name__ for rid in ("r1", "r2")] == [
            "CriterionResult",
            "ClassificationCriterionResult",
        ]
        # Index 0 — what the caller asked for — is a classification result on BOTH rows.
        assert all(len(_label_pairs(rows[rid], 0)) == 1 for rid in ("r1", "r2"))
        # And the boundary now refuses to answer the question at all.
        with pytest.raises(ValueError, match=r"criterion_index must be >= 0, got -1"):
            arm_row_scores(run_dirs=[run_dir], variant_ids=["incumbent"], suite_id=SUITE, criterion_index=-1)

    def test_the_persisted_verdict_cannot_carry_a_negative_index(self) -> None:
        # The mechanical half, on the model rather than at the boundary: a recorded verdict can
        # never claim a negative position even if some future caller bypassed the guard.
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            ActivationGateVerdict(
                incumbent_variant="i",
                candidate_variant="c",
                suite_id=SUITE,
                criterion_index=-1,
                confidence=0.95,
                n_resamples=10,
                rows_paired=0,
                rows_excluded=0,
                mean_diff=None,
                ci_low=None,
                ci_high=None,
                p_value=None,
                p_floor=None,
                n_discordant=None,
            )


class TestArmRowScores:
    def test_reads_every_row_and_variant(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-0"
        for variant, score in (("incumbent", 0.4), ("cand-a", 0.9)):
            for row in ("r1", "r2"):
                _write_row(run_dir, variant, row, _scored_result(row, score))

        arms = arm_row_scores(run_dirs=[run_dir], variant_ids=["incumbent", "cand-a"], suite_id=SUITE)
        assert [a.variant_id for a in arms] == ["incumbent", "cand-a"]
        assert arms[0].row_scores == {"r1": 0.4, "r2": 0.4}
        assert arms[1].row_scores == {"r1": 0.9, "r2": 0.9}

    def test_averages_replicates_across_run_dirs(self, tmp_path: Path) -> None:
        # A row scoring 0.0 and 1.0 in two run dirs reduces to 0.5 — not to whichever dir was
        # read first, which is what a single-run-dir signature would have given.
        run_dirs = _write_scored_arm(tmp_path, "incumbent", {"r1": [0.0, 1.0]})
        arms = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE)
        assert arms[0].row_scores == {"r1": 0.5}

    def test_reads_a_criterion_score_when_given_an_index(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-0"
        _write_row(run_dir, "incumbent", "r1", _eval_result("r1", [("yes", "no"), ("yes", "yes")]))
        by_criterion = arm_row_scores(run_dirs=[run_dir], variant_ids=["incumbent"], suite_id=SUITE, criterion_index=1)
        assert by_criterion[0].row_scores == {"r1": 1.0}

    def test_a_row_without_a_score_is_absent_not_zero(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-0"
        _write_row(run_dir, "incumbent", "r1", _scored_result("r1", 0.8))
        _write_row(run_dir, "incumbent", "r2", _eval_result("r2", [("yes", "yes")]))  # weighted_score is None
        arms = arm_row_scores(run_dirs=[run_dir], variant_ids=["incumbent"], suite_id=SUITE)
        assert arms[0].row_scores == {"r1": 0.8}


class TestParetoFront:
    def _arm(self, name: str, **rows: float) -> ArmRowScores:
        return ArmRowScores(variant_id=name, row_scores=rows)

    def test_excludes_a_dominated_arm(self) -> None:
        better = self._arm("cand-a", r1=1.0, r2=1.0)
        worse = self._arm("cand-b", r1=0.5, r2=1.0)
        assert pareto_front([better, worse]) == ["cand-a"]

    def test_keeps_complementary_arms(self) -> None:
        # Each wins a disjoint row set — the merge opportunity a suite average hides.
        a = self._arm("cand-a", r1=1.0, r2=0.0)
        b = self._arm("cand-b", r1=0.0, r2=1.0)
        assert pareto_front([a, b]) == ["cand-a", "cand-b"]

    def test_identical_arms_all_stay_on_the_front(self) -> None:
        arms = [self._arm(f"cand-{n}", r1=0.7, r2=0.7) for n in "abc"]
        assert pareto_front(arms) == ["cand-a", "cand-b", "cand-c"]

    def test_a_single_arm_is_its_own_front(self) -> None:
        assert pareto_front([self._arm("only", r1=0.1)]) == ["only"]

    def test_empty_input(self) -> None:
        assert pareto_front([]) == []

    def test_a_hole_never_fabricates_domination(self) -> None:
        # `full` beats `holed` on r1 and never scored r2 at all. It is NOT entitled to dominate:
        # counting its missing cell as 0.0 would fabricate a loss for `holed` on a row it won, and
        # ignoring the row entirely would let an arm dominate on the subset it happens to share.
        # Domination requires COVERAGE of everything the other arm scored.
        full = ArmRowScores(variant_id="full", row_scores={"r1": 1.0})
        holed = ArmRowScores(variant_id="holed", row_scores={"r1": 0.5, "r2": 1.0})
        assert pareto_front([full, holed]) == ["full", "holed"]

    def test_an_arm_that_covers_everything_still_dominates(self) -> None:
        # The other direction: coverage is a precondition, not a way to survive by scoring MORE.
        covered = ArmRowScores(variant_id="covered", row_scores={"r1": 1.0, "r2": 1.0})
        partial = ArmRowScores(variant_id="partial", row_scores={"r1": 0.5})
        assert pareto_front([covered, partial]) == ["covered"]

    def test_a_nan_cell_does_not_take_the_front_by_incomparability(self) -> None:
        # Every `>=` against NaN is False, so an unguarded NaN arm is undominatable AND dominates
        # nobody — it lands on the front in bold beside arms that earned it. Treated as a hole, it
        # goes through the coverage rule instead: `poisoned` is then a one-row arm that `winner`
        # covers and beats.
        winner = ArmRowScores(variant_id="winner", row_scores={"r1": 1.0, "r2": 1.0})
        poisoned = ArmRowScores(variant_id="poisoned", row_scores={"r1": 0.5, "r2": float("nan")})
        assert pareto_front([winner, poisoned]) == ["winner"]

    def test_an_arm_whose_every_cell_is_non_finite_is_excluded(self) -> None:
        # Same rule as an arm that scored no rows: nothing about an empty vector is a win.
        real = ArmRowScores(variant_id="real", row_scores={"r1": 0.5})
        broken = ArmRowScores(variant_id="broken", row_scores={"r1": float("nan"), "r2": float("inf")})
        assert pareto_front([real, broken]) == ["real"]


class TestEveryFrontGuardsNonFiniteScores:
    """The claim `cost_quality_front`'s docstring and CLAUDE.md both make, asserted rather than read.

    The three fronts answer different questions and guard by different mechanisms — a hole on the
    coverage front, a skipped maximum on GEPA's, an excluded arm on the cost one. What has to agree
    is the OUTCOME: a non-finite cell never wins anything and never makes its arm undominatable.
    """

    _CLEAN = ArmRowScores(variant_id="clean", row_scores={"r1": 1.0, "r2": 1.0})
    _POISONED = ArmRowScores(variant_id="poisoned", row_scores={"r1": 0.5, "r2": float("nan")})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
    def test_no_front_carries_the_non_finite_arm(self, bad: float) -> None:
        poisoned = ArmRowScores(variant_id="poisoned", row_scores={"r1": 0.5, "r2": bad})
        arms = [self._CLEAN, poisoned]
        assert pareto_front(arms) == ["clean"]
        assert instance_best_front(arms) == ["clean"]
        points = [
            CostQualityPoint("clean", 1.0, 0.5, frozenset({"r1", "r2"})),
            CostQualityPoint("poisoned", bad, 0.1, frozenset({"r1", "r2"})),
        ]
        assert cost_quality_front(points) == ["clean"]

    def test_the_finite_rows_of_a_mixed_arm_still_participate(self) -> None:
        # Not a stricter rule than `instance_best_front`'s: a NaN cell is dropped, the arm is not.
        # `poisoned` still owns r1, so both row-vector fronts keep it.
        poisoned = ArmRowScores(variant_id="poisoned", row_scores={"r1": 1.0, "r2": float("nan")})
        other = ArmRowScores(variant_id="other", row_scores={"r1": 0.5, "r2": 0.9})
        assert pareto_front([poisoned, other]) == ["poisoned", "other"]
        assert instance_best_front([poisoned, other]) == ["poisoned", "other"]


class TestRenderRowMatrix:
    def test_renders_holes_as_dash_and_says_they_were_excluded(self) -> None:
        arms = [
            ArmRowScores(variant_id="incumbent", row_scores={"r1": 0.5}),
            ArmRowScores(variant_id="cand-a", row_scores={"r1": 1.0, "r2": 1.0}),
        ]
        text = render_row_matrix(arms, pareto_front(arms))
        assert "| r2 | — | 1.000 |" in text
        assert "excluded from the domination" in text
        assert "**cand-a**" in text

    def test_flags_a_row_no_arm_scored(self) -> None:
        arms = [
            ArmRowScores(variant_id="a", row_scores={"r1": 0.0, "r2": 1.0}),
            ArmRowScores(variant_id="b", row_scores={"r1": 0.0, "r2": 0.5}),
        ]
        text = render_row_matrix(arms, pareto_front(arms))
        assert "Rows no arm scored above zero: r1" in text

    def test_empty_arms_render_a_sentence_not_a_broken_table(self) -> None:
        assert "No arms" in render_row_matrix([], [])


class TestAnArmThatScoredNothing:
    def test_is_not_on_the_front(self) -> None:
        # Nothing can COVER an empty vector, so the domination rule alone would make a candidate
        # that crashed on every row undominatable — and render it bold beside a real winner.
        real = ArmRowScores(variant_id="real", row_scores={"r1": 0.9})
        crashed = ArmRowScores(variant_id="crashed", row_scores={})
        assert pareto_front([real, crashed]) == ["real"]

    def test_is_named_in_the_matrix_as_a_wiring_problem(self) -> None:
        arms = [
            ArmRowScores(variant_id="real", row_scores={"r1": 0.9}),
            ArmRowScores(variant_id="crashed", row_scores={}),
        ]
        text = render_row_matrix(arms, pareto_front(arms))
        assert "scored no rows at all" in text
        assert "not a result" in text
        assert "**crashed**" not in text

    def test_all_arms_empty_yields_an_empty_front(self) -> None:
        arms = [ArmRowScores(variant_id=n, row_scores={}) for n in ("a", "b")]
        assert pareto_front(arms) == []


class TestUnbalancedReplicates:
    """Two arms that did the SAME thing on every row must not separate.

    A row's weight in an arm's pooled f1.yes is its observation count, so an arm that contributed
    three replicates for a row while the other contributed two has silently reweighted the
    comparison. The trigger is mundane — Stage B is three separate invocations, and one interrupted
    run leaves a partial row set. Measured before the fix: byte-identical labels on all 20 rows,
    f1.yes 0.818 vs 0.750, interval excluding zero, p = 0.022, rows_excluded 0, no note.
    """

    def _rows(self) -> dict[str, list[tuple[str, str]]]:
        # A mix the arms agree on exactly: 12 engage, 8 do not.
        return {f"r{i}": [("yes", "yes" if i % 5 else "no")] for i in range(20)}

    def _dirs(self, tmp_path: Path, *, truncate_after: int) -> list[Path]:
        rows = self._rows()
        run_dirs = []
        for i in range(3):
            run_dir = tmp_path / f"run-{i}"
            for n, (row_id, labels) in enumerate(sorted(rows.items())):
                # The incumbent's third invocation stopped part-way through.
                if not (i == 2 and n >= truncate_after):
                    _write_row(run_dir, "incumbent", row_id, _eval_result(row_id, labels))
                _write_row(run_dir, "candidate", row_id, _eval_result(row_id, labels))
            run_dirs.append(run_dir)
        return run_dirs

    def test_identical_arms_do_not_separate_when_one_run_was_interrupted(self, tmp_path: Path) -> None:
        verdict = _gate(self._dirs(tmp_path, truncate_after=12))
        assert verdict.incumbent_f1 == verdict.candidate_f1
        assert verdict.mean_diff == 0.0
        assert verdict.ci_low == verdict.ci_high == 0.0
        assert holm_promote([verdict])[0].promoted is False

    def test_the_trim_is_named_so_the_run_is_re_run_not_read(self, tmp_path: Path) -> None:
        verdict = _gate(self._dirs(tmp_path, truncate_after=12))
        note = " ".join(verdict.notes)
        assert "different replicate counts" in note
        assert "trimmed to the smaller count" in note
        assert "Re-run it" in note

    def test_balanced_arms_are_untouched(self, tmp_path: Path) -> None:
        verdict = _gate(self._dirs(tmp_path, truncate_after=20))
        assert not any("replicate counts" in n for n in verdict.notes)
        assert verdict.rows_paired == 20


class TestGuardrailScaleAndHoles:
    """Two ways the guardrail produced a number that meant something else."""

    def test_a_uniform_increase_is_judged_against_the_same_statistic_it_measures(self) -> None:
        """The interval is on the difference of MEANS, so the floor must scale by the mean.

        Per-row cost is strongly right-skewed. Measured against a median-scaled floor, a uniform
        +10% on `[0.01]*11 + [1.00]*9` rendered `FAIL ... 0.010 -> 0.011` against a 25% floor — a
        line that contradicts itself, and a real win killed by a unit mismatch.
        """
        costs = [0.01] * 11 + [1.00] * 9
        incumbent = _cost_rows({f"r{i}": [c] for i, c in enumerate(costs)})
        candidate = _cost_rows({f"r{i}": [c * 1.10] for i, c in enumerate(costs)})
        check = _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.passed is True, "a 10% increase must not breach a 25% floor"

    def test_a_genuinely_large_increase_still_fails_on_the_same_distribution(self) -> None:
        costs = [0.01] * 11 + [1.00] * 9
        incumbent = _cost_rows({f"r{i}": [c] for i, c in enumerate(costs)})
        candidate = _cost_rows({f"r{i}": [c * 2.0] for i, c in enumerate(costs)})
        assert _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate)).passed is False

    def test_a_row_measured_on_one_arm_only_cannot_fabricate_a_zero(self) -> None:
        """`mean([])` is 0.0, so an unfiltered empty cluster reads as "this arm cost nothing".

        Measured before the fix: incumbent $0.10/row, candidate $1.00 on half its rows and no cost
        recorded on the rest — a 10x increase PASSING with ci_low = -0.1, the incumbent's own mean
        negated by draws where the candidate contributed nothing.
        """
        incumbent = _cost_rows({f"r{i}": [0.10] for i in range(4)})
        candidate = _cost_rows({f"r{i}": ([1.00] if i < 2 else [None]) for i in range(4)})  # type: ignore[arg-type]
        check = _cost_check(cost_latency_guardrails(incumbent_rows=incumbent, candidate_rows=candidate))
        assert check.ci_low is not None and check.ci_low > 0.0, "the interval must not include the fabricated zero"
        assert check.passed is False, "a 10x cost increase must breach the floor"


class TestAnErroredRowIsAHoleNotAZero:
    def test_on_the_execution_track_too(self, tmp_path: Path) -> None:
        """`weighted_score` is 0.0 for an empty result list, so an errored row looked scored.

        Measured before the fix: arm B, identical to A except that it ERRORED on r1, was dropped
        from the Pareto front — discarded for crashing, with no `—` in the matrix to show why.
        """
        run_dir = tmp_path / "run-0"
        _write_row(run_dir, "a", "r0", _scored_result("r0", 0.5))
        _write_row(run_dir, "a", "r1", _scored_result("r1", 0.5))
        _write_row(run_dir, "b", "r0", _scored_result("r0", 0.5))
        errored = _eval_result("r1", []).model_copy(update={"weighted_score": 0.0})
        _write_row(run_dir, "b", "r1", errored)

        arms = arm_row_scores(run_dirs=[run_dir], variant_ids=["a", "b"], suite_id=SUITE)
        assert arms[1].row_scores == {"r0": 0.5}, "the errored row must be ABSENT, not 0.0"
        assert pareto_front(arms) == ["a", "b"]
        assert "| r1 | 0.500 | — |" in render_row_matrix(arms, pareto_front(arms))


class TestGateResampleCount:
    """The gate decides on the p; a report renders it. The two counts are deliberately different."""

    def test_gate_defaults_to_the_gate_resample_count(self, tmp_path: Path) -> None:
        # The one test that exercises the SIGNATURE default — everything else passes a small count.
        incumbent = {f"r{i}": [("yes", "yes" if i < 3 else "no")] for i in range(12)}
        candidate = {f"r{i}": [("yes", "yes")] for i in range(12)}
        verdict = activation_gate(
            incumbent_run_dirs=_shared_dirs(tmp_path, incumbent, candidate),
            candidate_run_dirs=_shared_dirs(tmp_path / "b", incumbent, candidate),
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
        )
        assert verdict.n_resamples == GATE_RESAMPLES
        assert GATE_RESAMPLES > BOOTSTRAP_RESAMPLES

    def test_gate_resamples_is_derived_from_its_inputs(self) -> None:
        # Recomputed, so the constant cannot be hand-edited away from its own derivation.
        import math

        assert math.ceil(2.0 / (GATE_P_PRECISION**2 * (DEFAULT_ALPHA / GATE_MAX_FAMILY))) == GATE_RESAMPLES

    def test_every_gate_default_uses_the_gate_resample_count(self) -> None:
        """The guardrail against a future gate function inheriting the report-grade default.

        Derived from the module rather than a list here: a new public callable that takes
        `n_resamples` is covered without anyone remembering to extend this.
        """
        import inspect

        import coder_eval.optimize_gate as gate

        offenders = []
        for name in dir(gate):
            if name.startswith("_"):
                continue
            obj = getattr(gate, name)
            if not callable(obj) or getattr(obj, "__module__", None) != gate.__name__:
                continue
            try:
                param = inspect.signature(obj).parameters.get("n_resamples")
            except (TypeError, ValueError):  # not introspectable
                continue
            if param is not None and param.default is not GATE_RESAMPLES:
                offenders.append(f"{name}(n_resamples={param.default!r})")
        assert not offenders, (
            f"{offenders} default to something other than GATE_RESAMPLES. Everything in this module "
            "feeds a promotion decision, and BOOTSTRAP_RESAMPLES is the report-grade count — at 2,000 "
            "draws a perfect 8-row candidate's p straddles its own Holm threshold across seeds."
        )

    def test_alpha_is_declared_once(self) -> None:
        import inspect

        assert inspect.signature(holm_promote).parameters["alpha"].default == DEFAULT_ALPHA
        assert inspect.signature(holm_rejections).parameters["alpha"].default == DEFAULT_ALPHA
        # `0\.05(?!\d)` rather than a substring test: a bare `"0.05" in line` also matches 0.056
        # and 0.0501, so a comment quoting an unrelated measured figure would fail this for a
        # reason that has nothing to do with alpha.
        alpha_literal = re.compile(r"0\.05(?!\d)")
        for module in (*_OPTIMIZE_MODULES, "reports_stats"):
            source = _module_source(module)
            literals = [ln for ln in source.splitlines() if alpha_literal.search(ln) and "DEFAULT_ALPHA = " not in ln]
            assert not literals, f"{module}.py still spells 0.05 outside DEFAULT_ALPHA: {literals}"


class TestDiscretenessFloor:
    def test_matches_the_closed_form(self) -> None:
        # 2*(1-R/M)^M: the probability a resample draws NO discordant row, doubled for two tails.
        assert _discreteness_floor(6, 3, 20_000) == pytest.approx(2 * 0.5**6)
        assert _discreteness_floor(8, 4, 20_000) == pytest.approx(2 * 0.5**8)
        assert _discreteness_floor(12, 6, 20_000) == pytest.approx(2 * 0.5**12)

    def test_is_bounded_by_the_estimator_floor(self) -> None:
        # Every row discordant -> the analytic term is 0, so the arithmetic's own floor wins.
        assert _discreteness_floor(40, 40, 2000) == pytest.approx(bootstrap_p_floor(2000))

    def test_of_identical_arms_is_one(self) -> None:
        # Zero discordant rows: (1-0)^M = 1, doubled and clamped. Two identical arms cannot be
        # separated at any alpha, and that is the honest reading rather than a bug.
        assert _discreteness_floor(6, 0, 20_000) == 1.0

    def test_no_rows_is_one(self) -> None:
        assert _discreteness_floor(0, 0, 20_000) == 1.0

    def test_more_discordant_than_rows_does_not_go_negative(self) -> None:
        assert _discreteness_floor(4, 9, 2000) == pytest.approx(bootstrap_p_floor(2000))


class TestMinDiscordantRows:
    """The lever a refusal names: how many rows the arms must DISAGREE on, not how many rows.

    Every expectation is a literal verified against the shipped module, and `alpha` is bound from
    `DEFAULT_ALPHA` rather than spelled `0.05` — the same rule the prose sensors enforce on the
    surfaces, applied to the tests that pin them.
    """

    def test_reproduces_the_sizing_figures(self) -> None:
        assert min_discordant_rows(8, DEFAULT_ALPHA) == 3
        assert min_discordant_rows(10, DEFAULT_ALPHA) == 4
        assert min_discordant_rows(20, DEFAULT_ALPHA) == 4
        assert min_discordant_rows(20, DEFAULT_ALPHA / 5) == 5
        assert min_discordant_rows(6, 0.001) == 5

    def test_returns_the_smallest_count_that_clears(self) -> None:
        # The contract is "smallest", so the answer must clear and its predecessor must not.
        for n_rows, threshold in ((8, DEFAULT_ALPHA), (10, DEFAULT_ALPHA), (20, DEFAULT_ALPHA / 5)):
            required = min_discordant_rows(n_rows, threshold)
            assert required is not None
            assert _discreteness_floor(n_rows, required, GATE_RESAMPLES) <= threshold
            assert _discreteness_floor(n_rows, required - 1, GATE_RESAMPLES) > threshold

    def test_the_row_count_is_not_the_lever(self) -> None:
        # Holding R fixed and ADDING rows makes the floor rise, which is why "add rows" is the
        # wrong remedy and this function exists. Same shape as the docstring's worked figures.
        floors = [_discreteness_floor(m, 3, GATE_RESAMPLES) for m in (8, 10, 20)]
        assert floors == sorted(floors) and floors[0] < floors[-1]

    def test_no_rows_clears_nothing(self) -> None:
        assert min_discordant_rows(0, DEFAULT_ALPHA) is None
        assert min_discordant_rows(-3, DEFAULT_ALPHA) is None

    def test_a_threshold_above_one_is_cleared_by_a_single_row(self) -> None:
        # The floor is clamped at 1.0, so any threshold at or above it is met by R = 1. Not
        # special-cased into None, which would report "impossible" for the trivially possible.
        assert min_discordant_rows(10, 1.0) == 1
        assert min_discordant_rows(10, 1.5) == 1

    def test_an_unclearable_bar_returns_none_rather_than_n_rows(self) -> None:
        # Every row discordant leaves the estimator's own floor, so a threshold below THAT cannot
        # be met at any R — at any suite size, since that floor is a function of the draw count
        # alone. The caller must send the reader to n_resamples, not to rows.
        assert min_discordant_rows(6, bootstrap_p_floor(2_000) / 2, 2_000) is None


class TestHolmThreshold:
    def test_returns_alpha_over_s_for_the_smallest_and_alpha_for_the_largest(self) -> None:
        family = [0.001, 0.01, 0.04]
        assert _holm_threshold(family, 0.001, 0.05) == pytest.approx(0.05 / 3)
        assert _holm_threshold(family, 0.04, 0.05) == pytest.approx(0.05)

    def test_ties_take_the_strictest_rank(self) -> None:
        # sorted().index() returns the FIRST occurrence, so every tied verdict is decided against
        # alpha/S. Conservative in the refusal direction, which is the right way to be wrong here.
        assert _holm_threshold([0.01] * 4, 0.01, 0.05) == pytest.approx(0.05 / 4)


def _tiny_suite(positives: int, distractors: int) -> tuple[dict, dict]:
    """A suite the incumbent misses entirely and the candidate gets perfect, plus shared distractors."""
    incumbent = {f"p{i}": [("yes", "no")] for i in range(positives)}
    candidate = {f"p{i}": [("yes", "yes")] for i in range(positives)}
    for i in range(distractors):
        incumbent[f"d{i}"] = [("no", "no")]
        candidate[f"d{i}"] = [("no", "no")]
    return incumbent, candidate


class TestReusedRunDirIsRefused:
    """`run.json` is per-INVOCATION; the tree under it is APPEND-ONLY.

    A second `coder-eval run --run-dir <same dir> --split test` leaves the first split's rows on
    disk while rewriting `row_selection` to say `test`. The cross-split refusal cannot see it —
    provenance reads clean, single-valued — and because both arms are subdirectories of the SAME
    run dir the contamination is symmetric: the stale rows pair on both sides, so there is no
    `rows_excluded` bump and no unpaired-rows note. The only trace is a `rows_paired` larger than
    the split, which nothing else flags.
    """

    def _clean(self, tmp_path: Path) -> Path:
        run_dir = tmp_path / "run-0"
        for row in ("t1", "t2", "t3"):
            _write_row(run_dir, "incumbent", row, _eval_result(row, [("yes", "no")]))
            _write_row(run_dir, "candidate", row, _eval_result(row, [("yes", "yes")]))
        return run_dir

    def _gate_one(self, run_dir: Path, **kwargs):
        return activation_gate(
            incumbent_run_dirs=[run_dir],
            candidate_run_dirs=[run_dir],
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=_FAST_RESAMPLES,
            **kwargs,
        )

    def test_the_defect_end_to_end_a_tree_holding_two_splits_is_refused(self, tmp_path: Path) -> None:
        """THE test that pins the finding. Before this preflight it returned a confident interval."""
        run_dir = self._clean(tmp_path)
        # The earlier invocation's train rows, still on disk, described by no run.json.
        for row in ("r1", "r2"):
            _write_row(run_dir, "incumbent", row, _eval_result(row, [("yes", "no")]), record=False)
            _write_row(run_dir, "candidate", row, _eval_result(row, [("yes", "yes")]), record=False)

        # Symmetric contamination: both arms pair the stale rows, so nothing else notices.
        assert set(load_suite_rows(run_dir, "incumbent", SUITE)) == {"t1", "t2", "t3", "r1", "r2"}

        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is not None
        assert (verdict.mean_diff, verdict.ci_low, verdict.ci_high, verdict.p_value) == (None, None, None, None)
        assert (verdict.incumbent_f1, verdict.candidate_f1) == (None, None)
        # Per LOCATION, and actionable: how many stale results, across how many rows, WHERE.
        # A tree-wide arm x dir total would be unreconcilable with the `Rows paired` line the
        # same block prints four lines below it.
        assert "2 result(s) across 2 row(s)" in verdict.gate_refusal
        assert "incumbent" in verdict.gate_refusal and "candidate" in verdict.gate_refusal
        assert "r1/00" in verdict.gate_refusal and "fresh --run-dir" in verdict.gate_refusal

    def test_it_renders_as_not_a_result_and_carries_no_p(self, tmp_path: Path) -> None:
        # A wiring refusal, so it joins the `NOT A RESULT` family — distinguishable in a ledger
        # from the discreteness refusal, which is the only one that ever carries a p.
        run_dir = self._clean(tmp_path)
        _write_row(run_dir, "candidate", "stale", _eval_result("stale", [("yes", "yes")]), record=False)
        decided = holm_promote([self._gate_one(run_dir)])[0]
        assert decided.p_value is None and decided.promoted is False
        text = render_markdown(decided)
        assert "NOT A RESULT" in text
        assert "CANNOT SEPARATE AT THIS SIZE" not in text

    def test_a_stale_replicate_inside_a_recorded_row_is_refused(self, tmp_path: Path) -> None:
        """Row ids alone are blind one level down, and the trigger is mundane.

        Re-using a run dir with a smaller `--repeats` leaves the earlier call's `<NN>` dirs inside
        rows the new run.json DOES record. `load_suite_rows` pools every replicate it finds and
        `_balance_pair` trims symmetrically — so, again, nothing else flags it and the gate returns
        a confident interval over contaminated clusters.
        """
        run_dir = self._clean(tmp_path)  # every row recorded at replicate 00
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            _write_row(run_dir, variant, "t1", _eval_result("t1", [("yes", observed)]), 1, record=False)
        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is not None
        assert "t1/01" in verdict.gate_refusal

    def test_a_recorded_replicate_is_not_flagged(self, tmp_path: Path) -> None:
        # The anti-over-fire half of the replicate key: a legitimate --repeats 2 run records both.
        run_dir = self._clean(tmp_path)
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            for row in ("t1", "t2", "t3"):
                _write_row(run_dir, variant, row, _eval_result(row, [("yes", observed)]), 1)
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_entry_with_no_replicate_index_covers_every_replicate_of_its_row(self, tmp_path: Path) -> None:
        # Permissive on ambiguity, exactly as for a missing variant_id: an unattributable entry
        # means "cannot rule this one in", not "this one is stale".
        run_dir = self._clean(tmp_path)
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            _write_row(run_dir, variant, "t1", _eval_result("t1", [("yes", observed)]), 1, record=False)
        payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        for entry in payload["task_results"]:
            entry.pop("replicate_index", None)
        (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_unreadable_suite_directory_degrades_to_a_note(self, tmp_path: Path) -> None:
        # `iterdir` can raise; the function's contract is to degrade to "cannot tell", exactly as
        # `read_split_provenance` does for an unreadable run.json.
        run_dir = self._clean(tmp_path)
        suite_dir = run_dir / "candidate" / SUITE
        suite_dir.chmod(0o000)
        try:
            verdict = self._gate_one(run_dir)
        finally:
            suite_dir.chmod(0o755)
        assert verdict.gate_refusal is None
        assert any("cannot be reconciled" in note for note in verdict.notes)

    def test_the_arms_in_different_run_dirs_name_both_locations(self, tmp_path: Path) -> None:
        inc_dir, cand_dir = tmp_path / "inc", tmp_path / "cand"
        for run_dir, variant, observed in ((inc_dir, "incumbent", "no"), (cand_dir, "candidate", "yes")):
            for row in ("t1", "t2", "t3"):
                _write_row(run_dir, variant, row, _eval_result(row, [("yes", observed)]))
            _write_row(run_dir, variant, "stale", _eval_result("stale", [("yes", observed)]), record=False)
        verdict = activation_gate(
            incumbent_run_dirs=[inc_dir],
            candidate_run_dirs=[cand_dir],
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=_FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        assert str(inc_dir) in verdict.gate_refusal and str(cand_dir) in verdict.gate_refusal

    def test_more_than_three_stale_results_are_truncated_with_an_ellipsis(self, tmp_path: Path) -> None:
        run_dir = self._clean(tmp_path)
        for row in ("s1", "s2", "s3", "s4"):
            _write_row(run_dir, "candidate", row, _eval_result(row, [("yes", "yes")]), record=False)
        refusal = self._gate_one(run_dir).gate_refusal
        assert refusal is not None
        assert "4 result(s) across 4 row(s)" in refusal and "…" in refusal

    def test_an_unreconcilable_sibling_dir_is_named_in_the_refusal(self, tmp_path: Path) -> None:
        # The note that would say so is unreachable past the refusal's return, so the refusal
        # itself has to carry it — otherwise its totals silently exclude a whole directory.
        dirty, opaque = tmp_path / "dirty", tmp_path / "opaque"
        for run_dir in (dirty, opaque):
            for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
                for row in ("t1", "t2", "t3"):
                    _write_row(run_dir, variant, row, _eval_result(row, [("yes", observed)]))
        _write_row(dirty, "candidate", "stale", _eval_result("stale", [("yes", "yes")]), record=False)
        (opaque / "run.json").unlink()
        verdict = activation_gate(
            incumbent_run_dirs=[dirty, opaque],
            candidate_run_dirs=[dirty, opaque],
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=_FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        assert "could not be reconciled either way" in verdict.gate_refusal

    def test_a_clean_run_dir_is_neither_refused_nor_noted(self, tmp_path: Path) -> None:
        # The anti-over-fire test, and the overwhelmingly common path: the guard must not fire by
        # default, and must not add a note to every block either.
        verdict = self._gate_one(self._clean(tmp_path))
        assert verdict.gate_refusal is None
        assert not any("task_results" in note or "re-used --run-dir" in note for note in verdict.notes)

    def test_a_missing_run_json_degrades_to_a_note(self, tmp_path: Path) -> None:
        run_dir = self._clean(tmp_path)
        (run_dir / "run.json").unlink()
        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert any("cannot be reconciled" in note for note in verdict.notes)

    def test_a_malformed_run_json_degrades_to_a_note(self, tmp_path: Path) -> None:
        run_dir = self._clean(tmp_path)
        (run_dir / "run.json").write_text('{"task_results": [', encoding="utf-8")
        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert any("cannot be reconciled" in note for note in verdict.notes)

    def test_a_run_json_predating_task_results_degrades_to_a_note(self, tmp_path: Path) -> None:
        # Old run dirs must stay gatable: the one state where contamination is undetectable must
        # not also be the one state that refuses everything.
        run_dir = self._clean(tmp_path)
        (run_dir / "run.json").write_text(json.dumps({_RUN_SELECTION_KEY: {"split": None}}), encoding="utf-8")
        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert any("cannot be reconciled" in note for note in verdict.notes)

    def test_a_tree_holding_fewer_rows_than_recorded_does_not_refuse(self, tmp_path: Path) -> None:
        # An interrupted write or a deleted row is not this defect; only rows the run.json never
        # wrote are. Nothing is unrecorded here, so there is nothing to refuse.
        run_dir = self._clean(tmp_path)
        shutil.rmtree(run_dir / "incumbent" / SUITE / "t3")
        shutil.rmtree(run_dir / "candidate" / SUITE / "t3")
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_entry_with_no_variant_id_counts_for_every_variant(self, tmp_path: Path) -> None:
        # Permissive on ambiguity: the harm is a FALSE refusal blocking a real promotion, and an
        # unattributable entry means "cannot rule this row in", not "this row is stale".
        run_dir = self._clean(tmp_path)
        _write_row(run_dir, "incumbent", "extra", _eval_result("extra", [("yes", "no")]), record=False)
        _write_row(run_dir, "candidate", "extra", _eval_result("extra", [("yes", "yes")]), record=False)
        payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        payload["task_results"].append({"task_id": f"{SUITE}/extra"})  # no variant_id
        (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        assert self._gate_one(run_dir).gate_refusal is None

    def test_another_suite_in_the_same_run_dir_is_not_mistaken_for_a_stale_row(self, tmp_path: Path) -> None:
        # A run dir legitimately holds several suites and variants. The reconciliation is scoped to
        # the arms' own suite, so a sibling suite's rows are neither counted nor blamed.
        run_dir = self._clean(tmp_path)
        other = run_dir / "incumbent" / "some-other-suite" / "x" / "00"
        other.mkdir(parents=True)
        (other / "task.json").write_text(_eval_result("x", [("yes", "yes")]).model_dump_json(), encoding="utf-8")
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_empty_row_directory_is_not_a_row(self, tmp_path: Path) -> None:
        # A directory holding no task.json is not a scored row — the reconciliation reads names but
        # still requires the replicate glob to match, so a stray mkdir cannot fabricate a refusal.
        run_dir = self._clean(tmp_path)
        (run_dir / "candidate" / SUITE / "leftover-dir").mkdir()
        assert self._gate_one(run_dir).gate_refusal is None

    def test_an_aggregate_rebuilt_run_dir_does_not_false_positive(self, tmp_path: Path) -> None:
        """`coder-eval aggregate` rebuilds run.json FROM the tree, so it must never be refused.

        Built through the REAL rebuild path (`recover_task_results` -> `build_run_summary` ->
        `write_run_summary`), not by hand-writing a run.json that assumes the answer. It is also
        this check's documented blind spot from the other side: because the record is derived from
        the tree, an already-contaminated dir is LAUNDERED into a clean reading by an aggregate.
        That is stated on `reconcile_tree_against_run_json` and accepted.
        """
        from coder_eval.orchestration.batch import build_run_summary, recover_task_results, write_run_summary

        run_dir = tmp_path / "resumed"
        # Rows on disk carrying their own variant_id, as a real run writes them. `record=False`
        # because the rebuild below is what writes run.json here — that IS the resume path.
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            for row in ("t1", "t2", "t3"):
                result = copy_with(_eval_result(row, [("yes", observed)]), variant_id=variant)
                _write_row(run_dir, variant, row, result, record=False)
        (run_dir / "run.json").unlink()  # the interrupted run left none

        recovered = recover_task_results(run_dir)
        assert len(recovered) == 6, "fixture: the rebuild must see every row on disk"
        summary = build_run_summary("resumed", recovered, datetime(2026, 8, 16), datetime(2026, 8, 16))
        write_run_summary(summary, run_dir)

        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert not any("cannot be reconciled" in note for note in verdict.notes)
        assert verdict.rows_paired == 3

    def test_a_resumed_run_dir_does_not_false_positive(self, tmp_path: Path) -> None:
        """The one assumption that would have invalidated this preflight, checked on the real path.

        `--resume` does NOT go through `recover_task_results` — that is `aggregate`'s. It calls
        `partition_for_resume` over the RESOLVED task set, reloads the already-finished ones into
        `prior_results`, and `run_batch` folds `[*prior_results, *processed]` into the summary. So
        run.json describes the whole resolved set, including rows this invocation did not execute.
        Modelled exactly: three rows already on disk from the interrupted run, one more executed
        now, and the summary built from the union — as `batch.py` does.
        """
        from coder_eval.orchestration.batch import build_run_summary, partition_for_resume, write_run_summary
        from coder_eval.path_utils import build_task_run_dir

        run_dir = tmp_path / "resumed"
        rows = ("t1", "t2", "t3")
        for variant, observed in (("incumbent", "no"), ("candidate", "yes")):
            for row in rows:
                result = copy_with(_eval_result(row, [("yes", observed)]), variant_id=variant)
                _write_row(run_dir, variant, row, result, record=False)
        (run_dir / "run.json").unlink()  # the interrupted run left none

        # `partition_for_resume` reads the RESOLVED set — what this invocation would run — and
        # finds every one of them already finished on disk.
        resolved = [
            ResolvedTask(
                task=TaskDefinition(
                    task_id=f"{SUITE}/{row}",
                    description="row",
                    initial_prompt="p",
                    success_criteria=[FileExistsCriterion(path="x", description="x")],
                ),
                task_file=Path("t.yaml"),
                # The PER-TASK dir: `_load_completed_result` reads `rt.run_dir / "task.json"`.
                run_dir=build_task_run_dir(run_dir, variant, f"{SUITE}/{row}"),
                variant_id=variant,
            )
            for variant in ("incumbent", "candidate")
            for row in rows
        ]
        to_run, prior_results, _prior_resolved = partition_for_resume(resolved)
        assert (len(to_run), len(prior_results)) == (0, 6), "fixture: every row must read as finished"

        # `run_batch` folds `[*prior_results, *processed]` into the summary; nothing new ran here.
        summary = build_run_summary("resumed", list(prior_results), datetime(2026, 8, 16), datetime(2026, 8, 16))
        write_run_summary(summary, run_dir)

        verdict = self._gate_one(run_dir)
        assert verdict.gate_refusal is None
        assert not any("cannot be reconciled" in note for note in verdict.notes)

    def test_an_empty_run_dir_is_not_refused(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "empty"
        run_dir.mkdir()
        (run_dir / "run.json").write_text(json.dumps({"task_results": []}), encoding="utf-8")
        assert self._gate_one(run_dir).gate_refusal is None


_NEGATIVE_RESULT_FRAGMENTS = (
    "not promoted:",
    "did not clear the Holm threshold",
    "still contains zero",
    "the interval separates",
    "FAILED — this forces",
)

_MDE_ADVISORY_FRAGMENTS = (
    "minimum detectable effect",
    "could not be priced",
    "tighter than this suite's own noise floor",
)


def _negative_result_notes(notes: list[str]) -> list[str]:
    """Every note that reads as a claim about a CANDIDATE that lost.

    Read from the note LIST, never by `not in` over the rendered page: the block legitimately
    contains the words "not promoted" in other sentences, and a `.replace()`d-string absence
    assertion is the vacuity trap this repo has already been bitten by.
    """
    return [note for note in notes if any(fragment in note for fragment in _NEGATIVE_RESULT_FRAGMENTS)]


class TestARefusalSuppressesNegativeResultProse:
    """Under a refusal, no note may claim the candidate lost — on EITHER track.

    A refusal says the comparison could not decide anything, so a negative-result note beneath it
    is a second, contradictory claim in one block — on the page a user pastes into a promotion
    ledger. `holm_promote_execution` has always guarded its whole ladder with one `if not refused:`;
    `holm_promote` guarded exactly one of its four rungs, so three fired regardless.

    The cross-product below is the point. Written as hand-picked cases, the three unguarded rungs
    were individually plausible and nobody noticed the combination; asserting ONE RULE over every
    combination is what makes the ladder a contract rather than three independently-remembered
    guards.

    **The MDE axis is deliberately absent from it, and that is a layering fact rather than a gap.**
    The full invariant is "under a refusal, no negative-result note AND no MDE advisory". Neither
    `holm_promote` nor `holm_promote_execution` can emit an MDE advisory — those are written in
    `activation_gate` and `_execution_diagnostics`, before either Holm wrapper runs — so varying
    `mde` here would add inert cases. The advisory half is asserted where it is producible:
    `TestExecutionGateRefusesAReusedRunDir::test_refused_already_is_reachable_as_true` and
    `TestExecutionDiagnostics::test_refused_already_suppresses_both_advisory_notes`, which cover a
    branch each.

    **On the activation track the exclusion is also a real asymmetry, not only layering.** A
    discreteness refusal is set inside `holm_promote`, AFTER `activation_gate` has already written
    its MDE notes, and nothing suppresses them — unlike the execution track, where
    `refused_already` does. Both are resolution statements, so they reinforce the refusal rather
    than contradict it and `promoted` is unaffected; it is stated here rather than presented as
    pure layering.
    """

    @pytest.mark.parametrize("mean_diff", [0.5, -0.3, 0.0])
    @pytest.mark.parametrize(("ci_low", "ci_high"), [(0.2, 0.75), (-0.5, -0.1), (-0.05, 0.6)])
    @pytest.mark.parametrize("p_value", [0.001, 0.9])
    @pytest.mark.parametrize("failing_sibling", [False, True])
    def test_the_activation_ladder_is_silent_under_a_refusal(
        self, mean_diff, ci_low, ci_high, p_value, failing_sibling
    ) -> None:
        sibling = [
            GuardrailCheck(
                name="sibling recall.yes [criterion 1]",
                incumbent=1.0,
                candidate=0.5,
                relative_change=-0.5,
                tolerance=0.0,
                passed=False,
            )
        ]
        # `p_floor` above every Holm threshold in a family of one, so `_refusal_message` fires
        # whatever the other axes do.
        refused = _parity_activation(
            mean_diff=mean_diff,
            ci_low=ci_low,
            ci_high=ci_high,
            p_value=p_value,
            p_floor=0.9,
            n_discordant=1,
            sibling_checks=sibling if failing_sibling else [],
            guardrails=[_failing_cost_check()],
        )
        decided = holm_promote([refused])[0]
        assert decided.gate_refusal is not None, "the fixture must refuse for this to mean anything"
        assert decided.promoted is False
        assert _negative_result_notes(decided.notes) == [], decided.notes
        assert _headline(render_markdown(decided)).startswith("CANNOT SEPARATE AT THIS SIZE")

    @pytest.mark.parametrize("mean_diff", [0.5, -0.3])
    @pytest.mark.parametrize(("ci_low", "ci_high"), [(0.2, 0.75), (-0.5, -0.1), (-0.05, 0.6)])
    @pytest.mark.parametrize("p_value", [0.001, 0.9])
    def test_the_execution_ladder_is_silent_under_a_refusal(self, mean_diff, ci_low, ci_high, p_value) -> None:
        refused = _parity_execution(
            mean_diff=mean_diff,
            ci_low=ci_low,
            ci_high=ci_high,
            p_value=p_value,
            gate_refusal="the observed difference is below this suite's MDE",
            guardrails=[_failing_cost_check()],
        )
        decided = holm_promote_execution([refused])[0]
        assert decided.promoted is False
        assert _negative_result_notes(decided.notes) == [], decided.notes
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT")

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            # Three of these four sentences are SHARED `_note_*` declarations, so the expected
            # fragment is the same on both tracks. The wrong-direction rung is deliberately not
            # shared — activation says "the interval separates in the incumbent's favour", the
            # execution track says "the paired difference favours the incumbent", because each
            # names its own statistic — so only their common prefix is asserted here.
            ({"p_value": 0.9}, "did not clear the Holm threshold"),
            ({"mean_diff": -0.3, "ci_low": -0.5, "ci_high": -0.1}, "not promoted:"),
            ({"ci_low": -0.05}, "still contains zero"),
            ({"guardrails": [_failing_cost_check()]}, "FAILED — this forces"),
        ],
        ids=["lost", "wrong-direction", "ci-contains-zero", "vetoed"],
    )
    def test_the_same_verdicts_do_produce_those_notes_without_a_refusal(self, gate, build, kwargs, expected) -> None:
        """Anti-vacuity for the suppression tests above, and for the fragment list itself.

        If `_NEGATIVE_RESULT_FRAGMENTS` stopped matching anything the suppression assertions would
        pass over an empty list forever — indistinguishable from a working guard. Every case here
        is witnessed against a verdict that genuinely earns a negative-result note. Three of the
        four pin the exact shared sentence; the wrong-direction case pins only the `"not promoted:"`
        prefix the two tracks share, because each names its own statistic after it.
        """
        decided = gate([build(**kwargs)])[0]
        assert decided.gate_refusal is None, "this half must NOT refuse"
        notes = _negative_result_notes(decided.notes)
        assert notes, decided.notes
        assert any(expected in note for note in notes), notes

    def test_the_reproduced_contradiction_is_gone(self) -> None:
        """The exact verdict the reviewer reproduced against shipped code.

        It rendered `**CANNOT SEPARATE AT THIS SIZE — … so this is NOT a negative result about
        it.**` with `not promoted: the interval separates in the incumbent's favour.` directly
        beneath. `promoted` was already correctly False — the defect was confined to the prose.
        """
        verdict = _parity_activation(
            p_value=0.01, p_floor=0.2, mean_diff=-0.3, ci_low=-0.5, ci_high=-0.1, rows_paired=6, n_discordant=1
        )
        decided = holm_promote([verdict])[0]
        assert decided.gate_refusal is not None
        assert _negative_result_notes(decided.notes) == [], decided.notes
        assert decided.promoted is False

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_family_and_near_floor_notes_survive_a_refusal(self, gate, build) -> None:
        """They are NOT negative-result rungs, and must not be swept into the guard.

        "Holm applied across a family of N" stays true under a refusal, and the execution track
        keeps its family note outside `if not refused:` for exactly that reason. Sweeping them in
        would leave a refused block unable to say what family it was decided in.
        """
        refusing = {"p_floor": 0.9, "n_discordant": 1} if build is _parity_activation else {"gate_refusal": "refused"}
        decided = gate([build(**refusing)])[0]
        assert decided.gate_refusal is not None
        assert any("Holm applied across a family of" in note for note in decided.notes)
        if build is _parity_activation:
            # The near-floor note is the other rung that must NOT be swept in: it is a statement
            # about the DRAW COUNT's resolution, which stays true whatever the refusal says. The
            # fixture's p = 0.001 against a 2,000-draw floor of 0.0010 puts it inside
            # NEAR_FLOOR_MULTIPLE, so this is a real witness rather than a vacuous branch.
            assert any("at or near this bootstrap's resolution floor" in note for note in decided.notes)


class TestGateRefusal:
    """A suite that structurally cannot separate is REFUSED, not reported as a negative result."""

    # Resample-sensitive, so an explicit count rather than the helper's — but 2,000 rather than the
    # real GATE_RESAMPLES, because what these assert is the ANALYTIC term. At 6 rows / 3 discordant
    # the floor is 2*(1-3/6)^6 = 0.03125, and it dominates the estimator's 2/(m+1) for any m above
    # 63; 20,000 draws would compute the identical floor and take ten times as long.
    _REFUSAL_RESAMPLES = 2_000

    def _gated(self, tmp_path: Path, positives: int, distractors: int, family: int, **kwargs):
        incumbent, candidate = _tiny_suite(positives, distractors)
        run_dirs = _shared_dirs(tmp_path, incumbent, candidate)
        verdicts = [_gate(run_dirs, n_resamples=self._REFUSAL_RESAMPLES, **kwargs) for _ in range(family)]
        return holm_promote(verdicts)

    def test_six_row_suite_refuses_rather_than_reporting_a_negative(self, tmp_path: Path) -> None:
        # 6 rows, 3 discordant -> floor 0.031, against a family-of-2 Holm threshold of 0.025.
        decided = self._gated(tmp_path, positives=3, distractors=3, family=2)
        for verdict in decided:
            assert verdict.gate_refusal is not None
            assert verdict.promoted is False
            assert "1 survivor(s)" in verdict.gate_refusal
            text = render_markdown(verdict)
            assert "CANNOT SEPARATE AT THIS SIZE" in text
            assert "NOT PROMOTED" not in text

    def test_a_healthy_suite_does_not_refuse(self, tmp_path: Path) -> None:
        decided = self._gated(tmp_path, positives=6, distractors=6, family=2)
        for verdict in decided:
            assert verdict.gate_refusal is None
            assert verdict.promoted is True

    def test_a_refused_suite_stays_refused_across_seeds(self, tmp_path: Path) -> None:
        """Without `and refusal is None` this fails on roughly half the seeds.

        `p_floor` bounds the p's EXPECTATION, so a realized p dips below it about half the time.
        The guard, not the seed, is what decides.
        """
        for seed in range(8):
            decided = self._gated(tmp_path / f"s{seed}", positives=3, distractors=3, family=2, seed=seed)
            assert [v.promoted for v in decided] == [False, False], f"seed {seed} promoted an unpromotable suite"

    def test_refusal_does_not_outrank_undecided(self) -> None:
        # Holm never ran, so there is no threshold for the floor to be refused against.
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=GATE_RESAMPLES,
            rows_paired=6,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.03,
            p_floor=0.9,
        )
        text = render_markdown(verdict)
        assert "UNDECIDED" in text
        assert "CANNOT SEPARATE" not in text

    def test_refusal_ranks_above_a_failing_guardrail(self, tmp_path: Path) -> None:
        # Reading a guardrail presupposes a statistic that separated, which a refused suite's
        # did not — so the refusal is the headline.
        failing = GuardrailCheck(
            name="cost (USD/row)",
            incumbent=1.0,
            candidate=2.0,
            relative_change=1.0,
            tolerance=MATERIALITY_FLOOR,
            ci_low=0.6,
            ci_high=1.4,
            passed=False,
        )
        incumbent, candidate = _tiny_suite(3, 3)
        run_dirs = _shared_dirs(tmp_path, incumbent, candidate)
        base = _gate(run_dirs, n_resamples=self._REFUSAL_RESAMPLES)
        decided = holm_promote([base.model_copy(update={"guardrails": [failing]})] * 2)
        text = render_markdown(decided[0])
        assert "CANNOT SEPARATE AT THIS SIZE" in text
        assert "BLOCKED BY A GUARDRAIL" not in text

    def test_a_refused_verdict_is_never_promoted(self) -> None:
        # The Monte-Carlo undershoot, constructed directly: p BELOW p_floor, floor above the bar.
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=GATE_RESAMPLES,
            rows_paired=6,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.0001,
            p_floor=0.03125,
        )
        decided = holm_promote([verdict, verdict])[0]
        assert decided.promoted is False
        assert decided.gate_refusal is not None
        text = render_markdown(decided)
        assert "CANNOT SEPARATE AT THIS SIZE" in text
        assert "**PROMOTED**" not in text

    def test_the_refusal_is_not_duplicated_into_notes(self, tmp_path: Path) -> None:
        decided = self._gated(tmp_path, positives=3, distractors=3, family=2)[0]
        assert decided.gate_refusal is not None
        assert not any("cannot express a p below" in note for note in decided.notes)
        # And the ordinary negative-result note is suppressed too — it would contradict the headline.
        assert not any("ordinary negative result" in note for note in decided.notes)

    def test_identical_arms_are_diagnosed_as_the_candidate_not_the_suite(self, tmp_path: Path) -> None:
        """Zero discordant rows is a DIFFERENT finding, and the remedy is not "add rows".

        `2*(1-R/M)**M` shrinks with M only when the discordance RATE is non-zero, so at R=0 the
        floor is 1.0 at every suite size — no number of extra rows changes it. Telling an operator
        to buy more rows for a candidate that behaved identically to the incumbent is the
        misdiagnosis this branch exists to prevent, and it is the most common degenerate outcome
        in this workflow (a wrong `plugins:` path gives exactly this shape).
        """
        rows = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(8)}
        verdict = _gate(_shared_dirs(tmp_path, rows, dict(rows)))
        assert verdict.p_floor == 1.0
        decided = holm_promote([verdict, verdict])[0]
        assert decided.promoted is False
        assert decided.gate_refusal is not None
        assert "identical labels on every one of the 8 scored rows" in decided.gate_refusal
        assert "adding more rows LIKE THESE cannot change it" in decided.gate_refusal
        # And NOT the suite-size remedy, which would be false here.
        assert "survivor(s) at alpha" not in decided.gate_refusal
        assert "the answer is more rows" not in decided.gate_refusal
        assert "CANNOT SEPARATE AT THIS SIZE" in render_markdown(decided)

    def test_the_refusal_is_rank_scoped_not_a_claim_about_every_candidate(self, tmp_path: Path) -> None:
        # `p_floor` is a property of the SUITE and identical across the family, but `threshold`
        # depends on rank — so a worse-ranked sibling can escape the refusal and promote. A
        # message claiming "no candidate can promote here" would contradict its own block.
        decided = self._gated(tmp_path, positives=3, distractors=3, family=2)[0]
        assert decided.gate_refusal is not None
        assert "This candidate could not have promoted" in decided.gate_refusal
        assert "No candidate can promote here" not in decided.gate_refusal

    def test_the_remedy_names_the_discordant_count_that_would_clear_the_bar(self, tmp_path: Path) -> None:
        """10 rows, 3 discordant: the floor (0.056) exceeds alpha, and "add rows" is FALSE here.

        At R = 3 fixed, buying rows raises the floor. The honest remedy names the discordant count
        — 4 at this row count — beside the 3 the suite actually has.
        """
        decided = self._gated(tmp_path, positives=3, distractors=7, family=1)[0]
        assert decided.n_discordant == 3
        refusal = decided.gate_refusal
        assert refusal is not None
        required = min_discordant_rows(10, DEFAULT_ALPHA, self._REFUSAL_RESAMPLES)
        assert required == 4
        assert f"from {decided.n_discordant} to {required}" in refusal
        assert "makes this floor worse" in refusal
        # The sentence this replaces: unconditionally true only when the added rows are discordant.
        assert "the answer is more rows, not fewer candidates" not in refusal

    def test_the_identical_arms_message_carries_none_of_the_row_remedy(self, tmp_path: Path) -> None:
        # The R = 0 branch owns its own diagnosis; the discordance remedy must not leak into it.
        rows = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(8)}
        decided = holm_promote([_gate(_shared_dirs(tmp_path, rows, dict(rows)))] * 2)[0]
        assert decided.n_discordant == 0
        assert decided.gate_refusal is not None
        assert "DISAGREE on" not in decided.gate_refusal
        assert "identical labels on every one of the 8 scored rows" in decided.gate_refusal

    def test_a_verdict_without_a_discordant_count_says_nothing_about_one(self) -> None:
        # `n_discordant` is None on the no-interval path, and a remedy must never invent it.
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=GATE_RESAMPLES,
            rows_paired=6,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.01,
            p_floor=0.03125,
        )
        # A family of two: the floor (0.031) exceeds alpha/2, so the refusal fires.
        refusal = holm_promote([verdict, verdict])[0].gate_refusal
        assert refusal is not None
        assert "survivor(s) at alpha" in refusal
        assert "DISAGREE on" not in refusal

    def test_an_unclearable_bar_does_not_prescribe_rows_that_cannot_work(self) -> None:
        """When no discordant count clears the bar, rows are not the lever — and saying so is wrong.

        `min_discordant_rows` returns None exactly when the floor at `R == M` — which collapses to
        the estimator's own `2/(m+1)`, a function of the DRAW COUNT and of nothing about the suite
        — is still above the threshold. So a message telling that reader to add rows sends them to
        spend on something provably incapable of helping.
        """
        resamples = 200  # a coarse estimator floor, so the bar sits under it
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=resamples,
            rows_paired=6,
            rows_excluded=0,
            n_discordant=2,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.001,
            p_floor=0.4,
        )
        assert min_discordant_rows(6, DEFAULT_ALPHA / 8, resamples) is None
        refusal = holm_promote([verdict] * 8)[0].gate_refusal
        assert refusal is not None
        assert f"{resamples} bootstrap draws" in refusal
        assert "larger n_resamples" in refusal
        assert "more rows AND more disagreement" not in refusal

    def test_max_family_zero_says_no_family_size_works(self) -> None:
        verdict = ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=GATE_RESAMPLES,
            rows_paired=4,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.01,
            p_floor=0.5,  # alpha/0.5 < 1
        )
        decided = holm_promote([verdict])[0]
        assert decided.gate_refusal is not None
        assert "No family size works" in decided.gate_refusal


class TestPFloorOnTheVerdict:
    def test_the_gate_sets_it_from_the_discordant_rows(self, tmp_path: Path) -> None:
        incumbent, candidate = _tiny_suite(3, 3)
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.p_floor == pytest.approx(_discreteness_floor(6, 3, _FAST_RESAMPLES))

    def test_a_row_whose_replicates_are_reordered_is_concordant(self, tmp_path: Path) -> None:
        # Multiset comparison, not sequence: the same pairs in a different replicate order is the
        # same row on both arms, and counting it discordant would understate the floor.
        run_dirs = []
        for i in range(2):
            run_dir = tmp_path / f"run-{i}"
            for arm, order in (("incumbent", 0), ("candidate", 1)):
                for row in range(4):
                    labels = [("yes", "yes")] if (i == order) else [("yes", "no")]
                    _write_row(run_dir, arm, f"r{row}", _eval_result(f"r{row}", labels))
            run_dirs.append(run_dir)
        verdict = _gate(run_dirs)
        assert verdict.p_floor == pytest.approx(_discreteness_floor(4, 0, _FAST_RESAMPLES))

    def test_no_interval_means_no_floor(self, tmp_path: Path) -> None:
        verdict = _gate(_shared_dirs(tmp_path, {"r0": [("yes", "yes")]}, {"r0": [("yes", "yes")]}))
        assert verdict.p_value is None
        assert verdict.p_floor is None
        # `None`, not 0: "the arms agreed everywhere" is a finding, "there was no comparison" is not.
        assert verdict.n_discordant is None
        assert holm_promote([verdict])[0].gate_refusal is None

    def test_the_gate_records_the_count_the_floor_came_from(self, tmp_path: Path) -> None:
        incumbent, candidate = _tiny_suite(3, 7)
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate))
        assert (verdict.rows_paired, verdict.n_discordant) == (10, 3)
        assert verdict.p_floor == pytest.approx(_discreteness_floor(10, 3, _FAST_RESAMPLES))

    def test_all_rows_discordant_records_the_row_count(self, tmp_path: Path) -> None:
        incumbent, candidate = _tiny_suite(4, 0)
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate))
        assert verdict.n_discordant == verdict.rows_paired == 4

    def test_render_prints_the_discordant_count_beside_the_paired_one(self, tmp_path: Path) -> None:
        # The quantity `p_floor` is computed from, visible without having to trigger a refusal.
        incumbent, candidate = _tiny_suite(3, 7)
        verdict = _gate(_shared_dirs(tmp_path, incumbent, candidate))
        assert "Rows paired: 10 · discordant: 3 · excluded: 0" in render_markdown(verdict)

    def test_render_shows_a_dash_when_there_was_no_comparison(self, tmp_path: Path) -> None:
        verdict = _gate(_shared_dirs(tmp_path, {"r0": [("yes", "yes")]}, {"r0": [("yes", "yes")]}))
        assert "discordant: —" in render_markdown(verdict)

    def test_render_reports_both_floors(self, tmp_path: Path) -> None:
        incumbent, candidate = _tiny_suite(6, 6)
        verdict = holm_promote([_gate(_shared_dirs(tmp_path, incumbent, candidate))])[0]
        text = render_markdown(verdict)
        assert f"estimator {bootstrap_p_floor(_FAST_RESAMPLES):.4f}" in text
        assert verdict.p_floor is not None
        assert f"this suite {verdict.p_floor:.4f}" in text


def _weighted_arm(tmp_path: Path, variant: str, per_row: dict[str, list[float]], *, run_dirs: int = 1) -> list[Path]:
    """One arm whose rows carry N replicates each, spread across ``run_dirs`` directories.

    `weighted_score` is set EXPLICITLY on every result: it is `float | None` with default None,
    populated by the orchestrator, so a fixture that forgets it makes every cluster empty and the
    floor silently comes back None.
    """
    dirs = [tmp_path / f"run-{i}" for i in range(run_dirs)]
    for row_id, scores in per_row.items():
        for i, score in enumerate(scores):
            run_dir = dirs[i % run_dirs]
            replicate = i // run_dirs
            _write_row(run_dir, variant, row_id, _scored_result(row_id, score), replicate)
    return dirs


def _execution_floor(run_dirs: list[Path], **kwargs) -> NoiseFloor | None:
    return measure_execution_noise_floor(
        run_dirs=run_dirs,
        variant_id="incumbent",
        suite_id=SUITE,
        **{"model": "claude-haiku-4-5", "n_resamples": _FAST_RESAMPLES, **kwargs},
    )


class TestExecutionNoiseFloor:
    """The execution track's preflight: a null split over REPLICATES, on weighted_score."""

    def _spread(self) -> dict[str, list[float]]:
        # 8 rows x 3 replicates with real within-row spread, so the null comparison has something
        # to measure and the floor is above zero.
        return {f"r{i}": [0.3 + 0.1 * ((i + j) % 4) for j in range(3)] for i in range(8)}

    def test_splits_replicates_not_run_dirs(self, tmp_path: Path) -> None:
        # One run dir, 3 replicates per row -> a floor. The SAME data spread one-replicate-per-dir
        # across 3 dirs still pools to 3 replicates per row, so it also works; what does NOT is a
        # fixture where no row has 2 replicates at all.
        floor = _execution_floor(_weighted_arm(tmp_path, "incumbent", self._spread()))
        assert floor is not None and floor.mde > 0.0

        single = _weighted_arm(tmp_path / "b", "incumbent", {f"r{i}": [0.5] for i in range(8)})
        assert _execution_floor(single) is None

    def test_pools_replicates_across_run_dirs(self, tmp_path: Path) -> None:
        # The split axis is replicates, but they may arrive from several run directories — one
        # `--repeats 3` invocation or three separate ones both give a row 3 replicates.
        spread = _weighted_arm(tmp_path, "incumbent", self._spread(), run_dirs=3)
        assert _execution_floor(spread) is not None

    def test_is_none_without_enough_replicated_rows(self, tmp_path: Path) -> None:
        one_row = {"r0": [0.2, 0.9, 0.4], "r1": [0.5]}  # only r0 qualifies
        assert _execution_floor(_weighted_arm(tmp_path, "incumbent", one_row)) is None

    def test_is_none_when_weighted_score_is_unset(self, tmp_path: Path, caplog) -> None:
        # A result with criteria but no weighted_score yields None from _row_score, so every
        # cluster is empty. It must return None rather than a confident 0.0.
        run_dir = tmp_path / "run-0"
        for i in range(8):
            for replicate in range(3):
                _write_row(run_dir, "incumbent", f"r{i}", _eval_result(f"r{i}", [("yes", "yes")]), replicate)
        with caplog.at_level(logging.WARNING):
            assert _execution_floor([run_dir]) is None
        assert "carry 2+ replicates" in caplog.text

    def test_splits_three_replicates_two_one(self, tmp_path: Path) -> None:
        """Pinned, so a "tidy" change to len//2 (which gives 1/2) cannot silently reverse the bias.

        With 3 replicates the first half must hold 2 and the second 1: the larger half first keeps
        the interval conservative, exactly as the invocation split does.
        """
        # Two rows whose third replicate is an outlier. Under a 2/1 split the outlier sits alone in
        # the second half; under 1/2 it would be averaged with a middle value, narrowing the
        # interval. The two produce different floors, which is what makes this test discriminating.
        rows = {"r0": [0.1, 0.1, 0.9], "r1": [0.2, 0.2, 0.8], "r2": [0.3, 0.3, 0.7]}
        floor = _execution_floor(_weighted_arm(tmp_path, "incumbent", rows))
        assert floor is not None
        # first half mean = 0.1, second = 0.9 for r0 -> the diff is large and the floor is not 0.
        assert floor.mde > 0.1

    def test_reads_weighted_score_not_f1(self, tmp_path: Path) -> None:
        """The regression test for the exact bug N2 names.

        Labels are perfect on every replicate, so an F1 floor reads a confidently meaningless
        0.000 — while `weighted_score` varies and the real floor is above zero.
        """
        # The per-row spread has to VARY across rows: an identical replicate pattern on every row
        # makes every resampled difference identical, the interval zero-width, and the floor 0.0 —
        # which is correct arithmetic and would make this test pass for the wrong reason.
        run_dir = tmp_path / "run-0"
        for i in range(8):
            for replicate, score in enumerate((0.2 + 0.05 * i, 0.55, 0.9 - 0.05 * i)):
                result = _eval_result(f"r{i}", [("yes", "yes")]).model_copy(update={"weighted_score": score})
                _write_row(run_dir, "incumbent", f"r{i}", result, replicate)

        f1_floor = measure_noise_floor(
            run_dirs=[run_dir, run_dir],
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            model="claude-haiku-4-5",
            n_resamples=_FAST_RESAMPLES,
        )
        assert f1_floor is not None and f1_floor.mde == 0.0, "the F1 floor is the meaningless 0.000 N2 names"

        execution = _execution_floor([run_dir])
        assert execution is not None and execution.mde > 0.0

    def test_a_different_repeat_count_is_a_different_cache_entry(self, tmp_path: Path) -> None:
        """The replicate count is the split AXIS, so it has to key — and nothing else catches it.

        `n_invocations` is 1 for both a `--repeats 3` and a `--repeats 2` control run, so without
        `n_replicates` the two records share a key and `lookup_noise_floor` serves one for the
        other. Measured before the fix: 0.099 at 3 replicates against 0.169 at 2, same key.

        Round-tripped through the REAL cache rather than hand-built `NoiseFloor`s, because a
        hand-built probe cannot catch a field the producer forgets to set.
        """
        three = {f"r{i}": [0.2 + 0.05 * i, 0.55, 0.9 - 0.05 * i] for i in range(8)}
        two = {row: scores[:2] for row, scores in three.items()}

        floor_3 = _execution_floor(_weighted_arm(tmp_path / "a", "incumbent", three))
        floor_2 = _execution_floor(_weighted_arm(tmp_path / "b", "incumbent", two))
        assert floor_3 is not None and floor_2 is not None
        assert (floor_3.n_replicates, floor_2.n_replicates) == (3, 2)
        assert floor_3.mde != floor_2.mde, "the fixture no longer distinguishes replicate counts"

        sidecar = tmp_path / ".optimize-skill" / "my-skill" / "measurements.json"
        record_noise_floor(sidecar, floor_3)
        measurements = record_noise_floor(sidecar, floor_2)
        assert len(measurements.noise_floors) == 2, "the 2-replicate floor REPLACED the 3-replicate one"

        # And the round that actually ran at --repeats 3 gets its own number back.
        reused = _execution_floor(_weighted_arm(tmp_path / "c", "incumbent", three), measurements=measurements)
        assert reused is not None and reused.mde == floor_3.mde

    def test_rows_with_uneven_replicate_counts_are_balanced(self, tmp_path: Path) -> None:
        """An unbalanced row must not invent a floor out of nothing.

        `cluster_bootstrap_diff_ci` pools the drawn clusters' OBSERVATIONS before applying the
        statistic, so a 3-replicate row weighs 2:1 across the halves while a 2-replicate row weighs
        1:1 — and between-row spread then leaks into a difference that is zero by construction.
        These rows have NO within-row variance at all, so the only honest floor is 0.0. Measured
        before the balancing: 0.056.
        """
        uneven = {f"r{i}": [1.0 if i % 2 else 0.0] * (3 if i < 4 else 2) for i in range(8)}
        floor = _execution_floor(_weighted_arm(tmp_path, "incumbent", uneven))
        assert floor is not None
        assert floor.mde == 0.0, "an unbalanced row leaked between-row spread into the null"
        assert floor.n_replicates == 2, "balancing trims to the smallest qualifying row"

    def test_a_mistyped_path_says_so_rather_than_blaming_repeats(self, tmp_path: Path, caplog) -> None:
        # "no row carries 2+ replicates" would send the reader off to check --repeats when the real
        # cause is a wrong variant, suite or run directory.
        with caplog.at_level(logging.WARNING):
            assert _execution_floor([tmp_path / "typo"]) is None
        assert "wrong variant id, a wrong suite id or a wrong run directory" in caplog.text
        assert "--repeats" not in caplog.text

    def test_records_its_metric(self, tmp_path: Path) -> None:
        floor = _execution_floor(_weighted_arm(tmp_path, "incumbent", self._spread()))
        assert floor is not None
        assert floor.metric == "weighted_score"
        assert floor.criterion_index is None

    def test_defaults_to_the_gate_resample_count(self) -> None:
        import inspect

        assert inspect.signature(measure_execution_noise_floor).parameters["n_resamples"].default == GATE_RESAMPLES


class TestEveryMissingFloorSaysWhy:
    """A silent None on a spend-gating function was the shipped defect; each one now names why."""

    def test_activation_floor_names_too_few_invocations(self, tmp_path: Path, caplog) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {f"r{i}": [("yes", "yes")] for i in range(6)}, invocations=1)
        with caplog.at_level(logging.WARNING):
            assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) is None
        assert "at least 2 invocations" in caplog.text

    def test_activation_floor_names_too_few_scored_rows(self, tmp_path: Path, caplog) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", {"only": [("yes", "yes")]}, invocations=3)
        with caplog.at_level(logging.WARNING):
            assert noise_floor_mde(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0) is None
        assert "in BOTH halves of the invocation split" in caplog.text

    def test_a_mistyped_run_directory_is_not_silent(self, tmp_path: Path, caplog) -> None:
        # The measured defect: a wrong path returned a bare None and printed nothing, on the one
        # function whose job is to stop a user spending.
        with caplog.at_level(logging.WARNING):
            assert (
                noise_floor_mde(
                    run_dirs=[tmp_path / "typo-a", tmp_path / "typo-b"],
                    variant_id="incumbent",
                    suite_id=SUITE,
                    criterion_index=0,
                )
                is None
            )
        assert "No noise floor could be computed" in caplog.text

    def test_the_activation_floor_names_the_path_not_the_criterion_index(self, tmp_path: Path, caplog) -> None:
        # The parity gap with the execution twin: without its own wrong-path guard this reported
        # "only 0 row(s) ... scored a classification result at criterion 0", sending the reader to
        # check the criterion index when the real fault is the variant / suite / run directory.
        with caplog.at_level(logging.WARNING):
            assert (
                measure_noise_floor(
                    run_dirs=[tmp_path / "typo-a", tmp_path / "typo-b"],
                    variant_id="incumbent",
                    suite_id=SUITE,
                    criterion_index=0,
                    model="m",
                )
                is None
            )
        assert "wrong variant id, a wrong suite id or a wrong run directory" in caplog.text
        assert "at criterion" not in caplog.text, "the criterion index is the wrong thing to blame here"

    def test_execution_floor_names_too_few_replicated_rows(self, tmp_path: Path, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert _execution_floor(_weighted_arm(tmp_path, "incumbent", {"r0": [0.1, 0.2]})) is None
        assert "carry 2+ replicates" in caplog.text


class TestActivationPreflight:
    """The two row-selection preflights as a unit, rather than through two bootstraps.

    Extracted from `activation_gate` verbatim — every pre-existing preflight test passes unmodified,
    which is the extraction's own acceptance criterion. These add what testing it through the gate
    could not: the precedence between the two causes, and the notes-list identity contract.
    """

    ROWS: ClassVar[dict[str, list[tuple[str, str]]]] = {f"r{i}": [("yes", "yes" if i else "no")] for i in range(3)}

    def _preflight(self, incumbent: list[Path], candidate: list[Path]) -> tuple[str | None, list[str]]:
        return _activation_preflight(
            incumbent_run_dirs=incumbent,
            candidate_run_dirs=candidate,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
        )

    def _split(self, run_dirs: list[Path], split: str | None) -> None:
        for run_dir in run_dirs:
            payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            payload["row_selection"] = {"split": split}
            (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_a_clean_pair_refuses_nothing_and_notes_nothing(self, tmp_path: Path) -> None:
        dirs = _shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        assert self._preflight(dirs, dirs) == (None, [])

    def test_a_cross_split_pair_refuses(self, tmp_path: Path) -> None:
        inc = _write_arm(tmp_path / "i", "incumbent", self.ROWS, invocations=1)
        cand = _write_arm(tmp_path / "c", "candidate", self.ROWS, invocations=1)
        self._split(inc, "train")
        self._split(cand, "test")
        refusal, _notes = self._preflight(inc, cand)
        assert refusal is not None and "DIFFERENT --split values" in refusal

    def test_a_contaminated_tree_refuses(self, tmp_path: Path) -> None:
        dirs = _shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        _write_row(dirs[0], "candidate", "stale", _eval_result("stale", [("yes", "yes")]), record=False)
        refusal, _notes = self._preflight(dirs, dirs)
        assert refusal is not None and "no recorded invocation wrote" in refusal

    def test_the_cross_split_cause_wins_when_both_hold(self, tmp_path: Path) -> None:
        """PRECEDENCE, and it is program order rather than an accident.

        A pair that is both cross-split and contaminated must report the cross-split cause: it is
        the more specific one, and its remedy ("re-run both arms under one --split") is actionable
        without first understanding the other. Reversing the two checks would silently swap which
        message a user acts on.
        """
        inc = _write_arm(tmp_path / "i", "incumbent", self.ROWS, invocations=1)
        cand = _write_arm(tmp_path / "c", "candidate", self.ROWS, invocations=1)
        self._split(inc, "train")
        self._split(cand, "test")
        _write_row(cand[0], "candidate", "stale", _eval_result("stale", [("yes", "yes")]), record=False)
        refusal, _notes = self._preflight(inc, cand)
        assert refusal is not None
        assert "DIFFERENT --split values" in refusal
        assert "no recorded invocation wrote" not in refusal

    def test_missing_provenance_is_a_note_not_a_refusal(self, tmp_path: Path) -> None:
        dirs = _shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        (dirs[0] / "run.json").unlink()
        refusal, notes = self._preflight(dirs, dirs)
        assert refusal is None, "old run dirs stay gatable"
        assert any("row-selection provenance is missing" in note for note in notes)

    def test_an_unreconcilable_dir_is_a_note_not_a_refusal(self, tmp_path: Path) -> None:
        dirs = _shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        payload = json.loads((dirs[0] / "run.json").read_text(encoding="utf-8"))
        payload.pop("task_results", None)
        (dirs[0] / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        refusal, notes = self._preflight(dirs, dirs)
        assert refusal is None
        assert any("record no `task_results`" in note for note in notes)

    def test_the_unknown_count_survives_inside_a_refusal(self, tmp_path: Path) -> None:
        # Both halves must reach the reader: a refusal whose totals silently exclude a directory
        # that could not be checked is a refusal a user cannot reconcile with the tree.
        dirs = _shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=2)
        _write_row(dirs[0], "candidate", "stale", _eval_result("stale", [("yes", "yes")]), record=False)
        payload = json.loads((dirs[1] / "run.json").read_text(encoding="utf-8"))
        payload.pop("task_results", None)
        (dirs[1] / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        refusal, _notes = self._preflight(dirs, dirs)
        assert refusal is not None
        assert "no recorded invocation wrote" in refusal
        assert "could not be reconciled either way" in refusal

    def test_the_notes_list_the_caller_holds_is_the_one_that_reaches_the_verdict(self, tmp_path: Path) -> None:
        """The identity contract the extraction had to preserve.

        `activation_gate` holds the SAME list object `_load_and_pair` returned, because pydantic
        COPIES it at construction — so a note appended after the model is built is silently
        discarded. Returning a fresh list and `extend`-ing preserves that; re-binding `notes` to a
        concatenation would not, and every later note would land in a list no verdict ever sees.
        """
        dirs = _shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        (dirs[0] / "run.json").unlink()
        verdict = _gate(dirs)
        # The preflight's note AND a note `_load_and_pair` wrote before it are both on the verdict,
        # which can only happen if one list carried both.
        assert any("row-selection provenance is missing" in note for note in verdict.notes)
        # And a note the gate appends AFTER the preflight also survives.
        assert any("minimum detectable effect" in note for note in verdict.notes)


class TestTheTwoDeduplications:
    """`_wrong_path_reason` and `_holm_family` each replace a byte-identical copy.

    A dedup that changes user-facing text is a silent report change, so the messages are asserted
    against pre-change LITERALS rather than against each other — comparing the two call sites to
    one another would pass just as happily if both had moved together.
    """

    def test_the_wrong_path_message_is_byte_identical_to_before(self, tmp_path: Path) -> None:
        expected = (
            f"nothing matched <run>/incumbent/{SUITE}/*/*/task.json under {tmp_path}/a, {tmp_path}/b — "
            "that is a wrong variant id, a wrong suite id or a wrong run directory, not a measurement"
        )
        assert _wrong_path_reason("incumbent", SUITE, [tmp_path / "a", tmp_path / "b"]) == expected

    def test_both_floors_emit_that_message_verbatim(self, tmp_path: Path, caplog) -> None:
        dirs = [tmp_path / "a", tmp_path / "b"]
        expected = _wrong_path_reason("incumbent", SUITE, dirs)
        for call in (
            lambda: measure_noise_floor(
                run_dirs=dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0, model="m"
            ),
            lambda: _execution_floor(dirs),
        ):
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                assert call() is None
            assert expected in caplog.text

    def test_the_empty_run_dirs_divergence_is_folded_in(self) -> None:
        """The ONE real difference the two copies had, and the reason to collapse them.

        The execution twin appended `or "no run dirs were given"` and the activation one did not,
        so an empty sequence read as a message trailing `under ` on one track and named the case on
        the other. Both now name it — the fuller form wins, which loses nothing.
        """
        assert "under no run dirs were given" in _wrong_path_reason("incumbent", SUITE, [])

    def test_holm_family_maps_original_indices_with_an_excluded_verdict_in_the_middle(self) -> None:
        """The off-by-one a naive `enumerate` over the filtered list produces.

        The `None`-p verdict is deliberately in the MIDDLE: with it last, filtered and original
        indices agree and a broken mapping still passes.
        """
        verdicts = [
            _parity_activation(p_value=0.001),
            _parity_activation(p_value=None, mean_diff=None, ci_low=None, ci_high=None),
            _parity_activation(p_value=0.002),
        ]
        family, rejected_at = _holm_family(verdicts, DEFAULT_ALPHA)
        assert [i for i, _p in family] == [0, 2], "membership is by ORIGINAL index"
        assert rejected_at == {0, 2}
        # And end to end: the excluded verdict is index 1, and its neighbours keep their decisions.
        decided = holm_promote(verdicts)
        assert [v.holm_rejected for v in decided] == [True, False, True]
        assert [v.promoted for v in decided] == [True, False, True]

    def test_holm_family_handles_an_empty_family(self) -> None:
        verdicts = [_parity_activation(p_value=None, mean_diff=None, ci_low=None, ci_high=None)]
        family, rejected_at = _holm_family(verdicts, DEFAULT_ALPHA)
        assert family == [] and rejected_at == set()

    @pytest.mark.parametrize(("gate", "build"), _TRACKS)
    def test_the_promoted_vector_is_unchanged_by_the_extraction(self, gate, build) -> None:
        """Both wrappers decide the same family the same way after routing through one helper.

        Holm's step-down over `[0.001, 0.02, 0.9]` at alpha 0.05: 0.001 <= 0.05/3 rejects,
        0.02 <= 0.05/2 rejects, 0.9 > 0.05/1 stops. The thresholds are spelled out because the
        answer is not the one a reader guesses — an uncorrected `p <= alpha` gives the same first
        two and would pass a test that only asserted the last.
        """
        family = [build(p_value=p) for p in (0.001, 0.02, 0.9)]
        decided = gate(family)
        assert [v.holm_rejected for v in decided] == [True, True, False]
        assert [v.promoted for v in decided] == [True, True, False]
        # And the correction genuinely bites: the same p in a family of FOUR is decided against
        # alpha/4 = 0.0125 and no longer rejects.
        assert gate([build(p_value=0.02) for _ in range(4)])[0].holm_rejected is False


class TestTheMdeNoteNamesTheRealCause:
    """`activation_gate`'s MDE note used to name ONE cause unconditionally, and there are five.

    It said "(a null comparison needs at least two invocations of the incumbent)" whatever had
    actually happened — reproduced against shipped code on an incumbent with TWO invocations where
    one row scored in both halves, which rendered that sentence beside `len(run_dirs) == 2`. The
    reason is threaded out through `noise_floor_mde(reasons=...)`, an additive keyword-only sink,
    so the public `float | None` return the skill's snippets import is unchanged.

    Five REACHABLE causes, each witnessed below. `_floor_from_clusters` records a sixth — the
    bootstrap declining on fewer than 2 clusters — which both floors' own `< 2` guards make
    unreachable from them, so it is deliberately not tested through this surface.
    """

    ROWS: ClassVar[dict[str, list[tuple[str, str]]]] = {f"r{i}": [("yes", "yes" if i else "no")] for i in range(4)}

    def _reasons(self, **kwargs) -> list[str]:
        reasons: list[str] = []
        noise_floor_mde(**{"criterion_index": 0, "n_resamples": _FAST_RESAMPLES, "reasons": reasons, **kwargs})
        return reasons

    def test_too_few_invocations(self, tmp_path: Path) -> None:
        dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=1)
        assert "at least 2 invocations" in " ".join(
            self._reasons(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE)
        )

    def test_a_contaminated_tree(self, tmp_path: Path) -> None:
        dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        _write_row(dirs[-1], "incumbent", "stale", _eval_result("stale", [("yes", "yes")]), record=False)
        reasons = self._reasons(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE)
        assert "no recorded invocation wrote" in " ".join(reasons)

    def test_a_wrong_path(self, tmp_path: Path) -> None:
        dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        reasons = self._reasons(run_dirs=dirs, variant_id="typo", suite_id=SUITE)
        assert "wrong variant id" in " ".join(reasons)

    def test_a_cross_split_pair(self, tmp_path: Path) -> None:
        dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        for run_dir, split in zip(dirs, ("train", "test"), strict=True):
            payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            payload["row_selection"] = {"split": split}
            (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        assert "DIFFERENT row selections" in " ".join(
            self._reasons(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE)
        )

    def test_too_few_rows_scored_in_both_halves(self, tmp_path: Path) -> None:
        dirs = _write_arm(tmp_path, "incumbent", {"only": [("yes", "yes")]}, invocations=2)
        assert "in BOTH halves of the invocation split" in " ".join(
            self._reasons(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE)
        )

    def test_the_gate_renders_the_threaded_cause_not_the_hardcoded_one(self, tmp_path: Path) -> None:
        """The reproduction: TWO invocations, so the old sentence was simply false.

        One row scores in both halves, so the floor declines on the row count — and the block used
        to blame the invocation count in front of a reader who could see there were two.
        """
        incumbent = {"only": [("yes", "yes")], "other": [("yes", "no")]}
        candidate = {"only": [("yes", "yes")], "other": [("yes", "yes")]}
        run_dirs = _shared_dirs(tmp_path, incumbent, candidate, invocations=2)
        # Strip one row from the incumbent's second invocation so only one row scores in BOTH.
        shutil.rmtree(run_dirs[1] / "incumbent" / SUITE / "other")
        verdict = _gate(run_dirs)
        assert verdict.mde is None
        note = next(n for n in verdict.notes if "minimum detectable effect could not be computed" in n)
        assert "in BOTH halves of the invocation split" in note
        assert "at least two invocations" not in note, note
        assert len(run_dirs) == 2, "the old sentence was false in front of a reader who could count"

    def test_the_reasons_keyword_is_additive(self, tmp_path: Path) -> None:
        """Every existing caller keeps working untouched — the whole point of a sink."""
        dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        without = noise_floor_mde(run_dirs=dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0)
        collected: list[str] = []
        with_sink = noise_floor_mde(
            run_dirs=dirs, variant_id="incumbent", suite_id=SUITE, criterion_index=0, reasons=collected
        )
        assert without == with_sink
        assert collected == [], "a floor that WAS measured records no reason"


class TestStageAReadersReconcileTheTree:
    """CE053 in behaviour: every run-tree reader either refuses or warns on a contaminated tree.

    Both gates already refused a run dir holding results its own `run.json` never wrote. The four
    Stage A / floor readers did not — measured, on dirs `activation_gate` correctly refuses,
    `measure_noise_floor` returned a floor computed over an extra pooled row and `arm_row_scores`
    returned the stale row in its vector. The floor decides whether a round runs at all; the
    vectors feed all three Pareto fronts.

    The RESPONSE differs by return type and that asymmetry is the phase's whole decision: a
    `NoiseFloor | None` can refuse, an `ArmRowScores` has nowhere to put a refusal.
    """

    ROWS: ClassVar[dict[str, list[tuple[str, str]]]] = {
        "r1": [("yes", "yes")],
        "r2": [("yes", "no")],
        "r3": [("no", "no")],
    }

    def _contaminate(self, run_dirs: list[Path], variant: str = "incumbent") -> None:
        """One row nothing recorded, in the LAST dir — an earlier invocation of a re-used --run-dir."""
        _write_row(run_dirs[-1], variant, "stale", _eval_result("stale", [("yes", "yes")]), record=False)

    def test_the_activation_floor_refuses_and_names_the_directory_and_a_pair(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        self._contaminate(run_dirs)
        with caplog.at_level(logging.WARNING):
            floor = measure_noise_floor(
                run_dirs=run_dirs,
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5",
                n_resamples=_FAST_RESAMPLES,
            )
        assert floor is None
        assert f"{run_dirs[-1]}/incumbent" in caplog.text
        assert "stale/00" in caplog.text, "the (row, replicate) pair is what a reader acts on"

    def test_the_execution_floor_refuses_the_same_way(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dirs = _weighted_arm(tmp_path, "incumbent", {f"r{i}": [0.1 * i, 0.2 * i, 0.3] for i in range(4)})
        _write_row(run_dirs[0], "incumbent", "stale", _scored_result("stale", 1.0), 7, record=False)
        with caplog.at_level(logging.WARNING):
            assert _execution_floor(run_dirs) is None
        assert f"{run_dirs[0]}/incumbent" in caplog.text
        assert "stale/07" in caplog.text

    def test_arm_row_scores_warns_and_still_returns_a_vector(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The warn-not-refuse half: `ArmRowScores` has no field a refusal could live in, so the
        # answer is still produced — with the same message the floors refuse with.
        run_dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=1)
        self._contaminate(run_dirs)
        with caplog.at_level(logging.WARNING):
            arms = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=0)
        assert [a.variant_id for a in arms] == ["incumbent"]
        assert set(arms[0].row_scores) == {"r1", "r2", "r3", "stale"}
        assert "results that no recorded invocation wrote" in caplog.text
        assert f"{run_dirs[-1]}/incumbent" in caplog.text

    def test_cost_quality_points_warns_exactly_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """The regression guard for the double-reconcile the plan rejected (S8).

        `cost_quality_points` reaches the tree through `arm_row_scores`, which reconciles. A
        second reconcile here would read every run.json twice per arm and warn twice about one
        fault — so the suppression is the record, and this test is what keeps it true.
        """
        run_dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=1)
        self._contaminate(run_dirs)
        with caplog.at_level(logging.WARNING):
            cost_quality_points(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=0)
        assert caplog.text.count("results that no recorded invocation wrote") == 1

    def test_one_warning_per_sweep_however_many_arms_share_a_run_dir(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Both arms live under one run dir, as a real experiment writes them. Reconciling inside
        # the per-arm loop would read that dir's run.json once per arm.
        run_dirs = _shared_dirs(tmp_path, self.ROWS, self.ROWS, invocations=1)
        self._contaminate(run_dirs, "candidate")
        with caplog.at_level(logging.WARNING):
            arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent", "candidate"], suite_id=SUITE, criterion_index=0)
        assert caplog.text.count("Row scores may be over a contaminated tree") == 1

    def test_a_clean_tree_is_byte_identical_and_silent(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        run_dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        with caplog.at_level(logging.WARNING):
            floor = measure_noise_floor(
                run_dirs=run_dirs,
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5",
                n_resamples=_FAST_RESAMPLES,
            )
            arms = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=0)
        assert floor is not None and floor.n_rows == 3
        assert set(arms[0].row_scores) == {"r1", "r2", "r3"}
        assert caplog.text == ""

    def test_an_unknown_run_dir_does_not_refuse(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A `run.json` that records no `task_results` is a NOTE, never a refusal.

        The module's settled missing-provenance stance: old run dirs stay measurable, and the one
        state where contamination is undetectable must not also be the one that refuses everything.
        Asserted at WARNING level, because a floor that WAS measured must not log as if it was not.
        """
        run_dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        for run_dir in run_dirs:
            payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            payload.pop("task_results", None)
            (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            floor = measure_noise_floor(
                run_dirs=run_dirs,
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5",
                n_resamples=_FAST_RESAMPLES,
            )
            arms = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, criterion_index=0)
        assert floor is not None
        assert set(arms[0].row_scores) == {"r1", "r2", "r3"}
        assert caplog.text == ""

    def test_a_wrong_path_still_blames_the_path_not_the_tree(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The reconcile runs BEFORE the load, so the ordering has to be checked: a wrong variant
        # leaves nothing on disk to be unrecorded, and the wrong-path message must still win.
        run_dirs = _write_arm(tmp_path, "incumbent", self.ROWS, invocations=2)
        with caplog.at_level(logging.WARNING):
            assert (
                measure_noise_floor(
                    run_dirs=run_dirs,
                    variant_id="typo",
                    suite_id=SUITE,
                    criterion_index=0,
                    model="claude-haiku-4-5",
                )
                is None
            )
        assert "wrong variant id, a wrong suite id or a wrong run directory" in caplog.text
        assert "no recorded invocation wrote" not in caplog.text


class TestInstanceBestFront:
    """GEPA's frontier, beside ours. Neither set contains the other, and that is the point."""

    def _arm(self, name: str, **rows: float) -> ArmRowScores:
        return ArmRowScores(variant_id=name, row_scores=rows)

    def _four_arms(self) -> list[ArmRowScores]:
        # The fixture from the docstring: A is dominated by nobody yet wins nothing; D ties a row's
        # maximum yet is dominated outright by B.
        return [
            self._arm("A", r1=0.5, r2=0.5),
            self._arm("B", r1=1.0, r2=0.4),
            self._arm("C", r1=0.4, r2=1.0),
            self._arm("D", r1=1.0, r2=0.3),
        ]

    def test_the_two_fronts_diverge_in_both_directions(self) -> None:
        arms = self._four_arms()
        assert pareto_front(arms) == ["A", "B", "C"]
        assert instance_best_front(arms) == ["B", "C", "D"]

    def test_keeps_a_single_row_winner(self) -> None:
        # The whole reason a merge reads this set rather than the coverage one.
        arms = [self._arm("broad", r1=0.9, r2=0.9, r3=0.9), self._arm("narrow", r1=0.1, r2=0.1, r3=1.0)]
        assert pareto_front(arms) == ["broad", "narrow"]
        assert instance_best_front(arms) == ["broad", "narrow"]
        # And when the narrow arm IS dominated, coverage drops it while instance-best keeps it.
        arms = [self._arm("broad", r1=0.9, r2=0.9, r3=1.0), self._arm("narrow", r1=0.1, r2=0.1, r3=1.0)]
        assert pareto_front(arms) == ["broad"]
        assert instance_best_front(arms) == ["broad", "narrow"]

    def test_excludes_an_arm_that_scored_nothing(self) -> None:
        real = self._arm("real", r1=0.9)
        crashed = ArmRowScores(variant_id="crashed", row_scores={})
        assert instance_best_front([real, crashed]) == ["real"]

    def test_counts_a_row_only_one_arm_scored(self) -> None:
        # A hole is not a zero, so the max is over the arms that MEASURED the row — an arm alone on
        # a row is trivially best on it. That is the "wins a row nobody else measured" case a merge
        # wants to see.
        alone = self._arm("alone", r1=0.1, r2=0.2)
        other = self._arm("other", r1=0.9)
        assert instance_best_front([alone, other]) == ["alone", "other"]

    def test_keeps_every_tied_arm(self) -> None:
        arms = [self._arm(f"cand-{n}", r1=0.7, r2=0.7) for n in "abc"]
        assert instance_best_front(arms) == ["cand-a", "cand-b", "cand-c"]
        assert pareto_front(arms) == instance_best_front(arms)

    def test_empty_input(self) -> None:
        assert instance_best_front([]) == []

    def test_render_row_matrix_names_the_disagreement(self) -> None:
        arms = self._four_arms()
        text = render_row_matrix(arms, pareto_front(arms), instance_best=instance_best_front(arms))
        assert "Instance-best front (GEPA's, the merge shortlist): B, C, D" in text
        assert "Pareto front (**bold**): A, B, C" in text
        # The diff is the finding — two bare lists teach a reader nothing.
        assert "on coverage without winning any row: A" in text
        assert "wins a row despite being dominated overall: D" in text
        assert "DISCARD" in text and "MERGE" in text

    def test_render_row_matrix_says_so_when_the_fronts_agree(self) -> None:
        arms = [self._arm("a", r1=1.0, r2=0.0), self._arm("b", r1=0.0, r2=1.0)]
        text = render_row_matrix(arms, pareto_front(arms), instance_best=instance_best_front(arms))
        assert "Both fronts agree on these arms." in text

    def test_a_non_finite_score_does_not_poison_the_rows_maximum(self) -> None:
        """A NaN must not drop the arm that genuinely won the row.

        `value > nan` is False, so an unguarded max latches NaN from whichever arm set it first —
        and then `v == best[r]` is False for EVERY arm, so the real winner falls off the merge
        shortlist silently. `pareto_front` degrades the other way (NaN comparisons are False in
        both directions, so nothing dominates and everything stays), which would leave the two
        fronts disagreeing for a reason the render then narrates as a finding.
        """
        nan = float("nan")
        arms = [
            self._arm("winner", r1=1.0, r2=0.5),
            ArmRowScores(variant_id="broken", row_scores={"r1": nan, "r2": 0.1}),
        ]
        assert instance_best_front(arms) == ["winner"]

    def test_render_says_nothing_about_agreement_when_no_arm_scored(self) -> None:
        # Both fronts empty is every arm having crashed. "Both fronts agree" would read as a
        # result, directly above the line calling it a wiring problem.
        arms = [ArmRowScores(variant_id=n, row_scores={}) for n in ("a", "b")]
        text = render_row_matrix(arms, pareto_front(arms), instance_best=instance_best_front(arms))
        assert "Both fronts agree" not in text
        assert "scored no rows at all" in text

    def test_render_row_matrix_without_instance_best_is_unchanged(self) -> None:
        # Byte-identical to today's output when the new keyword is omitted — the sole existing call
        # site in SKILL.md passes two positional arguments and must keep working.
        arms = self._four_arms()
        assert render_row_matrix(arms, pareto_front(arms)) == render_row_matrix(
            arms, pareto_front(arms), instance_best=None
        )
        assert "Instance-best" not in render_row_matrix(arms, pareto_front(arms))


def _cost_quality_arm(tmp_path: Path, variant: str, per_row: dict[str, tuple[float, float | None]]) -> Path:
    """One arm's rows as (weighted_score, cost) pairs. A None cost records no cost at all."""
    run_dir = tmp_path / "run-0"
    for row_id, (score, cost) in per_row.items():
        result = _costed_result(row_id, [("yes", "yes")], cost=cost, duration=10.0)
        _write_row(run_dir, variant, row_id, result.model_copy(update={"weighted_score": score}))
    return run_dir


class TestCostQualityFront:
    """Cost as a second AXIS of the shortlist — never a second gate."""

    def _points(self, tmp_path: Path, arms: dict[str, dict[str, tuple[float, float | None]]]):
        for variant, per_row in arms.items():
            _cost_quality_arm(tmp_path, variant, per_row)
        return cost_quality_points(
            run_dirs=[tmp_path / "run-0"], variant_ids=list(arms), suite_id=SUITE, criterion_index=None
        )

    def test_keeps_a_cheaper_slightly_worse_arm(self, tmp_path: Path) -> None:
        # The headline case: 2% worse and 40% cheaper is a trade worth showing the user.
        points = self._points(
            tmp_path,
            {
                "incumbent": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "cand-cheap": {f"r{i}": (0.88, 0.60) for i in range(6)},
            },
        )
        assert cost_quality_front(points) == ["incumbent", "cand-cheap"]

    def test_drops_a_dearer_and_worse_arm(self, tmp_path: Path) -> None:
        points = self._points(
            tmp_path,
            {
                "incumbent": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "cand-bad": {f"r{i}": (0.70, 1.50) for i in range(6)},
            },
        )
        assert cost_quality_front(points) == ["incumbent"]

    def test_a_free_arm_is_on_the_front_not_excluded(self, tmp_path: Path) -> None:
        # A zero cost is a real coordinate — a free model is legitimately the cheapest arm there
        # is. A truthiness test would exclude it, which is the register_pricing rule restated.
        points = self._points(
            tmp_path,
            {
                "paid": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "free": {f"r{i}": (0.40, 0.0) for i in range(6)},
            },
        )
        assert next(p for p in points if p.variant_id == "free").cost_per_row == 0.0
        assert cost_quality_front(points) == ["paid", "free"]

    def test_an_arm_with_no_recorded_cost_is_excluded_and_named(self, tmp_path: Path) -> None:
        points = self._points(
            tmp_path,
            {
                "measured": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "costless": {f"r{i}": (0.95, None) for i in range(6)},
            },
        )
        assert next(p for p in points if p.variant_id == "costless").cost_per_row is None
        front = cost_quality_front(points)
        assert front == ["measured"]
        text = render_cost_quality(points, front)
        assert "costless" in text
        assert "An unmeasured cost is not a free one" in text

    def test_identical_costs_degenerate_to_the_quality_maxima(self, tmp_path: Path) -> None:
        points = self._points(
            tmp_path,
            {
                "best": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "mid": {f"r{i}": (0.70, 1.00) for i in range(6)},
                "worst": {f"r{i}": (0.50, 1.00) for i in range(6)},
            },
        )
        assert cost_quality_front(points) == ["best"]

    def test_identical_quality_degenerates_to_the_cheapest(self, tmp_path: Path) -> None:
        points = self._points(
            tmp_path,
            {
                "dear": {f"r{i}": (0.80, 2.00) for i in range(6)},
                "cheap": {f"r{i}": (0.80, 0.50) for i in range(6)},
            },
        )
        assert cost_quality_front(points) == ["cheap"]

    def test_a_single_arm_is_its_own_front(self, tmp_path: Path) -> None:
        points = self._points(tmp_path, {"only": {f"r{i}": (0.5, 1.0) for i in range(4)}})
        assert cost_quality_front(points) == ["only"]

    def test_empty_input(self) -> None:
        assert cost_quality_front([]) == []
        assert "No arms" in render_cost_quality([], [])

    def test_a_crashed_arm_cannot_take_the_front_on_the_rows_it_skipped(self, tmp_path: Path) -> None:
        """Both coordinates must be averaged over the SAME rows — the ones the arm actually scored.

        A crashed row produces no criterion results, so `_row_score` returns None and quality
        excludes it — but the row still burned tokens, so an unrestricted cost median includes it.
        Measured before the fix: an arm completing 1 of 6 rows at a perfect score took the whole
        front and knocked the incumbent off it, rendered as two clean numbers with nothing showing
        the other five rows were missing. This is the failure `_dominates`'s coverage rule and
        `render_row_matrix`'s `—` cells exist to prevent one screen earlier.
        """
        run_dir = tmp_path / "run-0"
        for row in range(6):
            good = _costed_result(f"r{row}", [("yes", "yes")], cost=1.0, duration=10.0)
            _write_row(run_dir, "incumbent", f"r{row}", good.model_copy(update={"weighted_score": 0.9}))
            # The candidate finished r0 and crashed the rest: no criterion results, cost still recorded.
            if row == 0:
                _write_row(run_dir, "crashy", f"r{row}", good.model_copy(update={"weighted_score": 1.0}))
            else:
                crashed = _costed_result(f"r{row}", [], cost=1.0, duration=10.0)
                _write_row(run_dir, "crashy", f"r{row}", crashed.model_copy(update={"weighted_score": 0.0}))

        points = cost_quality_points(
            run_dirs=[run_dir], variant_ids=["incumbent", "crashy"], suite_id=SUITE, criterion_index=None
        )
        by_id = {p.variant_id: p for p in points}
        assert (by_id["incumbent"].n_rows, by_id["crashy"].n_rows) == (6, 1)
        # The incumbent must stay on the front: an arm measured on one row is not entitled to a
        # claim about "everywhere", so it cannot displace one measured on six. Both stay — the
        # crashed arm is shown with its row count rather than silently dropped or silently believed.
        assert cost_quality_front(points) == ["incumbent", "crashy"]
        # And the render must SAY the arm is standing on less evidence.
        text = render_cost_quality(points, cost_quality_front(points))
        assert "crashy (1/6)" in text
        assert "may be the missing rows rather than a real trade" in text

    def test_a_non_finite_coordinate_is_excluded_not_undominatable(self) -> None:
        # Every >=/<= against NaN is False, so a NaN arm would be undominatable and render in bold
        # as a live trade. Same guard, same reason, as instance_best_front.
        nan = float("nan")
        points = [
            CostQualityPoint(variant_id="good", score=1.0, cost_per_row=0.1, row_ids=frozenset("abcdef")),
            CostQualityPoint(variant_id="broken", score=0.5, cost_per_row=nan, row_ids=frozenset("abcdef")),
        ]
        assert cost_quality_front(points) == ["good"]

    def test_coverage_is_a_set_test_not_a_count(self) -> None:
        """Two arms on disjoint row sets of equal size must not dominate each other.

        A count-based precondition (`other_rows >= n_rows`) reads as satisfied in BOTH directions
        here, so whichever arm is better on the two aggregate numbers takes the front — while
        neither has a single row of evidence about where the other was measured. `_dominates`
        gates on set coverage for exactly this reason, and the aggregate rule has to agree with it.
        """
        disjoint = [
            CostQualityPoint(variant_id="rows-abc", score=0.9, cost_per_row=1.0, row_ids=frozenset("abc")),
            CostQualityPoint(variant_id="rows-xyz", score=0.5, cost_per_row=2.0, row_ids=frozenset("xyz")),
        ]
        assert cost_quality_front(disjoint) == ["rows-abc", "rows-xyz"]

        # Same numbers, but now the better arm COVERS the other's rows — it may dominate.
        covering = [
            CostQualityPoint(variant_id="rows-abcxyz", score=0.9, cost_per_row=1.0, row_ids=frozenset("abcxyz")),
            CostQualityPoint(variant_id="rows-xyz", score=0.5, cost_per_row=2.0, row_ids=frozenset("xyz")),
        ]
        assert cost_quality_front(covering) == ["rows-abcxyz"]

    def test_the_point_reports_the_rows_it_was_measured_on(self, tmp_path: Path) -> None:
        points = self._points(tmp_path, {"only": {f"r{i}": (0.5, 1.0) for i in range(4)}})
        assert points[0].row_ids == frozenset({"r0", "r1", "r2", "r3"})
        assert points[0].n_rows == 4  # the derived count the render shows

    def test_render_renders_the_advisory_constant(self, tmp_path: Path) -> None:
        # The sensor for the "advisory only, the gate is unchanged" decision. Verbatim, so the
        # claim cannot drift between the render and the two prose surfaces.
        points = self._points(tmp_path, {"only": {f"r{i}": (0.5, 1.0) for i in range(4)}})
        assert COST_FRONT_ADVISORY in render_cost_quality(points, cost_quality_front(points))

    def test_the_control_arm_sits_on_the_front_by_construction(self, tmp_path: Path) -> None:
        # Cheap and bad is undominated, so the emptied-body control is on the front — which is why
        # the standing advisory tells the reader to read it with the arms they are choosing between.
        points = self._points(
            tmp_path,
            {
                "incumbent": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "control": {f"r{i}": (0.05, 0.10) for i in range(6)},
            },
        )
        assert "control" in cost_quality_front(points)
        assert "cheap because it does less" in render_cost_quality(points, cost_quality_front(points))


class TestOneRowCostDefinition:
    def test_cost_quality_points_agree_with_the_guardrail_about_a_row_cost(self, tmp_path: Path) -> None:
        """Both surfaces print the same number, because both route through _row_cost_levels.

        This is the test that stops a second definition of "what a row cost" from appearing — the
        CE037-class defect this repo already added a lint rule for in the F1 direction.
        """
        per_row = {f"r{i}": (0.8, 0.5 + 0.1 * i) for i in range(8)}
        _cost_quality_arm(tmp_path, "incumbent", per_row)
        _cost_quality_arm(tmp_path, "candidate", per_row)
        run_dir = tmp_path / "run-0"

        points = cost_quality_points(
            run_dirs=[run_dir], variant_ids=["incumbent", "candidate"], suite_id=SUITE, criterion_index=None
        )
        check = _cost_check(
            cost_latency_guardrails(
                incumbent_rows=load_arm_rows([run_dir], "incumbent", SUITE),
                candidate_rows=load_arm_rows([run_dir], "candidate", SUITE),
                n_resamples=200,
            )
        )
        incumbent = next(p for p in points if p.variant_id == "incumbent")
        assert incumbent.cost_per_row == pytest.approx(check.incumbent)

    def test_row_cost_levels_is_the_only_row_cost_reduction(self) -> None:
        # Called directly and compared against the guardrail's reported level on a fixture with
        # UNEVEN replicate counts, so the shared reduction is exercised rather than assumed.
        rows = _cost_rows({"r0": [1.0, 3.0], "r1": [2.0], "r2": [4.0, 4.0, 4.0]})
        levels = _row_cost_levels([_row_costs(rows[rid]) for rid in sorted(rows)])
        assert levels == [2.0, 2.0, 4.0]

        check = _cost_check(cost_latency_guardrails(incumbent_rows=rows, candidate_rows=rows, n_resamples=200))
        assert check.incumbent == pytest.approx(_median(levels))

    def test_an_empty_cluster_is_absent_not_zero(self) -> None:
        # `mean([])` is 0.0, so an unfiltered empty cluster would read as "this row cost nothing".
        assert _row_cost_levels([[1.0], [], [3.0]]) == [1.0, 3.0]


# ---------------------------------------------------------------------------
# The execution track's gate
# ---------------------------------------------------------------------------

EXEC_SUITE = SUITE


def _experiment_json(run_dir: Path, variant_ids: list[str], per_replicate: dict[str, dict[str, list[float]]]) -> Path:
    """A minimal two-variant `experiment.json`, written where a real run writes it.

    Built through `ExperimentResult` rather than as a hand-rolled dict, so a field the model
    requires cannot be forgotten here and pass anyway.
    """
    result = ExperimentResult(
        experiment_id="round1-gate",
        description="gate",
        variant_ids=variant_ids,
        task_summaries=[],
        variant_aggregates={},
        total_duration_seconds=1.0,
        per_replicate_scores=per_replicate,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "experiment.json"
    path.write_text(result.model_dump_json(), encoding="utf-8")
    return path


def _exec_run_dir(
    tmp_path: Path,
    *,
    incumbent: dict[str, list[float]],
    candidate: dict[str, list[float]],
    declare_incumbent_first: bool = True,
    extra_scores: dict[str, dict[str, list[float]]] | None = None,
    variant_ids: list[str] | None = None,
) -> Path:
    """One Stage B gate run directory: both arms' rows on disk plus the experiment file.

    The directory name is FIXED, so two calls under one ``tmp_path`` would write into the same tree
    — the second arm's rows landing beside the first's while `experiment.json` is overwritten. That
    is silent and it drifts a fixture without failing anything: measured, a test comparing a refused
    fixture against a clean control ran the control over the refused arm's leftover rows and moved
    its `mde` from 2.8e-17 to 0.030, with the assertion still green. Refusing to build twice is what
    turns that into a test error; the fix at a call site is a distinct `tmp_path / "<name>"`.
    """
    run_dir = tmp_path / "round1-gate"
    assert not run_dir.exists(), (
        f"{run_dir} already exists — two _exec_run_dir calls under one tmp_path merge silently. "
        "Give each fixture its own subdirectory, e.g. _exec_run_dir(tmp_path / 'winner', ...)."
    )
    for variant, per_row in (("incumbent", incumbent), ("candidate", candidate)):
        for row_id, scores in per_row.items():
            for replicate, score in enumerate(scores):
                _write_row(run_dir, variant, row_id, _scored_result(row_id, score), replicate)

    per_replicate = {
        variant: {f"{EXEC_SUITE}/{row_id}": list(scores) for row_id, scores in per_row.items()}
        for variant, per_row in (("incumbent", incumbent), ("candidate", candidate))
    }
    for variant, extra in (extra_scores or {}).items():
        per_replicate.setdefault(variant, {}).update(extra)
    declared = variant_ids or (["incumbent", "candidate"] if declare_incumbent_first else ["candidate", "incumbent"])
    _experiment_json(run_dir, declared, per_replicate)
    return run_dir


def _exec_gate(run_dir: Path, **kwargs) -> ExecutionGateVerdict:
    return execution_gate(
        run_dir=run_dir,
        incumbent_variant="incumbent",
        candidate_variant="candidate",
        suite_id=EXEC_SUITE,
        **{"n_resamples": _FAST_RESAMPLES, **kwargs},
    )


# A candidate that wins on every row, with within-row spread so the paired t has variance.
_WINNER = {
    "incumbent": {"r1": [0.2, 0.3], "r2": [0.4, 0.5], "r3": [0.1, 0.2], "r4": [0.5, 0.6]},
    "candidate": {"r1": [0.7, 0.8], "r2": [0.9, 1.0], "r3": [0.6, 0.8], "r4": [0.9, 0.9]},
}


class TestExecutionGateSign:
    """The single most important assertion in this phase: the tool resolves the subtraction."""

    def test_the_candidate_wins_positively_whichever_arm_is_declared_first(self, tmp_path: Path) -> None:
        first = _exec_gate(_exec_run_dir(tmp_path / "a", **_WINNER, declare_incumbent_first=True))
        second = _exec_gate(_exec_run_dir(tmp_path / "b", **_WINNER, declare_incumbent_first=False))
        assert first.mean_diff is not None and first.mean_diff > 0.0
        assert second.mean_diff == pytest.approx(first.mean_diff)
        assert second.p_value == pytest.approx(first.p_value)

    def test_the_interval_stays_ordered_under_both_declaration_orders(self, tmp_path: Path) -> None:
        # Negating an interval reverses it; without the re-order a promoted candidate reports a
        # "low" above its "high".
        for i, order in enumerate((True, False)):
            verdict = _exec_gate(_exec_run_dir(tmp_path / f"o{i}", **_WINNER, declare_incumbent_first=order))
            assert verdict.ci_low is not None and verdict.ci_high is not None
            assert verdict.ci_low <= verdict.ci_high

    def test_the_effect_size_carries_the_sign_too(self, tmp_path: Path) -> None:
        first = _exec_gate(_exec_run_dir(tmp_path / "a", **_WINNER, declare_incumbent_first=True))
        second = _exec_gate(_exec_run_dir(tmp_path / "b", **_WINNER, declare_incumbent_first=False))
        assert first.effect_size is not None and first.effect_size > 0.0
        assert second.effect_size == pytest.approx(first.effect_size)

    def test_a_losing_candidate_reads_negative(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, incumbent=_WINNER["candidate"], candidate=_WINNER["incumbent"])
        verdict = _exec_gate(run_dir)
        assert verdict.mean_diff is not None and verdict.mean_diff < 0.0


class TestExecutionGateLoading:
    def test_narrows_to_the_target_suite(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(
            tmp_path,
            **_WINNER,
            extra_scores={
                "incumbent": {"other-suite/r1": [0.1], "other-suite/r2": [0.1]},
                "candidate": {"other-suite/r1": [0.9], "other-suite/r2": [0.9]},
            },
        )
        assert _exec_gate(run_dir).rows_paired == 4

    def test_a_missing_experiment_file_is_refused_not_raised(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        (run_dir / "experiment.json").unlink()
        verdict = _exec_gate(run_dir)
        assert (verdict.mean_diff, verdict.ci_low, verdict.p_value) == (None, None, None)
        assert verdict.gate_refusal is not None
        assert "experiment.json" in verdict.gate_refusal and "-e" in verdict.gate_refusal
        assert _headline(render_execution_markdown(holm_promote_execution([verdict])[0])).startswith("NOT A RESULT")

    def test_a_malformed_experiment_file_is_noted_not_raised(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        (run_dir / "experiment.json").write_text("{not json", encoding="utf-8")
        verdict = _exec_gate(run_dir)
        assert verdict.p_value is None
        assert verdict.gate_refusal is not None and "could not be read or parsed" in verdict.gate_refusal

    def test_an_unreadable_experiment_file_is_noted_not_raised(self, tmp_path: Path) -> None:
        # The docstring promises "Never an exception". `except ValueError` did not cover a
        # permission error or a file that vanished between the is_file() and the read.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        real_read_text = Path.read_text

        def _raise_on_the_experiment_file(self: Path, *args, **kwargs) -> str:
            if self.name == "experiment.json":
                raise OSError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        # Patched rather than `chmod 000`, which is a no-op as root and in many CI containers.
        with mock.patch.object(Path, "read_text", _raise_on_the_experiment_file):
            verdict = _exec_gate(run_dir)
        assert verdict.p_value is None and verdict.mean_diff is None
        assert verdict.gate_refusal is not None and "could not be read or parsed" in verdict.gate_refusal

    def test_a_three_variant_experiment_names_the_exactly_two_precondition(self, tmp_path: Path) -> None:
        # The triage file re-passed at Stage B: the mistake reaching the gate.
        run_dir = _exec_run_dir(tmp_path, **_WINNER, variant_ids=["incumbent", "candidate", "cand-b"])
        verdict = _exec_gate(run_dir)
        assert verdict.p_value is None
        assert verdict.gate_refusal is not None
        assert "EXACTLY two" in verdict.gate_refusal and "round<N>-gate.yaml" in verdict.gate_refusal

    def test_a_variant_the_experiment_does_not_carry_names_both_actual_ids(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="typo-arm",
            suite_id=EXEC_SUITE,
            n_resamples=_FAST_RESAMPLES,
        )
        assert verdict.mean_diff is None
        assert verdict.gate_refusal is not None
        assert "'incumbent'" in verdict.gate_refusal and "'candidate'" in verdict.gate_refusal

    def test_an_incumbent_the_experiment_does_not_carry_fails_closed(self, tmp_path: Path) -> None:
        """The return is the ONLY thing acting here, so a regression in it is attributable.

        A mistyped incumbent id also empties that arm, and the zero-row refusal would then carry
        the assertions — the test would pass with this branch reverted. So the fixture keeps the
        incumbent's rows on disk under the id the caller names, and makes only `experiment.json`
        disagree: it declares `inc-A`. That is the one configuration in which this branch decides
        the outcome, and before it returned, the block reported a real, significant
        `inc-A - candidate` difference under a header naming `incumbent`.
        """
        run_dir = _exec_run_dir(
            tmp_path,
            **_WINNER,
            extra_scores={"inc-A": {f"{EXEC_SUITE}/{r}": s for r, s in _WINNER["incumbent"].items()}},
            variant_ids=["inc-A", "candidate"],
        )
        verdict = _exec_gate(run_dir)
        assert (verdict.mean_diff, verdict.ci_low, verdict.ci_high) == (None, None, None)
        assert (verdict.p_value, verdict.effect_size) == (None, None)
        # The message is what attributes it: both arms DID load rows, so the zero-row cause cannot
        # be what set the refusal, and this text belongs to this branch alone.
        assert verdict.gate_refusal is not None
        assert "could not be resolved against the arm you named" in verdict.gate_refusal
        assert "loaded ZERO rows" not in verdict.gate_refusal
        assert holm_promote_execution([verdict])[0].promoted is False

    def test_a_mistyped_incumbent_id_is_refused_rather_than_promoted(self, tmp_path: Path) -> None:
        # The way the fault actually arrives: a typo makes the id unknown to the experiment file
        # AND empties the arm, so both this phase's halves fire. Kept beside the isolating test
        # above rather than instead of it — this is the realistic shape, that one is attributable.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbnet",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            n_resamples=_FAST_RESAMPLES,
        )
        assert (verdict.mean_diff, verdict.ci_low, verdict.ci_high) == (None, None, None)
        assert (verdict.p_value, verdict.effect_size) == (None, None)
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is not True
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT — ")

    def test_fewer_than_two_paired_rows_is_refused_with_the_count_still_carried(self, tmp_path: Path) -> None:
        # No interval can be computed at all, so there is nothing for a reader to weigh — rendering
        # it as NOT PROMOTED says the candidate lost a comparison that never happened. The COUNTS
        # stay on the verdict either way, which is what distinguishes this from a wiring fault: a
        # reader can see `paired 1` rather than having it flattened into the message.
        run_dir = _exec_run_dir(tmp_path, incumbent={"r1": [0.2, 0.3]}, candidate={"r1": [0.8, 0.9]})
        verdict = _exec_gate(run_dir)
        assert verdict.rows_paired == 1
        assert verdict.p_value is None
        assert verdict.gate_refusal is not None
        assert "fewer than the 2 a paired interval needs" in verdict.gate_refusal

    def test_an_unpairable_row_is_carried_as_excluded(self, tmp_path: Path) -> None:
        incumbent = {**_WINNER["incumbent"], "r5": [0.4]}
        run_dir = _exec_run_dir(tmp_path, incumbent=incumbent, candidate=_WINNER["candidate"])
        verdict = _exec_gate(run_dir)
        assert (verdict.rows_paired, verdict.rows_excluded) == (4, 1)


class TestExecutionGateIntegrity:
    def test_engagement_below_one_fails_and_names_the_drop(self, tmp_path: Path) -> None:
        # `_scored_result` writes observed="no" below 0.5, which is a recall.yes miss.
        candidate = {**_WINNER["candidate"], "r3": [0.6, 0.2]}
        run_dir = _exec_run_dir(tmp_path, incumbent=_WINNER["incumbent"], candidate=candidate)
        verdict = _exec_gate(run_dir)
        engagement = next(c for c in verdict.integrity_checks if "engagement" in c.name)
        assert engagement.candidate is not None and engagement.candidate < 1.0
        assert not engagement.passed

    def test_engagement_at_one_on_both_arms_passes(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(
            tmp_path,
            incumbent={f"r{i}": [0.6, 0.7] for i in range(4)},
            candidate={f"r{i}": [0.8, 0.9] for i in range(4)},
        )
        engagement = next(c for c in _exec_gate(run_dir).integrity_checks if "engagement" in c.name)
        assert engagement.passed and engagement.candidate == 1.0

    def test_a_non_classification_index_is_unevaluated_not_a_pass_on_the_merits(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        verdict = _exec_gate(run_dir, engagement_criterion_index=7)
        engagement = next(c for c in verdict.integrity_checks if "engagement" in c.name)
        assert engagement.note is not None and "NOT evaluated" in engagement.note
        assert "criterion_aggregates" in engagement.note
        assert (engagement.incumbent, engagement.candidate) == (None, None)

    def test_none_skips_engagement_and_leaves_only_completion(self, tmp_path: Path) -> None:
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_WINNER), engagement_criterion_index=None)
        assert [c.name for c in verdict.integrity_checks] == ["completion_rate"]

    def test_a_lower_completion_rate_on_the_candidate_fails(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        # An errored replicate: the row directory exists, but nothing scored.
        hollow = _scored_result("r2", 0.9).model_copy(update={"success_criteria_results": []})
        _write_row(run_dir, "candidate", "r2", hollow, 1)
        completion = next(c for c in _exec_gate(run_dir).integrity_checks if c.name == "completion_rate")
        assert not completion.passed
        assert completion.candidate is not None and completion.incumbent is not None
        assert completion.candidate < completion.incumbent

    def test_equal_completion_passes(self, tmp_path: Path) -> None:
        completion = next(
            c for c in _exec_gate(_exec_run_dir(tmp_path, **_WINNER)).integrity_checks if c.name == "completion_rate"
        )
        assert completion.passed and completion.candidate == completion.incumbent == 1.0

    def test_the_gate_reads_no_suite_json(self, tmp_path: Path) -> None:
        # The positional read of `criterion_aggregates` the planning spike falsified: that list is
        # FILTERED, so position i there is not criterion i. Nothing here may depend on it.
        import coder_eval.optimize_gate as gate

        # A PATH JOIN is what a read looks like — `run_dir / ... / "suite.json"`. Both functions
        # also NAME the file in prose (a docstring, and the wrong-index note that tells a user the
        # two index spaces differ), and that must stay legal, so the assertion is on the operator
        # rather than on the string.
        for function in (execution_gate, gate._integrity_checks):
            tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
            joined = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant)
                and isinstance(node.right.value, str)
                and node.right.value.endswith(".json")
                and node.right.value != "experiment.json"
            ]
            assert not joined, f"{function.__name__} joins a path to {[n.right.value for n in joined]}"  # type: ignore[attr-defined]
        assert "SuiteRollup" not in inspect.getsource(gate)


class TestExecutionGateMde:
    def test_a_floor_of_exactly_zero_survives_as_zero(self, tmp_path: Path) -> None:
        # Every replicate identical -> a real 0.000 floor. `measured.mde or None` would erase it.
        run_dir = _exec_run_dir(
            tmp_path,
            incumbent={f"r{i}": [0.4, 0.4] for i in range(4)},
            candidate={f"r{i}": [0.9, 0.9] for i in range(4)},
        )
        assert _exec_gate(run_dir).mde == 0.0

    def test_a_difference_below_the_mde_is_refused(self, tmp_path: Path) -> None:
        """An effect under the suite's own resolution is not a result — it is noise with a p.

        `mde` is the half-width of a bootstrap interval on a NULL difference (the incumbent's own
        replicates split against each other), so it is what this suite's run-to-run noise actually
        is. Promoting a difference below it claims an effect the instrument cannot measure. It used
        to be a note the reader could promote past.

        The per-row SHIFTS differ so the paired differences carry variance: with a constant shift
        the zero-variance refusal fires first and this test would pass without ever reaching the
        branch it is named for. The per-row replicate spreads differ too, or the null half-split
        has no variance and the floor comes back a real 0.000.
        """
        incumbent = {"r0": [0.1, 0.9], "r1": [0.3, 0.5], "r2": [0.0, 0.95], "r3": [0.45, 0.55], "r4": [0.2, 0.8]}
        shifts = {"r0": 0.02, "r1": 0.03, "r2": 0.01, "r3": 0.025, "r4": 0.015}
        candidate = {row: [round(v + shifts[row], 3) for v in values] for row, values in incumbent.items()}
        verdict = _exec_gate(_exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))
        assert verdict.mde is not None and verdict.mean_diff is not None
        assert verdict.effect_size is not None, "fixture drifted — the zero-variance cause must NOT apply"
        assert abs(verdict.mean_diff) < verdict.mde, "fixture drifted — the difference is no longer below the floor"
        assert verdict.gate_refusal is not None
        assert "minimum detectable effect" in verdict.gate_refusal
        assert holm_promote_execution([verdict])[0].promoted is False

    def test_an_unmeasurable_floor_is_said_rather_than_skipped(self, tmp_path: Path) -> None:
        # Both MDE-based checks are inert without a positive floor, and a floor of exactly 0.000 is
        # ordinary: the null split reduces to zero whenever every row carries the same replicate
        # pattern. Rendered as "Minimum detectable effect: 0.000" and nothing else, that reads as
        # "this suite can resolve anything" — the opposite of what it means.
        rows = {f"r{i}": [0.1, 0.5] for i in range(4)}
        candidate = {f"r{i}": [0.6 + 0.01 * i, 0.9 + 0.01 * i] for i in range(4)}
        verdict = _exec_gate(_exec_run_dir(tmp_path, incumbent=rows, candidate=candidate))
        assert verdict.mde == 0.0, "fixture drifted — this test is about an unmeasurable floor"
        assert verdict.gate_refusal is None
        assert any("NOT checked against a noise floor" in note for note in verdict.notes)

    def test_a_measurable_floor_says_nothing_about_being_unmeasurable(self, tmp_path: Path) -> None:
        # The anti-over-fire half: the note must not print on a suite that DID price its floor.
        incumbent = {"r0": [0.1, 0.9], "r1": [0.3, 0.5], "r2": [0.0, 0.95], "r3": [0.45, 0.55], "r4": [0.2, 0.8]}
        candidate = {row: [round(v + 0.02, 3) for v in values] for row, values in incumbent.items()}
        verdict = _exec_gate(_exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))
        assert verdict.mde is not None and verdict.mde > 0.0
        assert not any("NOT checked against a noise floor" in note for note in verdict.notes)

    def test_a_difference_above_a_measurable_mde_is_not_refused(self, tmp_path: Path) -> None:
        # The anti-over-fire half. `_WINNER` cannot witness this: its floor is 2.8e-17, so
        # "difference above the floor" is satisfied by any non-zero win at all and the assertion
        # would pass on a 1e-9 one. This fixture prices a real floor and clears it by a margin.
        # Every shifted CANDIDATE value must land in [0.5, 1.0], and the fixture asserts it rather
        # than trusting the arithmetic. Above 1.0 the score fails EvaluationResult validation,
        # `load_suite_rows` logs and SKIPS that task.json, and the arm trips the completion_rate
        # integrity check — `r2`'s 0.55 shifted to 1.01 and did exactly that, a replicate silently
        # missing here for the fixture's whole life. Below 0.5 `_scored_result` labels the row
        # `no`, so the arm did not engage the skill on it and the engagement check trips — `r2`'s
        # 0.0 shifted to 0.46 and did THAT. Both were invisible while a failed check was advisory;
        # both block the promotion now, which is the point of the change this fixture now backs.
        incumbent = {"r0": [0.1, 0.5], "r1": [0.2, 0.3], "r2": [0.05, 0.5], "r3": [0.25, 0.35], "r4": [0.15, 0.45]}
        candidate = {
            row: [round(v + 0.42 + 0.02 * i, 3) for v in values] for i, (row, values) in enumerate(incumbent.items())
        }
        assert all(0.5 <= v <= 1.0 for values in candidate.values() for v in values), "fixture drifted out of range"
        decided = holm_promote_execution(
            [_exec_gate(_exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))]
        )[0]
        assert decided.mde is not None and decided.mde > 0.05, "fixture drifted — the floor must be REAL"
        assert decided.mean_diff is not None and abs(decided.mean_diff) > 2 * decided.mde
        assert all(check.passed for check in (*decided.integrity_checks, *decided.guardrails))
        assert decided.gate_refusal is None and decided.promoted is True

    def test_a_candidate_that_merely_does_not_help_is_a_negative_result_not_a_refusal(self, tmp_path: Path) -> None:
        """The distinction the refusal must not swallow, and the reason it is two-sided.

        Under a true null the difference is below the floor for nearly every candidate, so refusing
        on that alone would retire NOT PROMOTED almost entirely and tell the reader to buy
        replicates for a candidate whose only problem is that it does not work. An interval that
        CONTAINS zero is the data agreeing it is null — an ordinary negative result.
        """
        incumbent = {"r0": [0.1, 0.9], "r1": [0.3, 0.5], "r2": [0.0, 0.95], "r3": [0.45, 0.55], "r4": [0.2, 0.8]}
        # Differences straddling zero: a candidate that helps on some rows and hurts on others.
        shifts = {"r0": 0.02, "r1": -0.03, "r2": 0.01, "r3": -0.02, "r4": 0.015}
        candidate = {row: [round(v + shifts[row], 3) for v in values] for row, values in incumbent.items()}
        verdict = _exec_gate(_exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))
        assert verdict.mde is not None and verdict.mean_diff is not None and verdict.ci_low is not None
        assert abs(verdict.mean_diff) < verdict.mde, "fixture drifted — it must be BELOW the floor"
        assert verdict.ci_low < 0.0 < (verdict.ci_high or 0.0), "and its interval must contain zero"
        assert verdict.gate_refusal is None, "below the floor AND consistent with zero is not a refusal"
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is False
        assert _headline(render_execution_markdown(decided)) == "NOT PROMOTED"

    def test_an_interval_tighter_than_the_floor_is_a_caveat_not_a_refusal(self, tmp_path: Path) -> None:
        """A large, consistent win reports an absurd p — the PRECISION is wrong, not the decision.

        The paired t's interval comes from the between-row spread of the differences, so two arms
        differing by a similar amount on every row report a half-width far below the suite's
        measured noise. Refusing that would be worse than the defect: a genuine 8-row 0.30 win has
        the same shape. So it is a note, and the promotion stands.
        """
        incumbent = {"r0": [0.1, 0.5], "r1": [0.2, 0.3], "r2": [0.0, 0.55], "r3": [0.25, 0.35], "r4": [0.15, 0.45]}
        shifts = {"r0": 0.40, "r1": 0.405, "r2": 0.395, "r3": 0.40, "r4": 0.405}
        candidate = {row: [round(v + shifts[row], 3) for v in values] for row, values in incumbent.items()}
        verdict = _exec_gate(_exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))
        assert verdict.mde is not None and verdict.ci_low is not None and verdict.ci_high is not None
        half_width = (verdict.ci_high - verdict.ci_low) / 2.0
        assert half_width < verdict.mde, "fixture drifted — the interval is no longer tighter than the floor"
        assert verdict.mean_diff is not None and abs(verdict.mean_diff) > verdict.mde
        assert verdict.gate_refusal is None, "an effect above the floor is a decision, however tight the interval"
        assert any("tighter than this suite's own noise floor" in note for note in verdict.notes)

    def test_a_missing_effect_size_is_explained_by_the_refusal(self, tmp_path: Path) -> None:
        # Two arms agreeing exactly on every row: zero variance, so Cohen's d is undefined while
        # the other statistics are fine. That used to be a note; it is now the refusal, which has
        # to REACH the verdict — pydantic copies the notes list and `gate_refusal` is passed at
        # construction for the same reason.
        rows = {f"r{i}": [0.4, 0.6] for i in range(4)}
        verdict = _exec_gate(_exec_run_dir(tmp_path, incumbent=rows, candidate=dict(rows)))
        assert verdict.mean_diff is not None and verdict.effect_size is None
        assert verdict.gate_refusal is not None and "zero variance" in verdict.gate_refusal
        # Subsumed, not printed beside it: one message per finding.
        assert not any("Cohen's d is undefined" in note for note in verdict.notes)


def _uniform_shift(n_rows: int, *, shift: float = 0.5) -> dict[str, dict[str, list[float]]]:
    """Two flat arms differing by an IDENTICAL amount on every row — zero paired variance.

    The shape the shipped `outcome-rows.jsonl` train split produces: per-row `weighted_score` is a
    weighted mean over a handful of discrete criterion scores, so identical per-row differences are
    ordinary rather than exotic. The paired t then reports p = 0.0000 with a zero-width interval.
    """
    return {
        "incumbent": {f"r{i}": [0.5, 0.5] for i in range(n_rows)},
        "candidate": {f"r{i}": [round(0.5 + shift, 3)] * 2 for i in range(n_rows)},
    }


class TestExecutionGateRefusesAReusedRunDir:
    """The execution track reads the SAME append-only tree, and here contamination flips `promoted`.

    This track has no cross-split refusal — one `run_dir` holds both arms, so they share one split
    by construction — but a re-used `--run-dir` is fully representable, and since Phase 3 folded
    the integrity checks and guardrails into `promoted` a stale replicate does not merely get
    reported: it changes the answer.
    """

    def test_a_stale_replicate_flips_the_verdict_and_is_refused(self, tmp_path: Path) -> None:
        clean = holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path / "clean", **_WINNER))])[0]
        assert clean.promoted is True and clean.gate_refusal is None, "control drifted"

        dirty_dir = _exec_run_dir(tmp_path / "dirty", **_WINNER)
        for row in ("r1", "r2", "r3", "r4"):
            _write_row(dirty_dir, "incumbent", row, _scored_result(row, 0.0), 7, record=False)
        dirty = holm_promote_execution([_exec_gate(dirty_dir)])[0]

        # Same winning candidate; without the preflight this reported promoted=False on a
        # completion_rate the stale replicates invented, with no refusal and no note.
        assert dirty.gate_refusal is not None
        assert "r1/07" in dirty.gate_refusal and "fresh --run-dir" in dirty.gate_refusal
        assert dirty.promoted is False

    def test_a_contaminated_candidate_arm_is_refused_too(self, tmp_path: Path) -> None:
        # The error runs the other way when the CANDIDATE carries the stale rows, so both arms
        # are reconciled rather than just the incumbent.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        _write_row(run_dir, "candidate", "r1", _scored_result("r1", 1.0), 7, record=False)
        verdict = _exec_gate(run_dir)
        assert verdict.gate_refusal is not None and "candidate" in verdict.gate_refusal

    def test_a_clean_gate_run_dir_is_untouched(self, tmp_path: Path) -> None:
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_WINNER))
        assert verdict.gate_refusal is None

    def test_it_renders_as_not_a_result(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        _write_row(run_dir, "incumbent", "r1", _scored_result("r1", 0.0), 7, record=False)
        decided = holm_promote_execution([_exec_gate(run_dir)])[0]
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT")

    def test_refused_already_is_reachable_as_true(self, tmp_path: Path) -> None:
        """The test whose absence let `_execution_diagnostics`'s docstring call two guards dead.

        That docstring said `refused_already` is "False at the only call site today". It is not:
        the tree-reconciliation cause calls `_refuse` and then `break`s rather than returning, so
        control reaches the diagnostics ladder with `gate_refusal` already set. A reader who
        believed the docstring would delete the two `not refused_already` guards as unreachable,
        and this contaminated run would immediately print the MDE and tighter-than-floor advisories
        under a `NOT A RESULT` headline.

        It witnesses ONE of the two advisories — the two branches are mutually exclusive on any
        single fixture (`could not be priced` needs `mde < FLOOR_RESOLUTION`, tighter-than-floor
        needs `mde >= FLOOR_RESOLUTION`), so no fixture can cover both. The other half is covered
        by `TestExecutionDiagnostics::test_refused_already_suppresses_both_advisory_notes`.
        """
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        _write_row(run_dir, "incumbent", "r1", _scored_result("r1", 0.0), 7, record=False)

        seen: list[bool] = []
        real = _execution_diagnostics

        def _spy(**kwargs):
            seen.append(kwargs["refused_already"])
            return real(**kwargs)

        with mock.patch.object(optimize_gate, "_execution_diagnostics", _spy):
            decided = holm_promote_execution([_exec_gate(run_dir)])[0]

        assert seen == [True], "the reconciliation cause must reach the ladder already refused"
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT")
        assert not any(fragment in note for note in decided.notes for fragment in _MDE_ADVISORY_FRAGMENTS), (
            decided.notes
        )


class TestExecutionGateRefusesAZeroVarianceSample:
    """A8: identical per-row differences make every promotion conjunct hold on nothing.

    `paired_t_test` returns 0.0 for a constant non-zero difference and `paired_t_ci` collapses to a
    point, so Holm rejects, the difference favours the candidate and the interval "excludes zero"
    — all three at once, on a sample that separated nothing.
    """

    @pytest.mark.parametrize("n_rows", [2, 4, 8])
    def test_it_refuses_at_any_row_count(self, tmp_path: Path, n_rows: int) -> None:
        # Variance is the defect, not size, so the refusal must not depend on the row count.
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_uniform_shift(n_rows)))
        decided = holm_promote_execution([verdict])[0]
        assert decided.p_value == 0.0, "fixture drifted — the degenerate p is what makes this dangerous"
        assert decided.gate_refusal is not None
        assert decided.promoted is not True

    def test_the_gate_sets_it_before_holm_runs(self, tmp_path: Path) -> None:
        # Pins the setter's location: `execution_gate` already evaluates this predicate, so moving
        # the detection into `holm_promote_execution` would be a second declaration of it.
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_uniform_shift(4)))
        assert verdict.promoted is None
        assert verdict.gate_refusal is not None

    def test_the_message_names_the_row_count_and_the_constant_difference(self, tmp_path: Path) -> None:
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_uniform_shift(4, shift=0.3)))
        assert verdict.gate_refusal is not None
        assert "0.300" in verdict.gate_refusal and "4 paired row" in verdict.gate_refusal

    def test_identical_arms_get_their_own_message_and_remedy(self, tmp_path: Path) -> None:
        """The strongest form of the case, split out the way the activation track splits its own.

        `paired_t_test` returns 1.0 — not 0.0 — for a constant difference of exactly zero, so one
        message covering both shapes would state a p the block four lines below it contradicts. And
        the remedy differs: identical arms are a finding about the CANDIDATE (a wrong `plugins:`
        path gives exactly this shape), so "add rows the arms disagree on" is the wrong advice.
        """
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_uniform_shift(4, shift=0.0)))
        assert verdict.mean_diff == 0.0 and verdict.p_value == 1.0
        assert verdict.gate_refusal is not None
        assert "p = 1.0000" in verdict.gate_refusal
        assert "identical per-row score" in verdict.gate_refusal
        assert "adding rows cannot change it" in verdict.gate_refusal
        # And it must not carry the other shape's claim or its remedy.
        assert "p = 0.0000" not in verdict.gate_refusal
        assert "do NOT agree on" not in verdict.gate_refusal

    def test_a_healthy_sample_is_not_refused(self, tmp_path: Path) -> None:
        # The anti-over-fire test. `_WINNER` has within-row spread, so the paired t has variance.
        decided = holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path, **_WINNER))])[0]
        assert decided.gate_refusal is None
        assert decided.promoted is True

    def test_zero_variance_favouring_the_incumbent_carries_no_negative_result_note(self, tmp_path: Path) -> None:
        # `promoted` was already False here; what changes is the headline — and an unguarded note
        # would print an ordinary negative result directly under a refusal.
        decided = holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path, **_uniform_shift(4, shift=-0.3)))])[0]
        assert decided.mean_diff is not None and decided.mean_diff < 0.0
        assert decided.gate_refusal is not None and decided.promoted is False
        assert not any("favours the incumbent" in note for note in decided.notes)


# A materially worse cost guardrail — the veto every headline-rung fixture below leans on.
_FAILING_GUARDRAIL = GuardrailCheck(
    name="cost (USD/row)",
    incumbent=1.0,
    candidate=3.0,
    relative_change=2.0,
    tolerance=MATERIALITY_FLOOR,
    ci_low=1.5,
    ci_high=2.5,
    passed=False,
)


class TestEveryExecutionHeadlineRungIsReachable:
    """All five rungs, one verdict each, asserted on the headline LINE.

    Folding the guardrail veto into `promoted` retires the BLOCKED rung the moment the renderer
    reads `promoted` for it — silently, because the block still renders and still says something
    plausible ("NOT PROMOTED"). The two rungs it collapses are the two a reader must never
    confuse: "it lost" and "it won and was vetoed" call for opposite next actions. The rungs are
    parametrized from one table so a future reorder cannot make one unreachable without failing
    here, and `test_blocked_and_not_promoted_differ_only_by_separated` pins the collapse itself.
    """

    def _verdict(self, **overrides) -> ExecutionGateVerdict:
        base: dict[str, object] = {
            "incumbent_variant": "incumbent",
            "candidate_variant": "cand",
            "suite_id": EXEC_SUITE,
            "confidence": 0.95,
            "n_resamples": _FAST_RESAMPLES,
            "rows_paired": 8,
            "rows_excluded": 0,
            "mean_diff": 0.2,
            "ci_low": 0.1,
            "ci_high": 0.3,
            "effect_size": 1.1,
            "p_value": 0.001,
        }
        # CE041 scans `src/` only; a test building a verdict from a base dict is the documented
        # legitimate splat.
        return ExecutionGateVerdict(**{**base, **overrides})

    # One row per rung, top of the ladder down. `apply_holm=False` is the UNDECIDED rung: the
    # renderer must not be handed a decided verdict for it, since `promoted is None` IS the rung.
    _RUNGS: ClassVar[list[tuple[str, dict, str]]] = [
        ("undecided", {}, "UNDECIDED"),
        ("not-a-result", {"gate_refusal": "there is no experiment file", "p_value": None}, "NOT A RESULT"),
        ("blocked", {"guardrails": [_FAILING_GUARDRAIL]}, "BLOCKED BY A GUARDRAIL"),
        # `failed` alone cannot tell this rung from the one above it — the primary reason here is
        # that the candidate LOST, and `separated` is the only thing that distinguishes them.
        (
            "lost-and-blocked",
            {"mean_diff": -0.2, "ci_low": -0.3, "ci_high": -0.1, "guardrails": [_FAILING_GUARDRAIL]},
            "NOT PROMOTED",
        ),
        ("promoted", {}, "PROMOTED"),
    ]

    @pytest.mark.parametrize(("rung", "overrides", "expected"), _RUNGS, ids=[r[0] for r in _RUNGS])
    def test_the_rung_is_reachable(self, rung: str, overrides: dict, expected: str) -> None:
        verdict = self._verdict(**overrides)
        # The UNDECIDED rung is the un-decided verdict itself; every other rung is post-Holm.
        decided = verdict if rung == "undecided" else holm_promote_execution([verdict])[0]
        assert _headline(render_execution_markdown(decided)).startswith(expected)

    def test_an_underpowered_candidate_is_not_blocked_however_its_guardrails_read(self) -> None:
        """The trap on the far side of the fold: `separated` alone must not reach the BLOCKED rung.

        `separated` is a property of ONE verdict and deliberately excludes the family decision, so
        at m > 1 a p between alpha/m and alpha leaves `ci_low > 0` while Holm rejects nothing.
        Two candidates identical in every statistic then rendered opposite headlines purely
        because one carried a failing cost check — telling that reader to fix cost when the real
        problem is power, directly above a note saying the p did not clear the Holm threshold.
        """
        # Family of 2 at p = 0.03: the step-down's first threshold is alpha/2 = 0.025, so NEITHER
        # is rejected. The only difference between the arms is the guardrail.
        blocked_arm, clean_arm = holm_promote_execution(
            [self._verdict(p_value=0.03, guardrails=[_FAILING_GUARDRAIL]), self._verdict(p_value=0.03)]
        )
        assert (blocked_arm.separated, clean_arm.separated) == (True, True)
        assert (blocked_arm.holm_rejected, clean_arm.holm_rejected) == (False, False)
        headlines = [_headline(render_execution_markdown(v)) for v in (blocked_arm, clean_arm)]
        assert headlines == ["NOT PROMOTED", "NOT PROMOTED"], "identical statistics must read identically"

    def test_blocked_and_not_promoted_differ_only_by_separated(self) -> None:
        # The pair the fold could collapse: both are `promoted is False`, and only `separated`
        # keeps their headlines apart.
        blocked = holm_promote_execution([self._verdict(guardrails=[_FAILING_GUARDRAIL])])[0]
        lost = holm_promote_execution(
            [self._verdict(mean_diff=-0.2, ci_low=-0.3, ci_high=-0.1, guardrails=[_FAILING_GUARDRAIL])]
        )[0]
        assert (blocked.promoted, lost.promoted) == (False, False)
        assert (blocked.separated, lost.separated) == (True, False)

    def test_a_refusal_outranks_a_failed_guardrail(self) -> None:
        # A zero-variance verdict has p = 0.0 and a zero-width interval, so `separated` holds on
        # it — the refusal must still win the headline.
        decided = holm_promote_execution(
            [self._verdict(gate_refusal="zero variance in the paired differences", guardrails=[_FAILING_GUARDRAIL])]
        )[0]
        assert decided.separated is True and decided.promoted is False
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT")

    def test_a_blocked_candidate_stays_in_the_holm_family(self) -> None:
        # The veto must not change `m` for its siblings: a blocked candidate was still TESTED, and
        # dropping it would LOOSEN alpha/m for everyone else.
        # p values chosen so the family SIZE decides: at m=2 the step-down's first threshold is
        # alpha/2 = 0.025, which 0.03 misses, so neither is rejected; the sibling alone clears
        # alpha/1 = 0.05. Dropping the blocked verdict would therefore PROMOTE the sibling.
        blocked = self._verdict(p_value=0.03, guardrails=[_FAILING_GUARDRAIL])
        sibling = self._verdict(p_value=0.04)
        decided = holm_promote_execution([blocked, sibling])
        assert decided[0].promoted is False
        assert all("family of 2" in " ".join(v.notes) for v in decided)
        # Rank-sensitive, unlike `holm_alpha` (which stores the family-wide input and would read
        # 0.05 either way): dropping the blocked verdict would leave a family of ONE, and p = 0.02
        # clears alpha/1 while it does not clear alpha/2. The sibling's decision is the witness.
        assert decided[1].promoted is False
        assert holm_promote_execution([sibling])[0].promoted is True

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({}, True),
            ({"mean_diff": None}, False),
            ({"ci_low": None}, False),
            ({"mean_diff": -0.2, "ci_low": -0.3, "ci_high": -0.1}, False),
            ({"ci_low": 0.0}, False),  # touching zero is not excluding it
            ({"mean_diff": 0.0}, False),
        ],
        ids=["separated", "no-mean", "no-ci-low", "favours-incumbent", "ci-touches-zero", "zero-diff"],
    )
    def test_separated_is_the_two_component_conjunction(self, overrides: dict, expected: bool) -> None:
        assert self._verdict(**overrides).separated is expected

    @pytest.mark.parametrize(
        ("model", "build"),
        [(ActivationGateVerdict, _parity_activation), (ExecutionGateVerdict, _parity_execution)],
        ids=["activation", "execution"],
    )
    def test_separated_is_not_a_serialized_field(self, model, build) -> None:
        # A property, never a stored field: nothing new is written, so no construction site can set
        # it inconsistently with the numbers it derives from. Asserted on BOTH verdicts — the
        # activation twin was added later, and a symmetry claim needs both halves witnessed.
        assert "separated" not in model.model_fields
        assert "separated" not in build().model_dump()


class TestHolmPromoteExecution:
    def _verdict(self, p: float, **overrides) -> ExecutionGateVerdict:
        base = {
            "incumbent_variant": "incumbent",
            "candidate_variant": "cand",
            "suite_id": EXEC_SUITE,
            "confidence": 0.95,
            "n_resamples": _FAST_RESAMPLES,
            "rows_paired": 8,
            "rows_excluded": 0,
            "mean_diff": 0.2,
            "ci_low": 0.1,
            "ci_high": 0.3,
            "effect_size": 1.1,
            "p_value": p,
        }
        return ExecutionGateVerdict(**{**base, **overrides})

    def test_the_correction_bites_across_a_family(self) -> None:
        # A p that promotes alone must not promote in a family of four identical ones: ties take
        # the strictest rank, so every one is decided against alpha/4.
        alone = holm_promote_execution([self._verdict(0.02)])
        assert alone[0].promoted is True
        family = holm_promote_execution([self._verdict(0.02) for _ in range(4)])
        assert [v.promoted for v in family] == [False] * 4

    def test_a_none_p_value_is_outside_the_family(self) -> None:
        decided = holm_promote_execution([self._verdict(0.001), self._verdict(0.0, p_value=None)])
        assert decided[1].promoted is False
        assert any("outside the family" in note for note in decided[1].notes)
        assert decided[0].promoted is True

    def test_a_difference_favouring_the_incumbent_never_promotes(self) -> None:
        decided = holm_promote_execution([self._verdict(0.001, mean_diff=-0.2, ci_low=-0.3, ci_high=-0.1)])[0]
        assert decided.promoted is False
        assert any("favours the incumbent" in note for note in decided.notes)

    def test_an_interval_containing_zero_never_promotes(self) -> None:
        decided = holm_promote_execution([self._verdict(0.001, ci_low=-0.05)])[0]
        assert decided.promoted is False
        assert any("contains zero" in note for note in decided.notes)

    def test_records_the_alpha_it_applied(self) -> None:
        assert holm_promote_execution([self._verdict(0.01)], alpha=0.10)[0].holm_alpha == 0.10

    def test_empty_list_returns_empty(self) -> None:
        assert holm_promote_execution([]) == []

    def test_a_refused_verdict_with_a_real_p_stays_in_the_family(self) -> None:
        """Membership is `p_value is not None` and nothing else — dropping a refusal LOOSENS Holm.

        Holm corrects for the hypotheses actually tested, and a candidate that was gated and
        measured was tested however degenerate its sample turned out to be. Excluding it shrinks
        `m`, so `alpha/m` gets looser for its siblings — the uncorrected-`p <= alpha` degeneration
        approached from the other side. Measured while this was briefly wrong: two below-MDE
        refusals promoted a p = 0.027 sibling that a family of three rejects.
        """
        real = self._verdict(0.03)
        refused = self._verdict(0.06, gate_refusal="the observed difference is below the MDE")
        assert holm_promote_execution([real])[0].promoted is True, "it promotes in a family of one"
        decided = holm_promote_execution([real, refused, self._verdict(0.04)])
        assert any("family of 3" in note for note in decided[0].notes), "the refusal is counted"
        assert decided[0].promoted is False, "the multiplicity that was actually incurred applies"
        assert decided[1].promoted is False, "and the refusal itself never promotes"

    def test_a_refusal_with_no_p_value_is_outside_the_family(self) -> None:
        # The other half: a cause meaning "there was no comparison at all" has no p, so it is
        # outside the family by the ordinary rule, without a second one keyed on the refusal.
        real = self._verdict(0.03)
        no_comparison = self._verdict(0.0, p_value=None, gate_refusal="there is no experiment file")
        decided = holm_promote_execution([real, no_comparison])
        assert any("family of 1" in note for note in decided[0].notes)
        assert decided[0].promoted is True and decided[1].promoted is False

    def test_a_refused_verdict_without_a_p_value_gets_no_negative_result_note(self) -> None:
        # Reachable, not theoretical: the zero-row refusal is set before `experiment.json` is even
        # opened, so "refused AND no p" is what a mistyped incumbent id produces.
        decided = holm_promote_execution([self._verdict(0.0, p_value=None, gate_refusal="loaded ZERO rows")])[0]
        assert decided.promoted is False
        assert not any("outside the family" in note for note in decided.notes)

    def test_a_failed_guardrail_forces_promoted_false_and_is_noted(self) -> None:
        # The veto now lives in the DECISION, not only in the render. What keeps the BLOCKED
        # headline reachable is `separated` — the statistical half, unaffected by the guardrail.
        failing = GuardrailCheck(
            name="cost (USD/row)",
            incumbent=1.0,
            candidate=3.0,
            relative_change=2.0,
            tolerance=MATERIALITY_FLOOR,
            ci_low=1.5,
            ci_high=2.5,
            passed=False,
        )
        decided = holm_promote_execution([self._verdict(0.001, guardrails=[failing])])[0]
        assert decided.promoted is False
        assert decided.separated is True
        assert any("cost (USD/row) FAILED" in note for note in decided.notes)
        # On the HEADLINE: the note above quotes the headline's own words, so a whole-page
        # substring test passes whichever rung the block actually took.
        assert _headline(render_execution_markdown(decided)).startswith("BLOCKED BY A GUARDRAIL")


class TestRenderExecutionMarkdown:
    def _decided(self, tmp_path: Path, **kwargs) -> ExecutionGateVerdict:
        return holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path, **_WINNER), **kwargs)])[0]

    def test_says_undecided_before_holm_has_run(self, tmp_path: Path) -> None:
        text = render_execution_markdown(_exec_gate(_exec_run_dir(tmp_path, **_WINNER)))
        assert "UNDECIDED" in text
        assert "NOT PROMOTED" not in text

    def test_prints_the_interval_the_mde_and_every_check(self, tmp_path: Path) -> None:
        text = render_execution_markdown(self._decided(tmp_path))
        assert "candidate - incumbent, sign resolved by the tool" in text
        assert "Minimum detectable effect" in text
        assert "Integrity checks" in text and "completion_rate" in text
        assert "Guardrails" in text

    def test_a_failing_integrity_check_blocks_the_headline(self, tmp_path: Path) -> None:
        candidate = {**_WINNER["candidate"], "r3": [0.6, 0.2]}
        run_dir = _exec_run_dir(tmp_path, incumbent=_WINNER["incumbent"], candidate=candidate)
        decided = holm_promote_execution([_exec_gate(run_dir, n_resamples=_FAST_RESAMPLES)])[0]
        # Unconditional on BOTH halves, so neither assertion can become a silent no-op: the check
        # vetoes the promotion, and `separated` records that the statistic itself came out — which
        # is what makes the BLOCKED headline reachable rather than an ordinary NOT PROMOTED.
        assert decided.promoted is False
        assert decided.separated is True
        text = render_execution_markdown(decided)
        # The headline, not the page — the failed-check note quotes this phrase too.
        assert _headline(text).startswith("BLOCKED BY A GUARDRAIL")
        assert "engagement" in text

    def test_renders_a_missing_effect_size_as_a_dash(self, tmp_path: Path) -> None:
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_WINNER)).model_copy(update={"effect_size": None})
        assert "Cohen's d: —" in render_execution_markdown(verdict)

    def test_a_refused_verdict_leads_with_not_a_result(self, tmp_path: Path) -> None:
        # SEPARATE tmp dirs: `_exec_run_dir` always writes `<tmp>/round1-gate` and never clears it,
        # so building both fixtures under one `tmp_path` leaves the refused arm's rows on disk for
        # the control — measured, it moved the control's `mde` from 2.8e-17 to 0.030.
        decided = holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path / "refused", **_uniform_shift(4)))])[0]
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT — ")
        # And the assertion is not a no-op: a clean fixture WITH spread headlines PROMOTED, so the
        # headline above is discriminating rather than whatever this renderer happens to print.
        assert _headline(render_execution_markdown(self._decided(tmp_path / "winner"))) == "PROMOTED"

    def test_a_refusal_outranks_a_failing_guardrail(self, tmp_path: Path) -> None:
        # Reading a guardrail presupposes a statistic that separated, so the refusal is above it —
        # matching `render_markdown`'s precedence. Guaranteed only indirectly today (a refusal
        # forces `promoted=False`, which makes the BLOCKED rung unreachable), which is exactly why
        # it is pinned: a change that stopped forcing it would reorder the ladder silently.
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_uniform_shift(4)))
        failing = GuardrailCheck(
            name="cost (USD/row)", incumbent=1.0, candidate=3.0, relative_change=2.0, tolerance=0.25, passed=False
        )
        decided = holm_promote_execution([verdict.model_copy(update={"guardrails": [failing]})])[0]
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT — ")

    def test_undecided_still_outranks_the_refusal(self, tmp_path: Path) -> None:
        # A verdict Holm never saw has no decision to refuse, so `promoted is None` wins the ladder.
        verdict = _exec_gate(_exec_run_dir(tmp_path, **_uniform_shift(4)))
        assert verdict.gate_refusal is not None
        assert _headline(render_execution_markdown(verdict)).startswith("UNDECIDED")

    def test_the_refusal_text_survives_the_undecided_headline(self, tmp_path: Path) -> None:
        """The message must reach the reader on EVERY render path, not only when it wins.

        The refusal replaced notes that `render_execution_markdown` printed unconditionally. Moving
        it to a headline-only channel meant a pre-Holm block over a mis-wired arm rendered a
        confident interval and four green checks with nothing anywhere saying the rows are missing
        — measured, and the exact silent-zero this module's docstring promises never happens.
        """
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        shutil.rmtree(run_dir / "incumbent")
        verdict = _exec_gate(run_dir)
        assert verdict.promoted is None and verdict.gate_refusal is not None
        block = render_execution_markdown(verdict)
        assert _headline(block).startswith("UNDECIDED"), "the ladder is unchanged — this is about the TEXT"
        assert verdict.gate_refusal in block
        # And it appears exactly once: when the headline DOES carry it, the extra line must not.
        decided = holm_promote_execution([verdict])[0]
        assert render_execution_markdown(decided).count(decided.gate_refusal or "") == 1

    def test_a_non_finite_score_cannot_reach_the_paired_statistic(self, tmp_path: Path) -> None:
        """`paired_t_ci` declines on a non-finite score — which would be an all-`None` statistic
        over a real `task_count`, i.e. every number `—` with no note saying why.

        It cannot arrive through `execution_gate`, and this pins the reason rather than guarding
        the same thing twice: pydantic's JSON validator REJECTS `NaN`, so the file never parses and
        the read's own note is what the reader gets. If that ever changes, this test is the thing
        that says the unreachability claim in `execution_gate` is no longer true.
        """
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        raw = ExperimentResult.model_validate_json((run_dir / "experiment.json").read_text(encoding="utf-8"))
        scores = {v: dict(per) for v, per in raw.per_replicate_scores.items()}
        scores["candidate"][f"{EXEC_SUITE}/r1"] = [float("nan"), 0.8]
        (run_dir / "experiment.json").write_text(
            raw.model_copy(update={"per_replicate_scores": scores}).model_dump_json(), encoding="utf-8"
        )
        verdict = _exec_gate(run_dir)
        assert (verdict.mean_diff, verdict.p_value) == (None, None)
        assert verdict.gate_refusal is not None and "could not be read or parsed" in verdict.gate_refusal
        assert holm_promote_execution([verdict])[0].promoted is False


# ---------------------------------------------------------------------------
# Behaviour-preservation pins for the literal-keyword construction rewrite
# ---------------------------------------------------------------------------

_VERDICT_PINS = Path(__file__).parent / "_fixtures" / "optimize_verdicts"


def _pinned_suite() -> tuple[dict, dict]:
    """`_tiny_suite` plus a SIBLING criterion the candidate annexes one row of.

    Load-bearing for the pins rather than decoration. `_tiny_suite`'s rows carry one criterion, so
    `sibling_checks` comes back `[]` — which is the model default, so a construction that dropped
    the `sibling_checks=` keyword entirely would reproduce the pin byte for byte. Measured: with a
    single-criterion suite, deleting `sibling_checks=` from both return paths left the whole file
    green. It is the field `holm_promote` folds into `promoted`, so it is the last one a
    behaviour pin may be blind to.
    """
    incumbent, candidate = _tiny_suite(4, 4)
    return (
        {rid: [*labels, ("yes", "yes")] for rid, labels in incumbent.items()},
        {rid: [*labels, ("yes", "no" if rid == "p0" else "yes")] for rid, labels in candidate.items()},
    )


def _assert_matches_pin(verdict, name: str) -> None:
    """Compare a verdict's dump against output captured BEFORE the construction rewrite.

    That rewrite replaced `verdict_kwargs` / `_verdict(**overrides)` with literal keywords on
    every construction path of both models. It is behaviour-preserving BY CONSTRUCTION — which is
    exactly the kind of claim that needs a witness, because a dropped or mis-spelled keyword shows
    up as a field quietly at its default rather than as an error.

    Compared as PARSED JSON so the pinned floats are the ones the run actually produced, and so a
    field ADDED to either model fails here rather than being silently absent from the expectation.
    Committed beside the other characterization fixtures (`report_snapshots`, `golden_streams`).

    They have since outlived that one job and become ordinary characterization snapshots: an
    INTENTIONAL change to what a verdict says will fail here too, and the fixture is then updated
    with the diff reviewed. That is the point — every field of a promotion verdict is something a
    reader acts on, so none of them should be able to move without someone looking at it.
    """
    expected = json.loads((_VERDICT_PINS / f"{name}.json").read_text(encoding="utf-8"))
    assert json.loads(verdict.model_dump_json()) == expected


class TestConstructionIsBehaviourPreserving:
    """The three construction paths the rewrite touches, pinned against pre-rewrite output."""

    def test_the_activation_verdict_is_unchanged(self, tmp_path: Path) -> None:
        _assert_matches_pin(_gate(_shared_dirs(tmp_path, *_pinned_suite())), "activation_gate")

    def test_the_activation_early_return_is_unchanged(self, tmp_path: Path) -> None:
        # The `bootstrap is None` path, which used to OVERWRITE two keys in the shared dict before
        # splatting it — the path the rewrite changes most. The incumbent scores three rows across
        # three invocations while only one PAIRS, so the bootstrap declines (1 scored row) while
        # the incumbent's own null split still yields a real `mde`: an early-return pin whose
        # `mde` was `None` could not see a dropped `mde=` keyword.
        incumbent = {f"p{i}": [("yes", "no"), ("yes", "yes")] for i in range(3)}
        candidate = {"p0": [("yes", "yes"), ("yes", "no")]}
        _assert_matches_pin(_gate(_shared_dirs(tmp_path, incumbent, candidate)), "activation_gate_early_return")

    def test_the_execution_verdict_is_unchanged(self, tmp_path: Path) -> None:
        _assert_matches_pin(_exec_gate(_exec_run_dir(tmp_path, **_WINNER)), "execution_gate")

    def test_the_refused_execution_verdict_is_unchanged(self, tmp_path: Path) -> None:
        # `gate_refusal` is the one value `_verdict` reads from its closure rather than taking as a
        # parameter, and it is `None` on every healthy verdict — so a pin that never refuses cannot
        # see it dropped.
        _assert_matches_pin(_exec_gate(_exec_run_dir(tmp_path, **_uniform_shift(4))), "execution_gate_refused")


class TestLoadAndPair:
    """The load/pair/exclude step, called directly rather than only through the gate.

    Six concerns used to be interleaved in `activation_gate`'s first hundred lines. Testing them
    through the gate meant paying two bootstraps to assert a note, so most of them were asserted
    only incidentally.
    """

    def _pair(self, run_dirs: list[Path], **kwargs):
        return _load_and_pair(
            **{
                "incumbent_run_dirs": run_dirs,
                "candidate_run_dirs": run_dirs,
                "incumbent_variant": "incumbent",
                "candidate_variant": "candidate",
                "suite_id": SUITE,
                "criterion_index": 0,
                **kwargs,
            }
        )

    def test_a_clean_pair_carries_every_row_and_no_notes(self, tmp_path: Path) -> None:
        rows = {f"r{i}": [("yes", "yes" if i else "no")] for i in range(4)}
        paired = self._pair(_shared_dirs(tmp_path, rows, rows))
        assert paired.scored_row_ids == ["r0", "r1", "r2", "r3"]
        assert paired.rows_excluded == 0
        assert paired.notes == []
        # Four rows x three invocations, flattened from the same clusters.
        assert len(paired.incumbent_clusters) == 4
        assert len(paired.incumbent_pairs) == 12
        assert paired.incumbent_pairs == paired.candidate_pairs
        assert paired.n_discordant == 0

    def test_a_zero_row_incumbent_says_which_arm_and_what_did_not_match(self, tmp_path: Path) -> None:
        run_dirs = _write_arm(tmp_path, "candidate", {"r1": [("yes", "yes")]})
        paired = self._pair(run_dirs)
        assert paired.scored_row_ids == []
        assert any("the incumbent arm loaded ZERO rows" in n for n in paired.notes)
        assert not any("the candidate arm loaded ZERO rows" in n for n in paired.notes)

    def test_an_unpaired_row_on_each_side_is_excluded_and_counted(self, tmp_path: Path) -> None:
        incumbent = {"shared": [("yes", "yes")], "only-inc": [("yes", "yes")]}
        candidate = {"shared": [("yes", "yes")], "only-cand": [("yes", "yes")]}
        paired = self._pair(_shared_dirs(tmp_path, incumbent, candidate))
        assert paired.scored_row_ids == ["shared"]
        assert paired.rows_excluded == 2
        assert any("only-cand, only-inc" in n for n in paired.notes)

    def test_a_hollow_row_is_dropped_from_both_vectors(self, tmp_path: Path) -> None:
        # The row directory exists on both arms, so it PAIRS — but only one arm scored it.
        run_dirs = _shared_dirs(tmp_path, {"r1": [("yes", "yes")]}, {"r1": [("yes", "yes")]})
        for run_dir in run_dirs:
            _write_row(run_dir, "candidate", "hollow", _eval_result("hollow", []))
            _write_row(run_dir, "incumbent", "hollow", _eval_result("hollow", [("yes", "yes")]))
        paired = self._pair(run_dirs)
        assert paired.scored_row_ids == ["r1"]
        assert paired.rows_excluded == 1
        assert any("scored on only one arm" in n and "hollow" in n for n in paired.notes)

    def test_unbalanced_replicates_are_trimmed_and_the_drop_is_counted(self, tmp_path: Path) -> None:
        run_dirs = _shared_dirs(tmp_path, {"r1": [("yes", "yes")]}, {"r1": [("yes", "yes")]})
        # A fourth candidate replicate for r1 only, which would otherwise weigh 4:3.
        _write_row(run_dirs[0], "candidate", "r1", _eval_result("r1", [("yes", "yes")]), replicate=1)
        paired = self._pair(run_dirs)
        assert len(paired.incumbent_pairs) == len(paired.candidate_pairs) == 3
        assert any("dropping 1 observation(s)" in n for n in paired.notes)

    def test_a_criterion_index_past_the_end_is_named_as_a_wiring_mistake(self, tmp_path: Path) -> None:
        rows = {"r1": [("yes", "yes")]}
        paired = self._pair(_shared_dirs(tmp_path, rows, rows), criterion_index=7)
        assert paired.incumbent_pairs == [] and paired.candidate_pairs == []
        assert any("criterion_index=7 selected NO classification results" in n for n in paired.notes)
        assert any("the index is past the end" in n for n in paired.notes)

    def test_the_returned_notes_list_is_the_one_the_gate_keeps_appending_to(self, tmp_path: Path) -> None:
        # Pydantic COPIES the list at construction, so a note appended after the model is built is
        # silently discarded. The gate must hold THIS list, not a copy of it.
        rows = {"r1": [("yes", "yes")]}
        paired = self._pair(_shared_dirs(tmp_path, rows, rows))
        before = len(paired.notes)
        paired.notes.append("added by the caller")
        assert len(paired.notes) == before + 1


def test_the_gate_notes_keep_their_order(tmp_path: Path) -> None:
    """Note ORDER is observable — `render_markdown` prints them in order and the pins compare bytes.

    A fixture that trips three of them at once, asserted as an ordered list of prefixes rather than
    a set, so the extraction cannot quietly reorder the ladder.

    THREE, not all four: the zero-row note fires only when an arm loaded nothing, and an arm that
    loaded nothing pairs no rows — so it cannot co-occur with the hollow and unbalanced notes,
    which need paired rows to exist. `TestLoadAndPair` covers that one on its own fixture.
    """
    incumbent = {"shared": [("yes", "yes")], "only-inc": [("yes", "yes")]}
    candidate = {"shared": [("yes", "yes")]}
    run_dirs = _shared_dirs(tmp_path, incumbent, candidate)
    for run_dir in run_dirs:
        _write_row(run_dir, "candidate", "hollow", _eval_result("hollow", []))
        _write_row(run_dir, "incumbent", "hollow", _eval_result("hollow", [("yes", "yes")]))
    _write_row(run_dirs[0], "candidate", "shared", _eval_result("shared", [("yes", "yes")]), replicate=1)

    verdict = _gate(run_dirs)
    prefixes = [
        "1 row(s) present in only one arm",
        "1 row(s) scored on only one arm",
        "1 row(s) had different replicate counts",
    ]
    matched = [
        next(p for p in prefixes if n.startswith(p)) for n in verdict.notes if any(n.startswith(p) for p in prefixes)
    ]
    assert matched == prefixes, f"note order changed: {matched}"


class TestRefusalMessage:
    """`_refusal_message` called directly, one test per branch of a ~60-line message builder."""

    def _verdict(
        self,
        *,
        p_floor: float | None,
        n_discordant: int | None = 3,
        rows_paired: int = 6,
        n_resamples: int = GATE_RESAMPLES,
    ) -> ActivationGateVerdict:
        # Literal keywords, not a splat: `extra="forbid"` plus CE041's static half is the two-sided
        # rule this repo settled on, and a helper that splatted would be the shape it forbids.
        return ActivationGateVerdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=SUITE,
            criterion_index=0,
            confidence=0.95,
            n_resamples=n_resamples,
            rows_paired=rows_paired,
            rows_excluded=0,
            incumbent_f1=0.0,
            candidate_f1=1.0,
            mean_diff=1.0,
            ci_low=0.5,
            ci_high=1.0,
            p_value=0.01,
            p_floor=p_floor,
            n_discordant=n_discordant,
        )

    def test_none_when_the_floor_is_below_the_threshold(self) -> None:
        assert _refusal_message(self._verdict(p_floor=0.001), threshold=0.025, family_size=2, alpha=0.05) is None

    def test_none_when_there_is_no_floor_at_all(self) -> None:
        # `None`, never `""` — `gate_refusal` is `str | None` and the render branches on `is not None`.
        assert _refusal_message(self._verdict(p_floor=None), threshold=0.025, family_size=2, alpha=0.05) is None

    def test_a_floor_of_one_is_the_zero_discordant_case_with_its_own_message(self) -> None:
        message = _refusal_message(self._verdict(p_floor=1.0), threshold=0.025, family_size=2, alpha=0.05)
        assert message is not None
        assert "identical labels on every one of the 6" in message
        assert "adding more rows LIKE THESE cannot change it" in message
        assert "survivor(s)" not in message, "the family lever is meaningless when nothing differs"

    def test_a_finite_floor_names_both_levers_and_the_required_discordant_count(self) -> None:
        message = _refusal_message(
            self._verdict(p_floor=0.03125, n_discordant=3, n_resamples=2000), threshold=0.025, family_size=2, alpha=0.05
        )
        assert message is not None
        assert "Gate at most 1 survivor(s)" in message
        assert "DISAGREE on from 3 to 4" in message
        assert "adding rows they agree on makes this floor worse" in message

    def test_a_verdict_with_no_discordant_count_gets_the_family_lever_alone(self) -> None:
        # Never a sentence about a count the verdict does not carry.
        message = _refusal_message(
            self._verdict(p_floor=0.03125, n_discordant=None), threshold=0.025, family_size=2, alpha=0.05
        )
        assert message is not None
        assert "Gate at most 1 survivor(s) at alpha=0.05." in message
        assert "DISAGREE on" not in message

    def test_an_unreachable_bar_says_rows_are_irrelevant_rather_than_insufficient(self) -> None:
        # `min_discordant_rows` returns None when even every row discordant leaves the estimator's
        # own floor above the bar — a draw-count fact, so "more rows" would be actively wrong.
        message = _refusal_message(
            self._verdict(p_floor=0.5, n_discordant=1, rows_paired=4, n_resamples=10),
            threshold=0.0001,
            family_size=5,
            alpha=0.05,
        )
        assert message is not None
        assert "no discordant count clears this bar" in message
        assert "a larger n_resamples or a smaller family — not rows" in message

    def test_a_non_finite_floor_takes_the_no_refusal_path_rather_than_raising(self) -> None:
        """The one place the early return is not a plain De Morgan of the guard it replaced.

        Every comparison against NaN is False, so a NaN floor refused nothing before; under a
        `p_floor <= threshold` spelling it falls THROUGH to `math.floor(alpha / nan)` and raises
        out of the skill's inline snippet.

        Built with `model_construct`, which is the honest framing: pydantic's validator rejects a
        non-finite float, so this cannot arrive through a validated verdict and the guard is
        defence in depth rather than a live path. It is kept because it is the ORIGINAL spelling's
        semantics — preserving them costs one `not` — and because `_refusal_message` is a
        standalone function now, reachable by a caller that builds a verdict without validating it.
        """
        verdict = ActivationGateVerdict.model_construct(rows_paired=6, p_floor=float("nan"), n_discordant=3)
        assert _refusal_message(verdict, threshold=0.025, family_size=2, alpha=0.05) is None

    def test_no_workable_family_size_says_so(self) -> None:
        message = _refusal_message(
            self._verdict(p_floor=0.9, n_discordant=1), threshold=0.025, family_size=2, alpha=0.05
        )
        assert message is not None
        assert "No family size works at alpha=0.05, not even a family of one" in message


class TestFrontSummary:
    """`None` and `[]` are different, and that distinction is the legacy two-argument call shape."""

    def test_none_emits_the_pareto_line_only(self) -> None:
        assert _front_summary(["a"], None) == ["Pareto front (**bold**): a"]

    def test_an_empty_instance_best_still_emits_its_line(self) -> None:
        lines = _front_summary(["a"], [])
        assert any(line.endswith("merge shortlist): none") for line in lines)

    def test_two_empty_fronts_emit_no_agreement_sentence(self) -> None:
        # With both fronts empty every arm crashed, and "both fronts agree" would read as a result
        # immediately above the line saying it is a wiring problem.
        lines = _front_summary([], [])
        assert not any("agree" in line for line in lines)
        assert sum("none" in line for line in lines) == 2

    def test_identical_non_empty_fronts_agree(self) -> None:
        assert "Both fronts agree on these arms." in _front_summary(["a", "b"], ["a", "b"])

    def test_disagreeing_fronts_name_each_side(self) -> None:
        text = "\n".join(_front_summary(["a"], ["b"]))
        assert "on coverage without winning any row: a" in text
        assert "wins a row despite being dominated overall: b" in text
        assert "Coverage is the set to DISCARD from" in text


class TestExecutionDiagnostics:
    """`_execution_diagnostics` called directly, one test per finding plus the ordering rule."""

    _ROWS: ClassVar[dict[str, list[EvaluationResult]]] = {"r1": [], "r2": []}

    def _comparison(self, **kwargs) -> PairedComparison:
        return PairedComparison(
            **{
                "vid_a": "candidate",
                "vid_b": "incumbent",
                "task_count": 4,
                "excluded_count": 0,
                "mean_diff": 0.5,
                "ci_low": 0.3,
                "ci_high": 0.7,
                "effect_size": 2.0,
                "p_value": 0.001,
                **kwargs,
            }
        )

    def _run(self, tmp_path: Path, **kwargs) -> tuple[str | None, list[str]]:
        rows = {"r1": [_scored_result("r1", 1.0)]}
        return _execution_diagnostics(
            **{
                "incumbent_rows": rows,
                "candidate_rows": rows,
                "incumbent_variant": "incumbent",
                "candidate_variant": "candidate",
                "suite_id": SUITE,
                "run_dir": tmp_path,
                "comparison": self._comparison(),
                "mean_diff": 0.5,
                "effect_size": 2.0,
                "mde": 0.1,
                "bounds": [0.3, 0.7],
                "refused_already": False,
                **kwargs,
            }
        )

    def test_a_healthy_sample_refuses_nothing(self, tmp_path: Path) -> None:
        refusal, notes = self._run(tmp_path)
        assert refusal is None and notes == []

    def test_an_empty_incumbent_arm_refuses(self, tmp_path: Path) -> None:
        refusal, _notes = self._run(tmp_path, incumbent_rows={})
        assert refusal is not None and "the incumbent arm ('incumbent')" in refusal
        assert "the candidate arm" not in refusal

    def test_both_arms_empty_produce_one_refusal_naming_both(self, tmp_path: Path) -> None:
        refusal, _notes = self._run(tmp_path, incumbent_rows={}, candidate_rows={})
        assert refusal is not None
        assert "the incumbent arm ('incumbent') and the candidate arm ('candidate')" in refusal

    def test_fewer_than_two_paired_rows_refuses(self, tmp_path: Path) -> None:
        refusal, _notes = self._run(tmp_path, comparison=self._comparison(task_count=1))
        assert refusal is not None and "fewer than the 2 a paired interval needs" in refusal

    def test_zero_variance_at_a_zero_difference_is_its_own_message(self, tmp_path: Path) -> None:
        refusal, _notes = self._run(tmp_path, mean_diff=0.0, effect_size=None, bounds=[0.0, 0.0], mde=None)
        assert refusal is not None
        assert "identical per-row score" in refusal and "p = 1.0000" in refusal

    def test_zero_variance_at_a_constant_non_zero_difference_is_the_other(self, tmp_path: Path) -> None:
        refusal, _notes = self._run(tmp_path, effect_size=None, bounds=[0.5, 0.5], mde=None)
        assert refusal is not None
        assert "differed by exactly 0.500 on every one" in refusal
        assert "p = 0.0000" in refusal

    def test_below_the_mde_with_an_interval_excluding_zero_refuses(self, tmp_path: Path) -> None:
        refusal, _notes = self._run(tmp_path, mean_diff=0.05, mde=0.2, bounds=[0.01, 0.09])
        assert refusal is not None
        assert "confident claim about an effect this suite cannot see" in refusal

    def test_below_the_mde_with_an_interval_containing_zero_is_an_ordinary_negative(self, tmp_path: Path) -> None:
        # 37 of 40 true-null candidates land here. Refusing them would retire NOT PROMOTED.
        refusal, notes = self._run(tmp_path, mean_diff=0.05, mde=0.2, bounds=[-0.1, 0.2])
        assert refusal is None
        assert any("ordinary negative result and not a measurement problem" in n for n in notes)

    def test_an_unavailable_floor_is_noted_not_skipped(self, tmp_path: Path) -> None:
        _refusal, notes = self._run(tmp_path, mde=None)
        assert any("came back unavailable" in n for n in notes)

    def test_a_floor_at_the_resolution_limit_is_noted_as_unpriced(self, tmp_path: Path) -> None:
        _refusal, notes = self._run(tmp_path, mde=0.0)
        assert any("came back 0.000" in n and "never as 'this suite can resolve anything'" in n for n in notes)

    def test_an_interval_tighter_than_the_floor_is_a_caveat_not_a_refusal(self, tmp_path: Path) -> None:
        # The t reads BETWEEN-row spread, which the MDE never sees. Refusing would throw away
        # genuine large consistent wins.
        refusal, notes = self._run(tmp_path, mean_diff=0.5, mde=0.3, bounds=[0.49, 0.51])
        assert refusal is None
        assert any("tighter than this suite's own noise floor" in n for n in notes)

    def test_the_first_of_two_causes_wins(self, tmp_path: Path) -> None:
        # Both the zero-row and the zero-variance causes apply. If the rows never loaded, whether
        # their differences vary is moot — so the wiring message is the one that survives.
        refusal, _notes = self._run(
            tmp_path, incumbent_rows={}, mean_diff=0.0, effect_size=None, bounds=[0.0, 0.0], mde=None
        )
        assert refusal is not None
        assert "loaded ZERO rows" in refusal
        assert "zero variance" not in refusal

    def test_refused_already_suppresses_both_advisory_notes(self, tmp_path: Path) -> None:
        # A note explaining a number printed under a refusal headline contradicts it.
        _refusal, unavailable = self._run(tmp_path, mde=None, refused_already=True)
        assert unavailable == []
        _refusal, tighter = self._run(tmp_path, mean_diff=0.5, mde=0.3, bounds=[0.49, 0.51], refused_already=True)
        assert tighter == []

    def test_a_cause_found_here_also_suppresses_them(self, tmp_path: Path) -> None:
        # `refused_already` is the CALLER's flag; a zero-variance verdict refused three lines up
        # would otherwise print a floor note under its own refusal headline.
        refusal, notes = self._run(tmp_path, mean_diff=0.0, effect_size=None, bounds=[0.0, 0.0], mde=None)
        assert refusal is not None
        assert not any("came back unavailable" in n for n in notes)

    def test_it_neither_refuses_nor_builds_a_verdict_itself(self) -> None:
        """Two setters for one field is the state `_refuse` collapsed.

        A helper that reached back into the gate's closure could not be tested without building a
        gate around it, so this pins that it does neither.

        Scanned over the CODE with the docstring removed, not over the raw source. The naive form
        punished the one thing it should reward: explaining, in this function's own docstring, what
        `gate_refusal` is and why `refused_already` is reachable. A sensor that fires on
        documentation of the invariant it guards is one an author routes around.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(_execution_diagnostics)))
        function = tree.body[0]
        assert isinstance(function, ast.FunctionDef)
        body = function.body[1:] if ast.get_docstring(function) is not None else function.body
        code = "\n".join(ast.unparse(node) for node in body)
        assert "_verdict(" not in code
        assert "gate_refusal" not in code
        # Anti-vacuity: the scan must still SEE the body it is checking.
        assert "_refuse(" in code


_RENDER_PINS = Path(__file__).parent / "_fixtures" / "optimize_renders"


def _assert_matches_render_pin(block: str, name: str, *, tmp_path: Path | None = None) -> None:
    """Compare a rendered markdown block against output captured BEFORE the module split.

    The verdict pins next door compare a `model_dump`; nothing compared the STRING the skill
    actually prints. `render_row_matrix` is about to be split into section helpers and every
    renderer is about to move modules, and both are the kind of change that reorders a line
    without changing a number — which no substring assertion in this file can see.

    Pass ``tmp_path`` for a block that NAMES run directories: the cross-split refusal quotes the
    paths it read, which are per-test temporaries. Normalising them to a placeholder is what lets
    that block be pinned whole rather than sampled by substring — the pin still covers every other
    character, including the headline and the order of the lines around it.
    """
    if tmp_path is not None:
        block = block.replace(str(tmp_path), "<TMP>")
    expected = (_RENDER_PINS / f"{name}.md").read_text(encoding="utf-8")
    assert block == expected


def _matrix_arms() -> list[ArmRowScores]:
    """Five arms chosen so every section of `render_row_matrix` renders something.

    `cand-broad` is on the coverage front while winning no row; `cand-dominated` wins r1 while
    being dominated outright — so the two fronts disagree in BOTH directions and the disagreement
    paragraph names each side. `r0` is scored 0.0 by every arm that measured it (the all-zero
    footnote) and absent from the rest (the hole footnote), and `cand-crashed` scored nothing at
    all (the unscored footnote).
    """
    return [
        ArmRowScores(variant_id="cand-broad", row_scores={"r1": 0.5, "r2": 0.5}),
        ArmRowScores(variant_id="cand-r1", row_scores={"r0": 0.0, "r1": 1.0, "r2": 0.4}),
        ArmRowScores(variant_id="cand-r2", row_scores={"r0": 0.0, "r1": 0.4, "r2": 1.0}),
        ArmRowScores(variant_id="cand-dominated", row_scores={"r1": 1.0, "r2": 0.3}),
        ArmRowScores(variant_id="cand-crashed", row_scores={}),
    ]


def _cost_quality_pin_points(tmp_path: Path) -> list[CostQualityPoint]:
    """Four arms: two fully measured, one thin (2 rows of 4) and one with no cost at all."""
    arms: dict[str, dict[str, tuple[float, float | None]]] = {
        "incumbent": {f"r{i}": (0.90, 1.00) for i in range(4)},
        "cand-cheap": {f"r{i}": (0.88, 0.60) for i in range(4)},
        "cand-thin": {f"r{i}": (0.95, 0.50) for i in range(2)},
        "cand-costless": {f"r{i}": (0.95, None) for i in range(4)},
    }
    for variant, per_row in arms.items():
        _cost_quality_arm(tmp_path, variant, per_row)
    return cost_quality_points(
        run_dirs=[tmp_path / "run-0"], variant_ids=list(arms), suite_id=SUITE, criterion_index=None
    )


class TestRenderingIsBehaviourPreserving:
    """The six rendered blocks, pinned whole against output captured before the module split.

    `TestRenderMarkdown` and its siblings assert substrings, so a reordered or dropped line stays
    green. Every renderer is about to move modules and `render_row_matrix` is about to be split
    into section helpers; these are the witnesses that neither changed a byte.
    """

    def test_the_activation_block_is_unchanged(self, tmp_path: Path) -> None:
        verdict = holm_promote([_gate(_shared_dirs(tmp_path, *_pinned_suite()))])[0]
        _assert_matches_render_pin(render_markdown(verdict), "activation_gate")

    def test_the_refused_activation_block_is_unchanged(self, tmp_path: Path) -> None:
        # The discreteness refusal, which is the one refused verdict carrying no filesystem path.
        incumbent, candidate = _tiny_suite(3, 3)
        run_dirs = _shared_dirs(tmp_path, incumbent, candidate)
        verdicts = [_gate(run_dirs, n_resamples=TestGateRefusal._REFUSAL_RESAMPLES) for _ in range(2)]
        _assert_matches_render_pin(render_markdown(holm_promote(verdicts)[0]), "activation_gate_refused")

    def test_the_execution_block_is_unchanged(self, tmp_path: Path) -> None:
        verdict = holm_promote_execution([_exec_gate(_exec_run_dir(tmp_path, **_WINNER))])[0]
        _assert_matches_render_pin(render_execution_markdown(verdict), "execution_gate")

    def test_the_cross_split_refusal_block_is_unchanged(self, tmp_path: Path) -> None:
        """The fifth headline, pinned whole like its siblings rather than sampled by substring.

        The other refused pin next door is the DISCRETENESS refusal, which carries a p and keeps
        `CANNOT SEPARATE AT THIS SIZE`. This is the other refusal on the same track — no p, no
        comparison made — and the two must not converge on one block.
        """
        inc, cand = TestCrossSplitRefusal._arms(tmp_path, "train", "test")
        verdict = holm_promote([TestCrossSplitRefusal._gate(inc, cand)])[0]
        _assert_matches_render_pin(render_markdown(verdict), "activation_gate_cross_split", tmp_path=tmp_path)

    def test_the_row_matrix_is_unchanged(self) -> None:
        arms = _matrix_arms()
        block = render_row_matrix(arms, pareto_front(arms), instance_best=instance_best_front(arms))
        _assert_matches_render_pin(block, "row_matrix")

    def test_the_cost_quality_table_is_unchanged(self, tmp_path: Path) -> None:
        points = _cost_quality_pin_points(tmp_path)
        _assert_matches_render_pin(render_cost_quality(points, cost_quality_front(points)), "cost_quality")

    def test_the_search_comparison_is_unchanged(self) -> None:
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        # The head vector comes from `TestSearchCompare` rather than being respelled here: this pin
        # exists to witness the block that class's corpus-regression case renders, and an inlined
        # copy would silently stop mirroring it the day that vector is edited.
        comparison = search_compare(
            _arm("head", TestSearchCompare._HEAD),
            _arm("cand", {"r1": 0.0, "r2": 1.0, "r3": 1.0, "r4": 1.0}),
            corpus=corpus,
        )
        _assert_matches_render_pin(render_search_comparison(comparison), "search_comparison")


def _full_guardrail_check() -> GuardrailCheck:
    """A `GuardrailCheck` with every field set away from its default."""
    return GuardrailCheck(
        name="cost (USD/row)",
        incumbent=1.0,
        candidate=2.0,
        relative_change=1.0,
        tolerance=0.25,
        ci_low=0.5,
        ci_high=1.5,
        rate=0.25,
        passed=False,
        note="a note",
    )


def _full_activation_verdict() -> ActivationGateVerdict:
    return ActivationGateVerdict(
        incumbent_variant="incumbent",
        candidate_variant="cand",
        suite_id=SUITE,
        criterion_index=1,
        confidence=0.9,
        n_resamples=99,
        rows_paired=8,
        rows_excluded=2,
        incumbent_f1=0.4,
        candidate_f1=0.8,
        mean_diff=0.4,
        ci_low=0.1,
        ci_high=0.7,
        p_value=0.01,
        p_floor=0.005,
        n_discordant=4,
        gate_refusal="refused",
        holm_alpha=0.05,
        holm_rejected=True,  # rejected at its rank, yet not promoted — the refusal is what vetoed
        # False, not True: a refusal forces `promoted=False`, and a carrier fixture that pairs a
        # refusal with a promotion is a state no gate can emit.
        promoted=False,
        range_non_overlap=True,
        mde=0.2,
        sibling_checks=[_full_guardrail_check()],
        guardrails=[_full_guardrail_check()],
        notes=["a note"],
    )


def _full_execution_verdict() -> ExecutionGateVerdict:
    return ExecutionGateVerdict(
        incumbent_variant="incumbent",
        candidate_variant="cand",
        suite_id=SUITE,
        confidence=0.9,
        n_resamples=99,
        rows_paired=8,
        rows_excluded=2,
        mean_diff=0.4,
        ci_low=0.1,
        ci_high=0.7,
        effect_size=1.2,
        p_value=0.01,
        gate_refusal="refused",
        holm_alpha=0.05,
        holm_rejected=True,  # rejected at its rank, yet not promoted — the refusal is what vetoed
        promoted=False,  # as above: a refusal forces this False
        mde=0.2,
        integrity_checks=[_full_guardrail_check()],
        guardrails=[_full_guardrail_check()],
        notes=["a note"],
    )


class TestTypedConstruction:
    """A mistyped or renamed field must RAISE, not silently become None.

    Both verdicts used to be built through string-keyed dicts splatted into the constructor, and
    neither model nor `GuardrailCheck` declared `extra="forbid"` — so `mean_dif=0.42` produced a
    verdict reporting no difference at all, with every other number intact and nothing to see.
    """

    @pytest.mark.parametrize(
        ("build", "typo"),
        [
            (_full_activation_verdict, "mean_dif"),
            (_full_execution_verdict, "mean_dif"),
            (_full_guardrail_check, "relative_chnge"),
        ],
        ids=["activation", "execution", "guardrail"],
    )
    def test_an_unknown_field_raises(self, build, typo: str) -> None:
        instance = build()
        model = type(instance)
        with pytest.raises(ValidationError):
            model(**{**instance.model_dump(), typo: 0.42})

    @pytest.mark.parametrize(
        "build",
        [_full_activation_verdict, _full_execution_verdict, _full_guardrail_check],
        ids=["activation", "execution", "guardrail"],
    )
    def test_a_fully_populated_instance_round_trips(self, build) -> None:
        instance = build()
        model = type(instance)
        # Over `model_fields`, never a hand-picked subset: a field the construction rewrite dropped
        # is invisible to a comparison that never looks at it.
        dumped = instance.model_dump()
        assert model.model_validate(dumped) == instance
        # And through the NARROWER dump too. `exclude_unset=True` drops every field left at its
        # default, so it is the shape that would silently lose a newly-added stored field whose
        # writer forgot to set it — `holm_rejected` is exactly that shape on both verdicts.
        assert model.model_validate(instance.model_dump(exclude_unset=True)) == instance
        # And every OPTIONAL field is genuinely exercised. Compared against the real resolved
        # default — `field.get_default(call_default_factory=True)`, which covers `default=None` and
        # `default_factory=list` alike. Reading `field.default` instead excludes exactly those two,
        # which is almost every optional field on these models, and the guard then checks nothing.
        still_default = [
            name
            for name, field in model.model_fields.items()
            if not field.is_required() and dumped[name] == field.get_default(call_default_factory=True)
        ]
        assert not still_default, f"fixture leaves {still_default} at the default — it proves nothing there"


class TestDeriveSiblingIndices:
    """The guardrail stops being opt-in: `None` derives, `()` is now how you opt OUT."""

    def test_skips_a_non_classification_criterion_between_two_classification_ones(self, tmp_path: Path) -> None:
        # Positions are ABSOLUTE. A "count the classification criteria" implementation returns [1]
        # here, which is the file_check — the exact case this test exists for.
        result = _eval_result("r1", [("yes", "yes")])
        basic = CriterionResult(criterion_type="file_check", description="f", score=1.0)
        sibling = ClassificationCriterionResult(
            criterion_type="skill_triggered",
            description="sibling",
            score=1.0,
            expected_label="yes",
            observed_label="yes",
        )
        results = [*result.success_criteria_results, basic, sibling]
        stacked = result.model_copy(update={"success_criteria_results": results})
        rows = {"r1": [stacked]}
        assert derive_sibling_indices(rows, primary_index=0) == [2]
        assert derive_sibling_indices(rows, primary_index=2) == [0]

    def test_a_single_criterion_suite_derives_nothing(self) -> None:
        rows = {"r1": [_eval_result("r1", [("yes", "yes")])]}
        assert derive_sibling_indices(rows, primary_index=0) == []

    def test_unions_both_arms_rather_than_letting_one_shadow_the_other(self) -> None:
        # `{**incumbent, **candidate}` would drop the incumbent's list for every shared row id —
        # which is every row in the common case — and derive from the candidate alone.
        incumbent = {"r1": [_eval_result("r1", [("yes", "yes"), ("no", "no")])]}
        candidate = {"r1": [_eval_result("r1", [("yes", "yes")])]}
        assert derive_sibling_indices(incumbent, candidate, primary_index=0) == [1]
        assert derive_sibling_indices(candidate, incumbent, primary_index=0) == [1]

    def test_a_row_with_no_results_contributes_nothing_rather_than_truncating(self) -> None:
        errored = _eval_result("r2", []).model_copy(update={"success_criteria_results": []})
        rows = {"r1": [_eval_result("r1", [("yes", "yes"), ("no", "no")])], "r2": [errored]}
        assert derive_sibling_indices(rows, primary_index=0) == [1]

    def test_a_primary_past_the_end_does_not_raise(self) -> None:
        rows = {"r1": [_eval_result("r1", [("yes", "yes"), ("no", "no")])]}
        assert derive_sibling_indices(rows, primary_index=9) == [0, 1]


class TestSiblingIndicesDefault:
    """`None` derives, `()` checks nothing, an explicit sequence checks exactly those."""

    @staticmethod
    def _stacked(tmp_path: Path) -> list[Path]:
        # Two classification criteria per row: the primary at 0, a sibling at 1 the candidate
        # annexes on half of the sibling's true rows.
        incumbent = {f"r{i}": [("yes", "yes"), ("yes", "yes")] for i in range(4)}
        candidate = {f"r{i}": [("yes", "yes"), ("yes", "no" if i < 2 else "yes")] for i in range(4)}
        return _shared_dirs(tmp_path, incumbent, candidate)

    def test_the_default_derives_the_same_list_as_passing_it_explicitly(self, tmp_path: Path) -> None:
        run_dirs = self._stacked(tmp_path)
        derived = _gate(run_dirs)
        explicit = _gate(run_dirs, sibling_indices=[1])
        assert [c.name for c in derived.sibling_checks] == [c.name for c in explicit.sibling_checks]
        assert derived.sibling_checks and "criterion 1" in derived.sibling_checks[0].name

    def test_an_empty_tuple_still_checks_nothing(self, tmp_path: Path) -> None:
        assert _gate(self._stacked(tmp_path), sibling_indices=()).sibling_checks == []

    def test_a_single_criterion_suite_is_silent(self, tmp_path: Path) -> None:
        incumbent, candidate = _tiny_suite(4, 4)
        assert _gate(_shared_dirs(tmp_path, incumbent, candidate)).sibling_checks == []


class TestAnnexationRate:
    def test_reports_the_fraction_the_candidate_alone_lost(self, tmp_path: Path) -> None:
        run_dirs = TestSiblingIndicesDefault._stacked(tmp_path)
        check = _gate(run_dirs).sibling_checks[0]
        # 4 true-yes sibling rows; the candidate turned 2 of them to "no" and the incumbent none.
        assert check.rate == pytest.approx(0.5)
        assert not check.passed  # the RECALL drop is what fails it, not the rate

    def test_an_equal_arm_reports_zero_and_passes(self, tmp_path: Path) -> None:
        rows = {f"r{i}": [("yes", "yes"), ("yes", "yes")] for i in range(4)}
        check = _gate(_shared_dirs(tmp_path, rows, dict(rows))).sibling_checks[0]
        assert check.rate == 0.0
        assert check.passed

    def test_a_sibling_with_no_true_instances_reports_none(self, tmp_path: Path) -> None:
        rows = {f"r{i}": [("yes", "yes"), ("no", "no")] for i in range(4)}
        check = _gate(_shared_dirs(tmp_path, rows, dict(rows))).sibling_checks[0]
        assert check.rate is None
        assert check.passed
        assert check.note is not None and "nothing to regress" in check.note

    def test_the_rate_changes_no_pass_fail_outcome(self, tmp_path: Path) -> None:
        # A non-zero annexation the INCUMBENT more than offsets: recall does not drop, so the
        # check passes while the rate is non-zero. The rate is a reading, never a second gate.
        incumbent = {f"r{i}": [("yes", "yes"), ("yes", "yes" if i == 0 else "no")] for i in range(4)}
        candidate = {f"r{i}": [("yes", "yes"), ("yes", "no" if i == 0 else "yes")] for i in range(4)}
        check = _gate(_shared_dirs(tmp_path, incumbent, candidate)).sibling_checks[0]
        assert check.rate == pytest.approx(0.25)
        assert check.passed

    def test_render_prints_the_rate_only_when_there_is_one(self, tmp_path: Path) -> None:
        with_rate = _gate(TestSiblingIndicesDefault._stacked(tmp_path)).sibling_checks[0]
        assert f"rate {with_rate.rate:.3f}" in "\n".join(_render_checks("Sibling checks", [with_rate]))

        no_rate = GuardrailCheck(
            name="cost (USD/row)", incumbent=1.0, candidate=1.0, relative_change=0.0, tolerance=0.25, passed=True
        )
        assert "rate" not in "\n".join(_render_checks("Guardrails", [no_rate]))


class TestExecutionGateCannotBeQuietlyMisread:
    """Every way this gate could report a confident verdict about nothing."""

    def test_a_mistyped_suite_id_is_refused_and_the_message_names_the_suite(self, tmp_path: Path) -> None:
        # The statistic comes from experiment.json and every CHECK comes from the row tree, so a
        # mistyped variant/suite/run-dir leaves a perfectly good p beside four `— -> —` passes.
        # Measured before the refusal existed: headline PROMOTED, every check green.
        #
        # A wrong SUITE id also empties both arms, but the more specific cause fires first: no row
        # of that suite scored on both arms, and naming the suite is what the reader has to fix.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id="a-suite-that-was-never-run",
            n_resamples=_FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        # Pin WHICH cause: the zero-row message interpolates the suite id too, so asserting the id
        # alone would pass under either and the precedence claim above would go unwitnessed.
        assert "no paired comparison" in verdict.gate_refusal
        assert "a-suite-that-was-never-run" in verdict.gate_refusal
        assert "loaded ZERO rows" not in verdict.gate_refusal
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is not True
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT — ")

    def test_both_arms_empty_produce_exactly_one_refusal_naming_both(self, tmp_path: Path) -> None:
        # Every id correct and the experiment file valid — only the row tree is gone. That is the
        # case no other cause can see, and it is where the zero-row message earns its place. ONE
        # refusal naming both arms: the loop this replaced appended the same finding twice.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        shutil.rmtree(run_dir / "incumbent")
        shutil.rmtree(run_dir / "candidate")
        verdict = _exec_gate(run_dir)
        assert verdict.gate_refusal is not None and "loaded ZERO rows" in verdict.gate_refusal
        assert "the incumbent arm ('incumbent')" in verdict.gate_refusal
        assert "the candidate arm ('candidate')" in verdict.gate_refusal
        assert not any("loaded ZERO rows" in note for note in verdict.notes), "one message, not one per arm"
        assert _headline(render_execution_markdown(holm_promote_execution([verdict])[0])).startswith("NOT A RESULT")

    def test_one_empty_arm_is_refused_where_the_variant_check_does_not_fire(self, tmp_path: Path) -> None:
        # A VALID incumbent id whose rows are simply not on disk (right id, wrong run dir). The
        # variant-mismatch return cannot see this — the experiment file names the arm perfectly
        # well — so the zero-row refusal is the only thing standing between it and PROMOTED.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        shutil.rmtree(run_dir / "incumbent")
        verdict = _exec_gate(run_dir)
        assert verdict.mean_diff is not None, "the statistic still computes — that is the whole hazard"
        assert verdict.gate_refusal is not None
        assert "the incumbent arm" in verdict.gate_refusal
        assert "the candidate arm" not in verdict.gate_refusal, "only the empty arm may be named"
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is False
        assert _headline(render_execution_markdown(decided)).startswith("NOT A RESULT — ")

    def test_a_wiring_refusal_outranks_a_zero_variance_one(self, tmp_path: Path) -> None:
        # Both causes at once. If the rows never loaded, whether their differences vary is moot —
        # so the wiring message is what renders, and its remedy is the one the reader needs.
        run_dir = _exec_run_dir(tmp_path, **_uniform_shift(4))
        shutil.rmtree(run_dir / "incumbent")
        verdict = _exec_gate(run_dir)
        # BOTH halves of the variance predicate (`mean_diff is not None and effect_size is None`):
        # asserting only the second would let a fixture that produced no comparison at all pass
        # this test without the second cause ever applying.
        assert verdict.mean_diff is not None and verdict.effect_size is None, (
            "fixture drifted — the zero-variance cause must also apply for precedence to mean anything"
        )
        assert verdict.gate_refusal is not None
        assert "loaded ZERO rows" in verdict.gate_refusal
        assert "zero variance" not in verdict.gate_refusal

    def test_the_same_variant_on_both_arms_reports_nothing(self, tmp_path: Path) -> None:
        # Sign resolution keys on the candidate, so a duplicated id used to yield `vid_a - vid_b`
        # labelled `candidate - incumbent` with both labels identical — a significant, sign-flipped
        # verdict about an arm compared to itself.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="incumbent",
            suite_id=EXEC_SUITE,
            n_resamples=_FAST_RESAMPLES,
        )
        assert (verdict.mean_diff, verdict.p_value, verdict.ci_low) == (None, None, None)
        assert verdict.gate_refusal is not None and "both 'incumbent'" in verdict.gate_refusal

    def test_a_row_that_vanished_from_one_arm_lowers_its_completion_rate(self, tmp_path: Path) -> None:
        # Computed over the paired intersection, this check reported 8/8 against 8/8 and PASSED
        # while two of the incumbent's rows were missing from the candidate entirely.
        incumbent = {**_WINNER["incumbent"], "r5": [0.4, 0.5], "r6": [0.4, 0.5]}
        run_dir = _exec_run_dir(tmp_path, incumbent=incumbent, candidate=_WINNER["candidate"])
        verdict = _exec_gate(run_dir)
        completion = next(c for c in verdict.integrity_checks if c.name == "completion_rate")
        assert completion.incumbent == 1.0
        assert completion.candidate is not None and completion.candidate < 1.0
        assert not completion.passed
        assert any("scored for one arm only" in note for note in verdict.notes)

    def test_the_checks_are_computed_over_the_rows_the_statistic_paired(self, tmp_path: Path) -> None:
        # A row on disk for both arms but carrying no score for one is IN the disk intersection and
        # OUT of the pairing, so the two sets differ — and a guardrail must guard its own sample.
        run_dir = _exec_run_dir(tmp_path, **_WINNER)
        raw = (run_dir / "experiment.json").read_text(encoding="utf-8")
        result = ExperimentResult.model_validate_json(raw)
        scores = {v: dict(per) for v, per in result.per_replicate_scores.items()}
        scores["candidate"][f"{EXEC_SUITE}/r4"] = []
        (run_dir / "experiment.json").write_text(
            result.model_copy(update={"per_replicate_scores": scores}).model_dump_json(), encoding="utf-8"
        )
        verdict = _exec_gate(run_dir)
        assert verdict.rows_paired == 3
        assert verdict.rows_excluded == 1
        assert any("scored for one arm only" in note for note in verdict.notes)


class TestTheSiblingCheckIsBalancedLikeThePrimaryOne:
    """A replicate imbalance must not move a sibling's recall — the check gates `promoted`."""

    @staticmethod
    def _rows(replicates: int, *, annexed: bool = False) -> dict[str, list[EvaluationResult]]:
        # Criterion 0 is the primary; criterion 1 is the sibling, true-yes on both rows.
        sibling = ("yes", "no") if annexed else ("yes", "yes")
        return {
            "r1": [_eval_result("r1", [("yes", "yes"), sibling])] * replicates,
            "r2": [_eval_result("r2", [("yes", "yes"), ("yes", "yes")])] * replicates,
        }

    def test_identical_labels_read_identically_despite_an_extra_replicate(self) -> None:
        # Before balancing: recall 0.5 vs 0.6 on byte-identical labels, purely from one row's
        # extra replicate — and the sibling check is folded into `promoted`.
        check = _sibling_checks(
            incumbent_rows=self._rows(2),
            candidate_rows={**self._rows(2), "r1": self._rows(3)["r1"]},
            paired_row_ids=["r1", "r2"],
            sibling_indices=[1],
        )[0]
        assert check.incumbent == check.candidate
        assert check.passed

    def test_the_annexation_rate_is_aligned_within_a_row_not_across_the_flattened_list(self) -> None:
        # An unbalanced row used to shift every later row's alignment, so a candidate that annexed
        # half the sibling's true rows rendered `rate` 0.000 — "took nothing".
        incumbent = {
            "r1": [_eval_result("r1", [("yes", "yes"), ("yes", "yes")])] * 3,
            "r2": [_eval_result("r2", [("yes", "yes"), ("yes", "yes")])] * 2,
        }
        candidate = {
            "r1": [_eval_result("r1", [("yes", "yes"), ("yes", "yes")])] * 2,
            "r2": [_eval_result("r2", [("yes", "yes"), ("yes", "no")])] * 2,
        }
        check = _sibling_checks(
            incumbent_rows=incumbent, candidate_rows=candidate, paired_row_ids=["r1", "r2"], sibling_indices=[1]
        )[0]
        # 4 balanced true-yes observations; the candidate turned r2's 2 into "no".
        assert check.rate == pytest.approx(0.5)
        assert not check.passed

    def test_a_one_sided_sibling_is_still_detected_after_balancing(self) -> None:
        # Balancing trims a one-sided row to nothing, so PRESENCE must be read from the untrimmed
        # pools or the "results on one arm only" note disappears.
        incumbent = {"r1": [_eval_result("r1", [("yes", "yes"), ("yes", "yes")])]}
        candidate = {"r1": [_eval_result("r1", [("yes", "yes")])]}
        check = _sibling_checks(
            incumbent_rows=incumbent, candidate_rows=candidate, paired_row_ids=["r1"], sibling_indices=[1]
        )[0]
        assert check.note is not None and "one arm only" in check.note
        assert check.passed and check.rate is None


class TestGuardrailsNeverRaiseOnACallerSuppliedRow:
    def test_a_row_id_absent_from_the_arms_is_skipped_not_indexed(self) -> None:
        # `execution_gate` passes the rows `paired_comparison` paired, which come from
        # experiment.json — an id named there but missing on disk used to raise a KeyError out of
        # the skill's inline snippet, discarding the wrong-path note composed just above it.
        checks = cost_latency_guardrails(incumbent_rows={}, candidate_rows={}, row_ids=["ghost-row"])
        assert [c.passed for c in checks] == [True, True]
        assert all(c.note is not None and "not evaluated" in c.note for c in checks)

    def test_an_unfanned_single_task_suite_returns_a_noted_verdict(self, tmp_path: Path) -> None:
        # `scoped_scores` keeps `task_id == suite_id`, so `removeprefix` leaves the SUITE id as the
        # row id — which no row directory is named. The contract is a noted verdict, not a raise.
        run_dir = tmp_path / "round1-gate"
        _experiment_json(
            run_dir,
            ["incumbent", "candidate"],
            {"incumbent": {EXEC_SUITE: [0.4, 0.5]}, "candidate": {EXEC_SUITE: [0.8, 0.9]}},
        )
        verdict = _exec_gate(run_dir)
        # The row tree is what is missing, and that outranks the "fewer than 2 paired rows" the
        # unfanned scores also produce: an arm with no rows at all is the more specific fault.
        assert verdict.gate_refusal is not None and "loaded ZERO rows" in verdict.gate_refusal
        assert [c.passed for c in verdict.guardrails] == [True, True]


def _leak_row(row_id: str, criteria: list) -> TaskDefinition:
    """One expanded train row — what `expand_dataset` hands `candidate_leaks`."""
    return TaskDefinition(
        task_id=f"{SUITE}/{row_id}",
        description="leak fixture",
        initial_prompt="do the thing",
        success_criteria=criteria,
    )


# The real shape: a criterion asserting a substantive string on a file the row expects.
_GRADED = "minimum-task-score"
_ROWS = [_leak_row("r1", [FileCheckCriterion(description="d", path="out.yml", includes=[_GRADED])])]


class TestCandidateLeaks:
    """The anti-memorization preflight. It is a DIFF, and every test here is about that."""

    def test_flags_a_span_the_candidate_adds(self) -> None:
        findings = candidate_leaks(f"set {_GRADED} in the workflow", "an incumbent that says nothing", _ROWS)
        assert len(findings) == 1
        assert _GRADED in findings[0] and "r1" in findings[0] and "file_check" in findings[0]

    def test_returns_nothing_when_the_baseline_already_says_it(self) -> None:
        """THE false-positive regression, and the reason this function takes a baseline.

        Measured against this repo's own `tasks/skills/ci-outcome.yaml`, an absolute scan flags the
        shipped `ci` skill on five strings that are simply the output contract its suite grades. A
        checker that fires on the shipped skill on its first run is one users learn to ignore.

        Built as candidate = baseline + unrelated prose rather than as the degenerate
        candidate == baseline: that is the real shape (a candidate is an EDIT of its baseline), and
        it proves the containment is per-span rather than a whole-text equality.
        """
        baseline = f"The workflow must set {_GRADED} to the project's floor."
        assert candidate_leaks(baseline, baseline, _ROWS) == []
        assert candidate_leaks(baseline + "\n\nAlso prefer the smallest edit that could work.", baseline, _ROWS) == []

    def test_a_clean_candidate_is_clean(self) -> None:
        assert candidate_leaks("nothing graded here at all", "", _ROWS) == []

    def test_an_empty_candidate_leaks_nothing(self) -> None:
        # Degenerate input returns empty rather than raising. (Not the emptied-body control, which
        # keeps its frontmatter and is skipped by the skill's loop anyway.)
        assert candidate_leaks("", "", _ROWS) == []

    def test_empty_rows_return_empty_rather_than_raising(self) -> None:
        assert candidate_leaks(f"a body mentioning {_GRADED}", "", []) == []

    def test_an_empty_baseline_flags_everything_present(self) -> None:
        # Correct rather than noisy: a baseline with no body cannot have contributed anything.
        assert len(candidate_leaks(f"we set {_GRADED} here", "", _ROWS)) == 1

    def test_identical_findings_are_de_duplicated(self) -> None:
        # `tasks/skills/ci-outcome.yaml` really carries `includes: [x, x]` on one row, and before
        # this the real train split produced 14 lines for 5 distinct spans — noise in a check whose
        # design rationale is not firing more than it has to.
        rows = [_leak_row("r1", [FileCheckCriterion(description="d", path="o.yml", includes=[_GRADED, _GRADED])])]
        assert len(candidate_leaks(f"we set {_GRADED} here", "", rows)) == 1

    def test_matching_is_case_insensitive(self) -> None:
        # Both consumers lower-case both sides; disagreeing about that would make "a leak" mean
        # two different things in the two rules that share this primitive.
        assert len(candidate_leaks(f"we set {_GRADED.upper()} here", "", _ROWS)) == 1

    def test_a_locator_value_in_the_candidate_does_not_flag(self) -> None:
        # Naming WHERE an artifact goes removes filename nondeterminism from the measurement
        # without revealing what is graded, so a body that names the path is not memorizing.
        rows = [_leak_row("r1", [FileCheckCriterion(description="d", path=".github/workflows/evals.yml")])]
        assert candidate_leaks("write it to .github/workflows/evals.yml", "", rows) == []

    def test_the_skills_own_name_does_not_flag(self) -> None:
        # CE036's `skill_name` exemption, pointed the other way: an outcome suite names the skill
        # in every row by design, and the skill's own body names itself constantly.
        #
        # The floor guard is the whole test. A name shorter than LEAK_MIN_CHARS returns [] whether
        # or not `skill_name` is exempt, so without it this passes on a rule that exempts nothing.
        # CE036's twin carries the identical assertion for the identical reason.
        assert len("optimize-skill") >= LEAK_MIN_CHARS, "fixture no longer exercises the floor"
        rows = [
            _leak_row("r1", [SkillTriggeredCriterion(description="d", skill_name="optimize-skill", expected_skill="")])
        ]
        assert candidate_leaks("This is the optimize-skill skill and it does things", "", rows) == []

    def test_the_criterion_type_does_not_flag(self) -> None:
        """`drop_type=True`, and it was found by measurement rather than by reasoning.

        Without it, `optimize-skill`'s own body — which discusses eval criteria at length — flags
        on `skill_triggered`, the criterion's DISCRIMINATOR rather than any graded content. A pure
        false positive on any skill whose body discusses evaluation.
        """
        rows = [_leak_row("r1", [SkillTriggeredCriterion(description="d", skill_name="s", expected_skill="")])]
        assert candidate_leaks("stack a skill_triggered criterion on every row", "", rows) == []

    def test_a_leak_nested_in_a_list_is_caught(self) -> None:
        rows = [_leak_row("r1", [FileCheckCriterion(description="d", path="o.yml", includes=["harmless-xx", _GRADED])])]
        assert len(candidate_leaks(f"body mentioning {_GRADED}", "", rows)) == 1

    def test_it_takes_no_min_chars_parameter(self) -> None:
        # One value, read from the constant. A parameter would be a second declaration of a
        # number CE036 and this function must agree on.
        assert "min_chars" not in inspect.signature(candidate_leaks).parameters


def _arm(variant: str, scores: dict[str, float]) -> ArmRowScores:
    return ArmRowScores(variant_id=variant, row_scores=scores)


class TestLineageHeadScores:
    """The head lookup, which the skill's snippet used to do with a bare `next()`/`max()`."""

    def _measurements(self, *rounds: RoundScores) -> OptimizeMeasurements:
        return OptimizeMeasurements(skill="my-skill", round_scores=list(rounds))

    def test_none_when_no_round_recorded_a_head(self) -> None:
        rounds = RoundScores(round=1, arm_row_scores=[_arm("a", {"r1": 1.0})])
        assert lineage_head_scores(self._measurements(rounds)) is None

    def test_none_on_an_empty_sidecar(self) -> None:
        assert lineage_head_scores(self._measurements()) is None

    def test_takes_the_highest_round_that_named_one(self) -> None:
        # Highest ROUND, not last-in-list: the sidecar replaces per round, so ordering is a
        # write-order artefact while `round` is the real sequence.
        early = RoundScores(round=3, arm_row_scores=[_arm("a", {"r1": 1.0})], lineage_head="a")
        late = RoundScores(round=7, arm_row_scores=[_arm("b", {"r1": 0.5})], lineage_head="b")
        head = lineage_head_scores(self._measurements(late, early))
        assert head is not None and head.variant_id == "b"

    def test_skips_a_later_round_that_accepted_nothing(self) -> None:
        # A round with no accept leaves the head where it was; it must not blank the lineage.
        kept = RoundScores(round=2, arm_row_scores=[_arm("a", {"r1": 1.0})], lineage_head="a")
        quiet = RoundScores(round=3, arm_row_scores=[_arm("b", {"r1": 0.5})])
        head = lineage_head_scores(self._measurements(kept, quiet))
        assert head is not None and head.variant_id == "a"


class TestSearchCompare:
    """The search loop's accept/revert decision, which used to be arithmetic in a markdown block.

    Every guard here was previously a line an agent had to copy faithfully.
    """

    _HEAD: ClassVar[dict[str, float]] = {"r1": 1.0, "r2": 0.0, "r3": 1.0, "r4": 0.0}  # mean 0.5

    def test_a_better_candidate_is_accepted(self) -> None:
        result = search_compare(_arm("head", self._HEAD), _arm("cand", {"r1": 1.0, "r2": 1.0, "r3": 1.0, "r4": 0.0}))
        assert result.beats and result.accepted
        assert result.head_score == 0.5 and result.candidate_score == 0.75
        assert result.shared_rows == ("r1", "r2", "r3", "r4")
        assert result.blocker is None

    def test_a_worse_candidate_is_not_accepted(self) -> None:
        result = search_compare(_arm("head", self._HEAD), _arm("cand", dict.fromkeys(self._HEAD, 0.0)))
        assert not result.beats and not result.accepted
        assert result.blocker is None, "losing is an ordinary result, not a blocked one"

    def test_a_tie_is_not_a_win(self) -> None:
        # Strictly greater. A tie that advanced the head would move the bar on an accident, and
        # the next round would then have to beat a number nothing earned.
        result = search_compare(_arm("head", self._HEAD), _arm("cand", dict(self._HEAD)))
        assert result.head_score == result.candidate_score
        assert not result.beats and not result.accepted

    def test_no_shared_rows_is_a_wiring_blocker_not_a_hole(self) -> None:
        # Checked BEFORE holes: no overlap at all is a wiring fault, and reporting it as a hole
        # sends the reader looking for a flaky row instead of an unpinned sample seed.
        result = search_compare(_arm("head", self._HEAD), _arm("cand", {"other": 1.0}))
        assert not result.accepted and result.head_score is None and result.candidate_score is None
        assert result.blocker is not None and "sample_seed" in result.blocker

    def test_a_hole_refuses_rather_than_averaging_around_it(self) -> None:
        # The candidate errored on r2 — its mean over the survivors would be 1.0 and would "win".
        result = search_compare(_arm("head", self._HEAD), _arm("cand", {"r1": 1.0, "r3": 1.0, "r4": 1.0}))
        assert not result.accepted and result.holes == ("r2",)
        assert result.head_score is None, "a refused comparison must report no number to misread"
        assert result.blocker is not None and "r2" in result.blocker

    def test_a_row_only_the_candidate_scored_is_not_a_hole(self) -> None:
        # Holes are asymmetric on purpose: an extra row the head never measured cannot make the
        # candidate look better, because the comparison runs over the intersection either way.
        result = search_compare(_arm("head", self._HEAD), _arm("cand", {**self._HEAD, "r5": 1.0}))
        assert result.holes == () and result.blocker is None
        assert result.shared_rows == ("r1", "r2", "r3", "r4")

    def test_a_corpus_regression_blocks_an_otherwise_winning_candidate(self) -> None:
        # THE reason the corpus is read here rather than at the next Stage A: an accept advances
        # the lineage, so a re-lost row rides forward until a multi-arm round notices.
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        result = search_compare(
            _arm("head", self._HEAD),
            _arm("cand", {"r1": 0.0, "r2": 1.0, "r3": 1.0, "r4": 1.0}),
            corpus=corpus,
        )
        assert result.beats, "the aggregate really does improve — that is what makes this dangerous"
        assert not result.accepted
        assert [row.row_id for row, _ in result.regressions] == ["r1"]
        assert result.blocker is not None and "oblique phrasing" in result.blocker

    def test_a_corpus_row_the_candidate_holds_does_not_block(self) -> None:
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        result = search_compare(
            _arm("head", self._HEAD),
            _arm("cand", {"r1": 1.0, "r2": 1.0, "r3": 1.0, "r4": 0.0}),
            corpus=corpus,
        )
        assert result.accepted and result.regressions == () and result.blocker is None

    def test_the_corpus_threshold_is_forwarded(self) -> None:
        # A fractional execution suite needs a bar other than 1.0; the parameter exists for it.
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="partial credit is fine")]
        candidate = _arm("cand", {"r1": 0.8, "r2": 1.0, "r3": 1.0, "r4": 1.0})
        assert not search_compare(_arm("head", self._HEAD), candidate, corpus=corpus).accepted
        assert search_compare(_arm("head", self._HEAD), candidate, corpus=corpus, threshold=0.5).accepted

    def test_a_losing_candidate_is_not_blocked_by_the_corpus(self) -> None:
        # It already failed on the score; adding a corpus blocker would misreport WHY.
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        result = search_compare(_arm("head", self._HEAD), _arm("cand", dict.fromkeys(self._HEAD, 0.0)), corpus=corpus)
        assert not result.beats and not result.accepted and result.blocker is None

    def test_an_empty_head_is_a_blocker_not_a_crash(self) -> None:
        # `RoundScores`' validator makes this unreachable through the sidecar, but the function
        # is public and must not divide by zero on a hand-built arm.
        result = search_compare(_arm("head", {}), _arm("cand", {"r1": 1.0}))
        assert not result.accepted and result.blocker is not None


class TestAcceptedIsDerived:
    """`accepted` was a field every construction site set to `beats and blocker is None`.

    Two spellings of one rule, settable inconsistently by any caller, with nothing to notice.
    """

    def _comparison(self, *, beats: bool, blocker: str | None) -> SearchComparison:
        return SearchComparison(
            beats=beats,
            head_score=0.5,
            candidate_score=0.75,
            shared_rows=("r1",),
            holes=(),
            regressions=(),
            blocker=blocker,
        )

    def test_accepted_is_not_stored(self) -> None:
        assert "accepted" not in SearchComparison._fields
        assert len(SearchComparison._fields) == 7

    def test_a_winning_unblocked_candidate_is_accepted(self) -> None:
        assert self._comparison(beats=True, blocker=None).accepted is True

    def test_a_blocker_defeats_a_win(self) -> None:
        assert self._comparison(beats=True, blocker="a corpus regression").accepted is False

    def test_accepted_cannot_be_set_inconsistently(self) -> None:
        # The state a caller could previously have stored as `accepted=True`: it did not win.
        assert self._comparison(beats=False, blocker=None).accepted is False

    def test_replace_on_the_dropped_field_now_raises(self) -> None:
        # The failure mode the keyword-form construction sites exist to make loud rather than
        # silent — a positional build would have shifted every later argument instead.
        # `TypeError` on 3.13, not the `ValueError` older CPython raised here.
        with pytest.raises(TypeError, match="accepted"):
            self._comparison(beats=True, blocker=None)._replace(accepted=False)


class TestRenderSearchComparison:
    def test_an_accepted_comparison_names_both_numbers_and_the_row_count(self) -> None:
        block = render_search_comparison(
            search_compare(_arm("head", {"r1": 0.0, "r2": 1.0}), _arm("cand", {"r1": 1.0, "r2": 1.0}))
        )
        assert "ACCEPT" in block and "0.500" in block and "1.000" in block and "2" in block

    def test_a_blocked_comparison_leads_with_the_blocker(self) -> None:
        block = render_search_comparison(search_compare(_arm("head", {"r1": 1.0}), _arm("cand", {"other": 1.0})))
        assert "sample_seed" in block
        # Read the discriminating LINE. The old form here asserted that "ACCEPT" was absent from
        # the block once the negative headline had been stripped out of it — vacuous on this
        # fixture, whose headline is CANNOT COMPARE: the strip removed nothing, so the absence
        # assertion could not fail while reading as a strong guard.
        #
        # Not `_headline`: that helper returns the first line starting with `**`, and
        # `render_search_comparison` leads with an `###` heading on all three of its paths, so
        # calling it here raises StopIteration. Same discipline, differently-shaped block.
        assert block.splitlines()[0] == "### Search round — CANNOT COMPARE"

    def test_a_corpus_regression_renders_do_not_accept(self) -> None:
        """The other headline, asserted as PRESENT on the input that produces it.

        `DO NOT ACCEPT` and `CANNOT COMPARE` carry opposite meanings — a candidate that WINS on
        the aggregate but re-lost a corpus row, against one where no comparison could be made at
        all. Each is now pinned on its own input, so neither can be produced by the other's path.
        """
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        comparison = search_compare(
            _arm("head", {"r1": 1.0, "r2": 0.0, "r3": 0.0, "r4": 0.0}),
            _arm("cand", {"r1": 0.0, "r2": 1.0, "r3": 1.0, "r4": 1.0}),
            corpus=corpus,
        )
        assert comparison.beats and not comparison.accepted

        block = render_search_comparison(comparison)
        assert block.splitlines()[0] == "### Search round — DO NOT ACCEPT"
        assert "oblique phrasing" in block
        # Both train scores print: a reader has to see that the aggregate really did improve, or
        # the block reads as an ordinary loss rather than as the trap it is.
        assert "0.750" in block and "0.250" in block

    def test_a_none_score_renders_a_dash_rather_than_raising(self) -> None:
        # Both scores are `float | None` on the model and were formatted with a bare `:.3f`, which
        # raises. `search_compare` refuses before producing a `None` score today, so this builds
        # the tuple directly — the function is public, and a TypeError out of the skill's inline
        # snippet would discard the block it was rendering.
        blocked = SearchComparison(
            beats=True,
            head_score=None,
            candidate_score=None,
            shared_rows=("r1",),
            holes=(),
            regressions=(),
            blocker="a blocker",
        )
        assert "—" in render_search_comparison(blocked)
        # `accepted` is derived, so clearing the blocker is the whole edit — with `beats=True` and
        # no blocker the property already reads True.
        unblocked = blocked._replace(blocker=None)
        assert unblocked.accepted is True
        assert "—" in render_search_comparison(unblocked)

    def test_it_says_a_search_accept_is_not_a_promotion(self) -> None:
        # The block is printed into a ledger a human reads later, and this is the one thing that
        # must not be inferred from a green word.
        block = render_search_comparison(search_compare(_arm("head", {"r1": 0.0}), _arm("cand", {"r1": 1.0})))
        assert "not a promotion" in block.lower()


# ---------------------------------------------------------------------------
# Row-selection provenance: the gate stops pairing a train run against a test run
# ---------------------------------------------------------------------------


def _set_split(run_dir: Path, split: str | None) -> None:
    """Overwrite a fixture run dir's recorded split (``_write_row`` stamps `None` by default)."""
    (run_dir / "run.json").write_text(
        json.dumps({"row_selection": {"split": split, "max_rows": None, "sample_per_stratum": None}}),
        encoding="utf-8",
    )


class TestReadSplitProvenance:
    """`None` (no --split was passed) and "unrecorded" (we cannot tell) are different answers."""

    def test_a_recorded_split_is_read(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "r"
        _write_row(run_dir, "v", "r1", _eval_result("r1", [("yes", "yes")]))
        _set_split(run_dir, "train")
        assert read_split_provenance([run_dir]) == SplitProvenance(recorded=frozenset({"train"}), unrecorded=0)

    def test_a_recorded_null_split_is_recorded_not_unrecorded(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "r"
        _write_row(run_dir, "v", "r1", _eval_result("r1", [("yes", "yes")]))
        provenance = read_split_provenance([run_dir])
        assert provenance == SplitProvenance(recorded=frozenset({None}), unrecorded=0)
        assert provenance.value is None

    @pytest.mark.parametrize(
        ("name", "write"),
        [
            ("no run.json", lambda p: None),
            ("unparseable JSON", lambda p: (p / "run.json").write_text("{not json", encoding="utf-8")),
            ("run.json is a list", lambda p: (p / "run.json").write_text("[]", encoding="utf-8")),
            ("no row_selection key", lambda p: (p / "run.json").write_text('{"run_id": "x"}', encoding="utf-8")),
            (
                "row_selection is null",
                lambda p: (p / "run.json").write_text('{"row_selection": null}', encoding="utf-8"),
            ),
        ],
    )
    def test_every_unreadable_shape_counts_as_unrecorded(self, tmp_path: Path, name: str, write) -> None:
        run_dir = tmp_path / "r"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").unlink(missing_ok=True)
        write(run_dir)
        provenance = read_split_provenance([run_dir])
        assert provenance == SplitProvenance(recorded=frozenset(), unrecorded=1), name
        assert provenance.value == UNRECORDED_SPLIT

    def test_mixed_dirs_report_both_halves(self, tmp_path: Path) -> None:
        recorded = tmp_path / "a"
        _write_row(recorded, "v", "r1", _eval_result("r1", [("yes", "yes")]))
        _set_split(recorded, "test")
        missing = tmp_path / "b"
        missing.mkdir()
        provenance = read_split_provenance([recorded, missing])
        assert provenance == SplitProvenance(recorded=frozenset({"test"}), unrecorded=1)
        # Any unrecorded dir makes the whole measurement uncacheable, whatever the others said.
        assert provenance.value == UNRECORDED_SPLIT

    def test_different_recorded_splits_are_mismatched(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        for run_dir, split in ((a, "train"), (b, "test")):
            _write_row(run_dir, "v", "r1", _eval_result("r1", [("yes", "yes")]))
            _set_split(run_dir, split)
        assert read_split_provenance([a, b]).mismatched is True

    def test_the_same_split_everywhere_is_not_mismatched(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        for run_dir in (a, b):
            _write_row(run_dir, "v", "r1", _eval_result("r1", [("yes", "yes")]))
            _set_split(run_dir, "train")
        provenance = read_split_provenance([a, b])
        assert provenance.mismatched is False and provenance.value == "train"


class TestCrossSplitRefusal:
    """A train arm against a test arm is not a weak comparison — it is not a comparison."""

    @staticmethod
    def _arms(tmp_path: Path, incumbent_split: str | None, candidate_split: str | None):
        labels = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(6)}
        inc = _write_arm(tmp_path, "incumbent", labels, invocations=2, prefix="inc-")
        cand = _write_arm(tmp_path, "candidate", labels, invocations=2, prefix="cand-")
        for d in inc:
            _set_split(d, incumbent_split)
        for d in cand:
            _set_split(d, candidate_split)
        return inc, cand

    @staticmethod
    def _gate(inc, cand):
        return activation_gate(
            incumbent_run_dirs=inc,
            candidate_run_dirs=cand,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=_FAST_RESAMPLES,
        )

    def test_a_train_vs_test_pair_is_refused_with_both_splits_named(self, tmp_path: Path) -> None:
        verdict = self._gate(*self._arms(tmp_path, "train", "test"))
        assert verdict.gate_refusal is not None
        assert "'train'" in verdict.gate_refusal and "'test'" in verdict.gate_refusal
        # No statistic is reported: there was nothing to compute one over.
        assert verdict.p_value is None
        assert verdict.mean_diff is None and verdict.ci_low is None and verdict.ci_high is None
        assert verdict.incumbent_f1 is None and verdict.candidate_f1 is None
        # Left for holm_promote, exactly like every other activation verdict.
        assert verdict.promoted is None
        # It still says what it LOADED, so the reader can see the arms were otherwise fine.
        assert verdict.rows_paired > 0

    def test_a_recorded_null_split_against_a_named_one_is_also_refused(self, tmp_path: Path) -> None:
        """A full-suite run and a --split run scored different row sets just as surely."""
        verdict = self._gate(*self._arms(tmp_path, None, "test"))
        assert verdict.gate_refusal is not None and verdict.p_value is None

    def test_matching_splits_produce_no_refusal_and_no_note(self, tmp_path: Path) -> None:
        """Silence is the correct output for a correctly wired gate."""
        verdict = self._gate(*self._arms(tmp_path, "train", "train"))
        assert verdict.gate_refusal is None
        assert not any("provenance" in note for note in verdict.notes)

    def test_one_unrecorded_arm_notes_but_does_not_refuse(self, tmp_path: Path) -> None:
        """The recorded arm proves nothing about the other, so a note — not a refusal."""
        inc, cand = self._arms(tmp_path, "train", "train")
        (cand[0] / "run.json").unlink()
        verdict = self._gate(inc, cand)
        assert verdict.gate_refusal is None
        assert verdict.p_value is not None, "an unrecorded arm must stay gatable"
        assert any("provenance is missing from 1 of 4" in note for note in verdict.notes)

    def test_a_within_arm_mismatch_is_caught_too(self, tmp_path: Path) -> None:
        """Stage B runs one arm three times; those three can disagree with each other."""
        inc, cand = self._arms(tmp_path, "train", "train")
        _set_split(inc[0], "test")
        verdict = self._gate(inc, cand)
        assert verdict.gate_refusal is not None and verdict.p_value is None

    def test_the_refusal_blocks_a_promotion_that_would_otherwise_happen(self, tmp_path: Path) -> None:
        """The assertion that proves the preflight does something.

        The other tests here use a zero-discordant fixture that could never promote, so they would
        pass with the whole preflight deleted. This one uses the clear-win fixture — incumbent
        engages 3 of 12, candidate 12 of 12 — which promotes reliably on matching splits. The ONLY
        difference between the two halves is the recorded split.
        """
        labels_inc = {f"r{i}": [("yes", "yes" if i < 3 else "no")] for i in range(12)}
        labels_cand = {f"r{i}": [("yes", "yes")] for i in range(12)}

        def _pair(root: Path, inc_split: str | None, cand_split: str | None):
            inc = _write_arm(root, "incumbent", labels_inc, invocations=2, prefix="inc-")
            cand = _write_arm(root, "candidate", labels_cand, invocations=2, prefix="cand-")
            for d in inc:
                _set_split(d, inc_split)
            for d in cand:
                _set_split(d, cand_split)
            return inc, cand

        (promoted,) = holm_promote([self._gate(*_pair(tmp_path / "same", "train", "train"))])
        assert promoted.promoted is True, "the fixture must promote when the splits agree"

        (blocked,) = holm_promote([self._gate(*_pair(tmp_path / "crossed", "train", "test"))])
        assert blocked.promoted is False
        assert blocked.gate_refusal is not None

    def test_holm_forces_not_promoted_and_keeps_the_refusal(self, tmp_path: Path) -> None:
        verdict = self._gate(*self._arms(tmp_path, "train", "test"))
        (decided,) = holm_promote([verdict])
        assert decided.promoted is False
        assert decided.gate_refusal == verdict.gate_refusal
        # Outside the family by the p-based rule, so the "outside the family" note would be
        # redundant AND contradictory under a refusal headline.
        assert _NOTE_OUTSIDE_FAMILY not in decided.notes

    def test_a_refused_verdict_does_not_shrink_a_siblings_holm_threshold(self, tmp_path: Path) -> None:
        """Family membership is `p_value is not None` and nothing else.

        Dropping a refused verdict from the family would shrink `m` and LOOSEN `alpha/m` for
        every sibling — the uncorrected-p degeneration, arrived at from the other side.
        """
        refused = self._gate(*self._arms(tmp_path / "x", "train", "test"))
        incumbent, candidate = _tiny_suite(positives=6, distractors=6)
        sibling = _gate(_shared_dirs(tmp_path / "y", incumbent, candidate))
        assert sibling.p_value is not None, "the sibling fixture must actually produce a p"

        alone = holm_promote([sibling])[0]
        with_refused = next(v for v in holm_promote([sibling, refused]) if v.p_value is not None)
        # Asserted on the RANK-DEPENDENT quantity, not on `holm_alpha`: that one is assigned
        # `alpha` unconditionally in both branches, so comparing it is 0.05 == 0.05 and passes
        # even with the family filter broken. `_note_ordinary_negative` and `_note_holm_family`
        # both spell the family SIZE, which is the number a dropped verdict would move.
        assert _note_holm_family(1, DEFAULT_ALPHA) in alone.notes
        assert _note_holm_family(1, DEFAULT_ALPHA) in with_refused.notes, (
            "the refused verdict was counted in the family — dropping it would shrink m and "
            "LOOSEN alpha/m for every sibling"
        )
        assert alone.promoted == with_refused.promoted


class TestCrossSplitRendering:
    """The refusal must reach the reader whatever state the verdict is in."""

    @staticmethod
    def _refused(tmp_path: Path):
        return TestCrossSplitRefusal._gate(*TestCrossSplitRefusal._arms(tmp_path, "train", "test"))

    def test_after_holm_the_headline_is_not_a_result(self, tmp_path: Path) -> None:
        (decided,) = holm_promote([self._refused(tmp_path)])
        block = render_markdown(decided)
        assert "**NOT A RESULT — " in block
        # The discreteness refusal's headline is a different claim and must not appear.
        assert "CANNOT SEPARATE AT THIS SIZE" not in block

    def test_before_holm_undecided_wins_the_headline_but_the_reason_still_prints(self, tmp_path: Path) -> None:
        """The regression the execution renderer's comment describes: UNDECIDED outranks the
        refusal, so without an own-line fallback the reason lands nowhere on the page."""
        block = render_markdown(self._refused(tmp_path))
        assert "**UNDECIDED" in block
        assert "**NOT A RESULT:** " in block
        assert "DIFFERENT --split values" in block

    def test_an_ordinary_discreteness_refusal_still_says_cannot_separate(self, tmp_path: Path) -> None:
        """Regression guard for the new branch: that one carries a p and keeps its own headline.

        6 rows / 3 discordant gives a floor of 0.031 against a family-of-2 threshold of 0.025 —
        the established refusal fixture, which crucially DOES compute a p.
        """
        incumbent, candidate = _tiny_suite(positives=3, distractors=3)
        dirs = _shared_dirs(tmp_path, incumbent, candidate)
        decided = holm_promote([_gate(dirs, n_resamples=2_000) for _ in range(2)])
        for verdict in decided:
            assert verdict.gate_refusal is not None and verdict.p_value is not None
            block = render_markdown(verdict)
            assert "CANNOT SEPARATE AT THIS SIZE" in block
            assert "NOT A RESULT" not in block


class TestAllNegativeSubsetNote:
    """Two suites that render byte-identically today, and only one of them is a measurement."""

    @staticmethod
    def _decided(tmp_path: Path, pairs: tuple[str, str]):
        rows = {f"r{i}": [pairs] for i in range(8)}
        dirs = _shared_dirs(tmp_path, rows, rows)
        (decided,) = holm_promote([_gate(dirs, n_resamples=2_000)])
        return decided

    def test_a_suite_with_no_yes_anywhere_names_the_missing_positive_rows(self, tmp_path: Path) -> None:
        decided = self._decided(tmp_path, ("no", "no"))
        note = "\n".join(decided.notes)
        assert "undefined on BOTH arms" in note
        assert "expected_skill" in note and "--split" in note

    def test_rows_that_expect_yes_but_nobody_engaged_get_no_such_note(self, tmp_path: Path) -> None:
        """That one IS a real measurement: the label is present, both arms simply failed it."""
        decided = self._decided(tmp_path, ("yes", "no"))
        assert not any("undefined on BOTH arms" in note for note in decided.notes)

    def test_the_two_blocks_are_no_longer_byte_identical(self, tmp_path: Path) -> None:
        """The whole point. Before this note they were the same text with the same wrong remedy."""
        absent = render_markdown(self._decided(tmp_path / "a", ("no", "no")))
        unengaged = render_markdown(self._decided(tmp_path / "b", ("yes", "no")))
        assert absent != unengaged

    def test_a_wiring_fault_with_no_pairs_does_not_get_the_all_negative_note(self, tmp_path: Path) -> None:
        """`any()` over an empty iterable is False, so an unguarded check fires here too.

        A mistyped `criterion_index` scores nothing on either arm. That already has its own note
        naming the index; adding "no row expects or observes 'yes' — check `expected_skill` and
        your --split" puts two contradictory remedies in one block, on the commonest wiring error
        this gate has a dedicated message for. It also breaks the note's own justification: with
        no pairs `n_discordant` is None, so the zero-discordant path does NOT refuse, and the
        "it is already refused anyway" argument does not hold.
        """
        rows = {f"r{i}": [("yes", "no")] for i in range(6)}
        dirs = _shared_dirs(tmp_path, rows, rows)
        verdict = _gate(dirs, criterion_index=9, n_resamples=_FAST_RESAMPLES)

        assert verdict.rows_paired == 0 and verdict.n_discordant is None
        assert not any("undefined on BOTH arms" in note for note in verdict.notes)
        # The note that SHOULD be there still is.
        assert any("criterion_index=9" in note for note in verdict.notes)

    def test_the_note_changes_no_decision(self, tmp_path: Path) -> None:
        """A note, not a refusal: the zero-discordant path still owns the outcome."""
        decided = self._decided(tmp_path, ("no", "no"))
        assert decided.promoted is False
        assert decided.gate_refusal is not None
        assert decided.n_discordant == 0

    def test_the_zero_discordant_remedy_no_longer_claims_rows_cannot_help(self, tmp_path: Path) -> None:
        """In the all-negative case adding POSITIVE rows is exactly the fix, so the old
        unqualified "adding rows cannot change it" was false precisely here."""
        decided = self._decided(tmp_path, ("no", "no"))
        assert decided.gate_refusal is not None
        assert "adding more rows LIKE THESE cannot change it" in decided.gate_refusal


class TestExecutionGateSplitNote:
    """The execution track NOTES its split and never refuses on it.

    That asymmetry with `activation_gate` is a consequence of the data sources: this track takes
    ONE run_dir holding BOTH variants, so the arms share one run.json and one split by
    construction, and a cross-split pair is unrepresentable here.
    """

    def test_the_note_names_the_recorded_split(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, incumbent=_WINNER["incumbent"], candidate=_WINNER["candidate"])
        _set_split(run_dir, "test")
        verdict = _exec_gate(run_dir)
        assert any("--split 'test'" in note for note in verdict.notes)
        assert verdict.gate_refusal is None

    def test_a_full_suite_run_says_nothing(self, tmp_path: Path) -> None:
        """A recorded `split: null` is the ordinary case; silence is the right output."""
        run_dir = _exec_run_dir(tmp_path, incumbent=_WINNER["incumbent"], candidate=_WINNER["candidate"])
        verdict = _exec_gate(run_dir)
        assert not any("--split" in note or "provenance" in note for note in verdict.notes)

    def test_an_unrecorded_run_dir_says_so(self, tmp_path: Path) -> None:
        run_dir = _exec_run_dir(tmp_path, incumbent=_WINNER["incumbent"], candidate=_WINNER["candidate"])
        (run_dir / "run.json").unlink()
        verdict = _exec_gate(run_dir)
        assert any("provenance is missing" in note for note in verdict.notes)
        assert verdict.gate_refusal is None, "provenance is never a refusal on this track"


class TestNoiseFloorCarriesItsSplit:
    """The floor's split is DERIVED from the run dirs, never passed by a caller.

    A caller-supplied `split=` would default to `None` and reintroduce the cross-split bug on the
    first snippet that forgot it — unlike `model`, which a caller at least resolved and can see is
    unresolved.
    """

    @staticmethod
    def _dirs(tmp_path: Path, splits: list[str | None]) -> list[Path]:
        labels = {f"r{i}": [("yes", "yes" if i % 2 else "no")] for i in range(6)}
        dirs = _write_arm(tmp_path, "incumbent", labels, invocations=len(splits))
        for run_dir, split in zip(dirs, splits, strict=True):
            _set_split(run_dir, split)
        return dirs

    def test_a_matching_split_is_stamped_on_the_floor(self, tmp_path: Path) -> None:
        floor = measure_noise_floor(
            run_dirs=self._dirs(tmp_path, ["train", "train"]),
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            model="claude-haiku-4-5",
            n_resamples=_FAST_RESAMPLES,
        )
        assert floor is not None and floor.split == "train"

    def test_mismatched_splits_refuse_to_measure_and_log_why(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            floor = measure_noise_floor(
                run_dirs=self._dirs(tmp_path, ["train", "test"]),
                variant_id="incumbent",
                suite_id=SUITE,
                criterion_index=0,
                model="claude-haiku-4-5",
                n_resamples=_FAST_RESAMPLES,
            )
        assert floor is None, "a null split pooled across two row sets is not a floor"
        assert "DIFFERENT row selections" in caplog.text

    def test_an_unrecorded_dir_makes_the_floor_uncacheable(self, tmp_path: Path) -> None:
        dirs = self._dirs(tmp_path, ["train", "train"])
        (dirs[0] / "run.json").unlink()
        floor = measure_noise_floor(
            run_dirs=dirs,
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            model="claude-haiku-4-5",
            n_resamples=_FAST_RESAMPLES,
        )
        # Still MEASURED — an unrecorded run dir stays usable — but carrying the sentinel, so
        # `record_noise_floor` refuses to write it and no lookup can ever match it.
        assert floor is not None and floor.split == UNRECORDED_SPLIT

    def test_the_execution_floor_carries_its_split_too(self, tmp_path: Path) -> None:
        dirs = _weighted_arm(tmp_path, "incumbent", {f"r{i}": [0.2, 0.55, 0.9] for i in range(8)})
        _set_split(dirs[0], "test")
        floor = _execution_floor(dirs)
        assert floor is not None and floor.split == "test"
