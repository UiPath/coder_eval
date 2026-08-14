"""Unit tests for the activation track's promotion gate (`coder_eval.optimize_gate`).

Fixtures write real ``task.json`` files into a ``tmp_path`` run-dir tree, so the loader is
exercised against the on-disk contract rather than against a mock of it.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.models import (
    ActivationGateVerdict,
    ArmRowScores,
    ClassificationCriterionResult,
    CriterionResult,
    EvaluationResult,
    FinalStatus,
    GuardrailCheck,
    NoiseFloor,
    TokenUsage,
)
from coder_eval.optimize_gate import (
    COST_FRONT_ADVISORY,
    GATE_MAX_FAMILY,
    GATE_P_PRECISION,
    GATE_RESAMPLES,
    MATERIALITY_FLOOR,
    CostQualityPoint,
    _discreteness_floor,
    _holm_threshold,
    _label_pairs,
    _median,
    _row_cost_levels,
    _row_costs,
    activation_gate,
    arm_row_scores,
    cost_latency_guardrails,
    cost_quality_front,
    cost_quality_points,
    holm_promote,
    instance_best_front,
    load_arm_rows,
    load_suite_rows,
    measure_execution_noise_floor,
    measure_noise_floor,
    min_discordant_rows,
    noise_floor_mde,
    pareto_front,
    record_noise_floor,
    render_cost_quality,
    render_markdown,
    render_row_matrix,
    resolve_model,
)
from coder_eval.reports_stats import BOOTSTRAP_RESAMPLES, DEFAULT_ALPHA, bootstrap_p_floor, holm_rejections


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


def _write_row(run_dir: Path, variant: str, row_id: str, result: EvaluationResult, replicate: int = 0) -> Path:
    task_dir = run_dir / variant / SUITE / row_id / f"{replicate:02d}"
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "task.json"
    path.write_text(result.model_dump_json(), encoding="utf-8")
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


def test_module_imports_no_cli_machinery() -> None:
    """A core library the skill drives from a snippet — not a command.

    CE004 does NOT cover this: its _CORE_DIRS regex matches only the models/criteria/... SUBDIRS,
    so a top-level module is out of scope, and it bans importing `coder_eval.cli` rather than
    typer/rich at all. Hence a real assertion here.
    """
    source = (Path(__file__).parent.parent / "src" / "coder_eval" / "optimize_gate.py").read_text(encoding="utf-8")
    for banned in ("import typer", "import rich", "from typer", "from rich", "coder_eval.cli"):
        assert banned not in source, f"optimize_gate.py imports {banned!r} — it is a library, not a CLI surface"


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


class TestPromotionIsNotOverstated:
    """Two ways the rendered block could claim more than the tool decided."""

    def _verdict(self, **overrides) -> ActivationGateVerdict:
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

    def test_an_interval_containing_zero_never_promotes(self) -> None:
        # Holm can reject at a corrected alpha while the reported interval still contains zero.
        # The method file states the rule as "the interval excludes zero", so the code must make
        # that literally true rather than approximately true.
        decided = holm_promote([self._verdict(ci_low=-0.05, ci_high=0.6)])[0]
        assert decided.promoted is False
        assert any("still contains zero" in note for note in decided.notes)

    def test_a_failed_guardrail_never_renders_as_promoted(self) -> None:
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
        decided = holm_promote([self._verdict(guardrails=[failing])])[0]
        # `promoted` stays the primary statistic's decision — the guardrails gate in the
        # procedure — but the headline must not invite the reader to ship it anyway.
        assert decided.promoted is True
        text = render_markdown(decided)
        assert "BLOCKED BY A GUARDRAIL" in text
        assert "cost (USD/row)" in text
        assert "Do not promote on this block" in text

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
        assert "**PROMOTED**" in text
        assert "BLOCKED" not in text


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
        for module in ("optimize_gate.py", "reports_stats.py"):
            source = (Path(__file__).parent.parent / "src" / "coder_eval" / module).read_text(encoding="utf-8")
            literals = [ln for ln in source.splitlines() if alpha_literal.search(ln) and "DEFAULT_ALPHA = " not in ln]
            assert not literals, f"{module} still spells 0.05 outside DEFAULT_ALPHA: {literals}"


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
        # be met at any R — and the caller must say "more rows AND more disagreement", not name a
        # count that does not work.
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
        assert "adding rows cannot change it" in decided.gate_refusal
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

    def test_execution_floor_names_too_few_replicated_rows(self, tmp_path: Path, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert _execution_floor(_weighted_arm(tmp_path, "incumbent", {"r0": [0.1, 0.2]})) is None
        assert "carry 2+ replicates" in caplog.text


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
