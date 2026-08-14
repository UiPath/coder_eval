"""Unit tests for the activation track's promotion gate (`coder_eval.optimize_gate`).

Fixtures write real ``task.json`` files into a ``tmp_path`` run-dir tree, so the loader is
exercised against the on-disk contract rather than against a mock of it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.models import (
    ActivationGateVerdict,
    ClassificationCriterionResult,
    CriterionResult,
    EvaluationResult,
    FinalStatus,
)
from coder_eval.optimize_gate import (
    _label_pairs,
    activation_gate,
    holm_promote,
    load_arm_rows,
    load_suite_rows,
    render_markdown,
)
from coder_eval.reports_stats import BOOTSTRAP_RESAMPLES


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


def _gate(run_dirs: list[Path], **kwargs) -> ActivationGateVerdict:
    return activation_gate(
        incumbent_run_dirs=run_dirs,
        candidate_run_dirs=run_dirs,
        incumbent_variant="incumbent",
        candidate_variant="candidate",
        suite_id=SUITE,
        **{"criterion_index": 0, **kwargs},
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
        assert verdict.n_resamples == BOOTSTRAP_RESAMPLES
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
        from coder_eval.models import GuardrailCheck

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
