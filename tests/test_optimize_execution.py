"""Unit tests for `coder_eval.optimize.execution` — does a candidate BODY produce better outcomes?

Per-row `weighted_score` through the reporter's paired comparison. Track-specific: the sign
resolution, the integrity checks, the replicate-split noise floor, dead weight, and Stage C.
"""

import ast
import inspect
import logging
import shutil
import textwrap
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from coder_eval.models import (
    ConfirmVerdict,
    CriterionResult,
    EvaluationResult,
    ExecutionGateVerdict,
    ExperimentResult,
    FinalStatus,
    GuardrailCheck,
    copy_with,
)
from coder_eval.optimize import execution as optimize_execution
from coder_eval.optimize.activation import measure_noise_floor
from coder_eval.optimize.execution import (
    _below_mde_findings,
    _dead_weight,
    _execution_diagnostics,
    _note_tight_interval,
    _note_unpriced_floor,
    _paired_row_ids,
    _primary_reading,
    _provenance_notes,
    _refuse_no_comparison,
    _refuse_stale_tree,
    _refuse_unusable_sample,
    _refuse_zero_variance,
    _signed_statistic,
    confirm_gate_execution,
    execution_gate,
    holm_promote_execution,
    measure_execution_noise_floor,
    resolve_model,
)
from coder_eval.optimize.gate import FLOOR_RESOLUTION, GATE_RESAMPLES, MATERIALITY_FLOOR
from coder_eval.optimize.store import record_noise_floor
from coder_eval.reports_optimize import render_execution_markdown
from coder_eval.reports_stats import PairedComparison
from tests.optimize_fixtures import (
    EXEC_SUITE,
    FAST_RESAMPLES,
    SUITE,
    WINNER,
    confirm_dir,
    eval_result,
    exec_gate,
    exec_run_dir,
    execution_floor,
    experiment_json,
    headline_line,
    module_source,
    scored_result,
    set_split,
    shifted_replicate_arms,
    uniform_shift,
    weighted_arm,
    write_row,
)


class TestResolveModel:
    def test_returns_the_single_model_used(self, tmp_path: Path) -> None:
        rows = {"r0": [eval_result("r0", [("yes", "yes")]).model_copy(update={"model_used": "claude-haiku-4-5"})]}
        assert resolve_model(rows) == "claude-haiku-4-5"

    def test_returns_none_when_rows_disagree(self, tmp_path: Path) -> None:
        rows = {
            "r0": [eval_result("r0", [("yes", "yes")]).model_copy(update={"model_used": "claude-haiku-4-5"})],
            "r1": [eval_result("r1", [("yes", "yes")]).model_copy(update={"model_used": "claude-sonnet-5"})],
        }
        assert resolve_model(rows) is None

    def test_returns_none_when_unset(self) -> None:
        assert resolve_model({"r0": [eval_result("r0", [("yes", "yes")])]}) is None


_MDE_ADVISORY_FRAGMENTS = (
    "minimum detectable effect",
    "could not be priced",
    "tighter than this suite's own noise floor",
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
        floor = execution_floor(weighted_arm(tmp_path, "incumbent", self._spread()))
        assert floor is not None and floor.mde > 0.0

        single = weighted_arm(tmp_path / "b", "incumbent", {f"r{i}": [0.5] for i in range(8)})
        assert execution_floor(single) is None

    def test_pools_replicates_across_run_dirs(self, tmp_path: Path) -> None:
        # The split axis is replicates, but they may arrive from several run directories — one
        # `--repeats 3` invocation or three separate ones both give a row 3 replicates.
        spread = weighted_arm(tmp_path, "incumbent", self._spread(), run_dirs=3)
        assert execution_floor(spread) is not None

    def test_is_none_without_enough_replicated_rows(self, tmp_path: Path) -> None:
        one_row = {"r0": [0.2, 0.9, 0.4], "r1": [0.5]}  # only r0 qualifies
        assert execution_floor(weighted_arm(tmp_path, "incumbent", one_row)) is None

    def test_is_none_when_weighted_score_is_unset(self, tmp_path: Path, caplog) -> None:
        # A result with criteria but no weighted_score yields None from row_score, so every
        # cluster is empty. It must return None rather than a confident 0.0.
        run_dir = tmp_path / "run-0"
        for i in range(8):
            for replicate in range(3):
                write_row(run_dir, "incumbent", f"r{i}", eval_result(f"r{i}", [("yes", "yes")]), replicate)
        with caplog.at_level(logging.WARNING):
            assert execution_floor([run_dir]) is None
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
        floor = execution_floor(weighted_arm(tmp_path, "incumbent", rows))
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
                result = eval_result(f"r{i}", [("yes", "yes")]).model_copy(update={"weighted_score": score})
                write_row(run_dir, "incumbent", f"r{i}", result, replicate)

        f1_floor = measure_noise_floor(
            run_dirs=[run_dir, run_dir],
            variant_id="incumbent",
            suite_id=SUITE,
            criterion_index=0,
            model="claude-haiku-4-5",
            n_resamples=FAST_RESAMPLES,
        )
        assert f1_floor is not None and f1_floor.mde == 0.0, "the F1 floor is the meaningless 0.000 N2 names"

        execution = execution_floor([run_dir])
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

        floor_3 = execution_floor(weighted_arm(tmp_path / "a", "incumbent", three))
        floor_2 = execution_floor(weighted_arm(tmp_path / "b", "incumbent", two))
        assert floor_3 is not None and floor_2 is not None
        assert (floor_3.n_replicates, floor_2.n_replicates) == (3, 2)
        assert floor_3.mde != floor_2.mde, "the fixture no longer distinguishes replicate counts"

        sidecar = tmp_path / ".optimize-skill" / "my-skill" / "measurements.json"
        record_noise_floor(sidecar, floor_3)
        measurements = record_noise_floor(sidecar, floor_2)
        assert len(measurements.noise_floors) == 2, "the 2-replicate floor REPLACED the 3-replicate one"

        # And the round that actually ran at --repeats 3 gets its own number back.
        reused = execution_floor(weighted_arm(tmp_path / "c", "incumbent", three), measurements=measurements)
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
        floor = execution_floor(weighted_arm(tmp_path, "incumbent", uneven))
        assert floor is not None
        assert floor.mde == 0.0, "an unbalanced row leaked between-row spread into the null"
        assert floor.n_replicates == 2, "balancing trims to the smallest qualifying row"

    def test_a_mistyped_path_says_so_rather_than_blaming_repeats(self, tmp_path: Path, caplog) -> None:
        # "no row carries 2+ replicates" would send the reader off to check --repeats when the real
        # cause is a wrong variant, suite or run directory.
        with caplog.at_level(logging.WARNING):
            assert execution_floor([tmp_path / "typo"]) is None
        assert "wrong variant id, a wrong suite id or a wrong run directory" in caplog.text
        assert "--repeats" not in caplog.text

    def test_records_its_metric(self, tmp_path: Path) -> None:
        floor = execution_floor(weighted_arm(tmp_path, "incumbent", self._spread()))
        assert floor is not None
        assert floor.metric == "weighted_score"
        assert floor.criterion_index is None

    def test_defaults_to_the_gate_resample_count(self) -> None:
        import inspect

        assert inspect.signature(measure_execution_noise_floor).parameters["n_resamples"].default == GATE_RESAMPLES


class TestExecutionGateSign:
    """The single most important assertion in this phase: the tool resolves the subtraction."""

    def test_the_candidate_wins_positively_whichever_arm_is_declared_first(self, tmp_path: Path) -> None:
        first = exec_gate(exec_run_dir(tmp_path / "a", **WINNER, declare_incumbent_first=True))
        second = exec_gate(exec_run_dir(tmp_path / "b", **WINNER, declare_incumbent_first=False))
        assert first.mean_diff is not None and first.mean_diff > 0.0
        assert second.mean_diff == pytest.approx(first.mean_diff)
        assert second.p_value == pytest.approx(first.p_value)

    def test_the_interval_stays_ordered_under_both_declaration_orders(self, tmp_path: Path) -> None:
        # Negating an interval reverses it; without the re-order a promoted candidate reports a
        # "low" above its "high".
        for i, order in enumerate((True, False)):
            verdict = exec_gate(exec_run_dir(tmp_path / f"o{i}", **WINNER, declare_incumbent_first=order))
            assert verdict.ci_low is not None and verdict.ci_high is not None
            assert verdict.ci_low <= verdict.ci_high

    def test_the_effect_size_carries_the_sign_too(self, tmp_path: Path) -> None:
        first = exec_gate(exec_run_dir(tmp_path / "a", **WINNER, declare_incumbent_first=True))
        second = exec_gate(exec_run_dir(tmp_path / "b", **WINNER, declare_incumbent_first=False))
        assert first.effect_size is not None and first.effect_size > 0.0
        assert second.effect_size == pytest.approx(first.effect_size)

    def test_a_losing_candidate_reads_negative(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, incumbent=WINNER["candidate"], candidate=WINNER["incumbent"])
        verdict = exec_gate(run_dir)
        assert verdict.mean_diff is not None and verdict.mean_diff < 0.0


class TestExecutionGateLoading:
    def test_narrows_to_the_target_suite(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(
            tmp_path,
            **WINNER,
            extra_scores={
                "incumbent": {"other-suite/r1": [0.1], "other-suite/r2": [0.1]},
                "candidate": {"other-suite/r1": [0.9], "other-suite/r2": [0.9]},
            },
        )
        assert exec_gate(run_dir).rows_paired == 4

    def test_a_missing_experiment_file_is_refused_not_raised(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        (run_dir / "experiment.json").unlink()
        verdict = exec_gate(run_dir)
        assert (verdict.mean_diff, verdict.ci_low, verdict.p_value) == (None, None, None)
        assert verdict.gate_refusal is not None
        assert "experiment.json" in verdict.gate_refusal and "-e" in verdict.gate_refusal
        assert headline_line(render_execution_markdown(holm_promote_execution([verdict])[0])).startswith("NOT A RESULT")

    def test_a_malformed_experiment_file_is_noted_not_raised(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        (run_dir / "experiment.json").write_text("{not json", encoding="utf-8")
        verdict = exec_gate(run_dir)
        assert verdict.p_value is None
        assert verdict.gate_refusal is not None and "could not be read or parsed" in verdict.gate_refusal

    def test_an_unreadable_experiment_file_is_noted_not_raised(self, tmp_path: Path) -> None:
        # The docstring promises "Never an exception". `except ValueError` did not cover a
        # permission error or a file that vanished between the is_file() and the read.
        run_dir = exec_run_dir(tmp_path, **WINNER)
        real_read_text = Path.read_text

        def _raise_on_the_experiment_file(self: Path, *args, **kwargs) -> str:
            if self.name == "experiment.json":
                raise OSError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        # Patched rather than `chmod 000`, which is a no-op as root and in many CI containers.
        with mock.patch.object(Path, "read_text", _raise_on_the_experiment_file):
            verdict = exec_gate(run_dir)
        assert verdict.p_value is None and verdict.mean_diff is None
        assert verdict.gate_refusal is not None and "could not be read or parsed" in verdict.gate_refusal

    def test_a_three_variant_experiment_names_the_exactly_two_precondition(self, tmp_path: Path) -> None:
        # The triage file re-passed at Stage B: the mistake reaching the gate.
        run_dir = exec_run_dir(tmp_path, **WINNER, variant_ids=["incumbent", "candidate", "cand-b"])
        verdict = exec_gate(run_dir)
        assert verdict.p_value is None
        assert verdict.gate_refusal is not None
        assert "EXACTLY two" in verdict.gate_refusal and "round<N>-gate.yaml" in verdict.gate_refusal

    def test_a_variant_the_experiment_does_not_carry_names_both_actual_ids(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="typo-arm",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
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
        run_dir = exec_run_dir(
            tmp_path,
            **WINNER,
            extra_scores={"inc-A": {f"{EXEC_SUITE}/{r}": s for r, s in WINNER["incumbent"].items()}},
            variant_ids=["inc-A", "candidate"],
        )
        verdict = exec_gate(run_dir)
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
        run_dir = exec_run_dir(tmp_path, **WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbnet",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert (verdict.mean_diff, verdict.ci_low, verdict.ci_high) == (None, None, None)
        assert (verdict.p_value, verdict.effect_size) == (None, None)
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is not True
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT — ")

    def test_fewer_than_two_paired_rows_is_refused_with_the_count_still_carried(self, tmp_path: Path) -> None:
        # No interval can be computed at all, so there is nothing for a reader to weigh — rendering
        # it as NOT PROMOTED says the candidate lost a comparison that never happened. The COUNTS
        # stay on the verdict either way, which is what distinguishes this from a wiring fault: a
        # reader can see `paired 1` rather than having it flattened into the message.
        run_dir = exec_run_dir(tmp_path, incumbent={"r1": [0.2, 0.3]}, candidate={"r1": [0.8, 0.9]})
        verdict = exec_gate(run_dir)
        assert verdict.rows_paired == 1
        assert verdict.p_value is None
        assert verdict.gate_refusal is not None
        assert "fewer than the 2 a paired interval needs" in verdict.gate_refusal

    def test_an_unpairable_row_is_carried_as_excluded(self, tmp_path: Path) -> None:
        incumbent = {**WINNER["incumbent"], "r5": [0.4]}
        run_dir = exec_run_dir(tmp_path, incumbent=incumbent, candidate=WINNER["candidate"])
        verdict = exec_gate(run_dir)
        assert (verdict.rows_paired, verdict.rows_excluded) == (4, 1)


class TestExecutionGateIntegrity:
    def test_engagement_below_one_fails_and_names_the_drop(self, tmp_path: Path) -> None:
        # `scored_result` writes observed="no" below 0.5, which is a recall.yes miss.
        candidate = {**WINNER["candidate"], "r3": [0.6, 0.2]}
        run_dir = exec_run_dir(tmp_path, incumbent=WINNER["incumbent"], candidate=candidate)
        verdict = exec_gate(run_dir)
        engagement = next(c for c in verdict.integrity_checks if "engagement" in c.name)
        assert engagement.candidate is not None and engagement.candidate < 1.0
        assert not engagement.passed

    def test_engagement_at_one_on_both_arms_passes(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(
            tmp_path,
            incumbent={f"r{i}": [0.6, 0.7] for i in range(4)},
            candidate={f"r{i}": [0.8, 0.9] for i in range(4)},
        )
        engagement = next(c for c in exec_gate(run_dir).integrity_checks if "engagement" in c.name)
        assert engagement.passed and engagement.candidate == 1.0

    def test_a_non_classification_index_is_unevaluated_not_a_pass_on_the_merits(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        verdict = exec_gate(run_dir, engagement_criterion_index=7)
        engagement = next(c for c in verdict.integrity_checks if "engagement" in c.name)
        assert engagement.note is not None and "NOT evaluated" in engagement.note
        assert "criterion_aggregates" in engagement.note
        assert (engagement.incumbent, engagement.candidate) == (None, None)

    def test_none_skips_engagement_and_leaves_only_completion(self, tmp_path: Path) -> None:
        verdict = exec_gate(exec_run_dir(tmp_path, **WINNER), engagement_criterion_index=None)
        assert [c.name for c in verdict.integrity_checks] == ["completion_rate"]

    def test_a_lower_completion_rate_on_the_candidate_fails(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        # An errored replicate: the row directory exists, but nothing scored.
        hollow = scored_result("r2", 0.9).model_copy(update={"success_criteria_results": []})
        write_row(run_dir, "candidate", "r2", hollow, 1)
        completion = next(c for c in exec_gate(run_dir).integrity_checks if c.name == "completion_rate")
        assert not completion.passed
        assert completion.candidate is not None and completion.incumbent is not None
        assert completion.candidate < completion.incumbent

    def test_equal_completion_passes(self, tmp_path: Path) -> None:
        completion = next(
            c for c in exec_gate(exec_run_dir(tmp_path, **WINNER)).integrity_checks if c.name == "completion_rate"
        )
        assert completion.passed and completion.candidate == completion.incumbent == 1.0

    def test_the_gate_reads_no_suite_json(self, tmp_path: Path) -> None:
        # The positional read of `criterion_aggregates` the planning spike falsified: that list is
        # FILTERED, so position i there is not criterion i. Nothing here may depend on it.
        from coder_eval.optimize import execution as gate

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
        run_dir = exec_run_dir(
            tmp_path,
            incumbent={f"r{i}": [0.4, 0.4] for i in range(4)},
            candidate={f"r{i}": [0.9, 0.9] for i in range(4)},
        )
        assert exec_gate(run_dir).mde == 0.0

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
        verdict = exec_gate(exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))
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
        verdict = exec_gate(exec_run_dir(tmp_path, incumbent=rows, candidate=candidate))
        assert verdict.mde == 0.0, "fixture drifted — this test is about an unmeasurable floor"
        assert verdict.gate_refusal is None
        assert any("NOT checked against a noise floor" in note for note in verdict.notes)

    def test_a_measurable_floor_says_nothing_about_being_unmeasurable(self, tmp_path: Path) -> None:
        # The anti-over-fire half: the note must not print on a suite that DID price its floor.
        incumbent = {"r0": [0.1, 0.9], "r1": [0.3, 0.5], "r2": [0.0, 0.95], "r3": [0.45, 0.55], "r4": [0.2, 0.8]}
        candidate = {row: [round(v + 0.02, 3) for v in values] for row, values in incumbent.items()}
        verdict = exec_gate(exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))
        assert verdict.mde is not None and verdict.mde > 0.0
        assert not any("NOT checked against a noise floor" in note for note in verdict.notes)

    def test_a_difference_above_a_measurable_mde_is_not_refused(self, tmp_path: Path) -> None:
        # The anti-over-fire half. `WINNER` cannot witness this: its floor is 2.8e-17, so
        # "difference above the floor" is satisfied by any non-zero win at all and the assertion
        # would pass on a 1e-9 one. This fixture prices a real floor and clears it by a margin.
        # Every shifted CANDIDATE value must land in [0.5, 1.0], and the fixture asserts it rather
        # than trusting the arithmetic. Above 1.0 the score fails EvaluationResult validation,
        # `load_suite_rows` logs and SKIPS that task.json, and the arm trips the completion_rate
        # integrity check — `r2`'s 0.55 shifted to 1.01 and did exactly that, a replicate silently
        # missing here for the fixture's whole life. Below 0.5 `scored_result` labels the row
        # `no`, so the arm did not engage the skill on it and the engagement check trips — `r2`'s
        # 0.0 shifted to 0.46 and did THAT. Both were invisible while a failed check was advisory;
        # both block the promotion now, which is the point of the change this fixture now backs.
        incumbent = {"r0": [0.1, 0.5], "r1": [0.2, 0.3], "r2": [0.05, 0.5], "r3": [0.25, 0.35], "r4": [0.15, 0.45]}
        candidate = {
            row: [round(v + 0.42 + 0.02 * i, 3) for v in values] for i, (row, values) in enumerate(incumbent.items())
        }
        assert all(0.5 <= v <= 1.0 for values in candidate.values() for v in values), "fixture drifted out of range"
        decided = holm_promote_execution([exec_gate(exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))])[
            0
        ]
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
        verdict = exec_gate(exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))
        assert verdict.mde is not None and verdict.mean_diff is not None and verdict.ci_low is not None
        assert abs(verdict.mean_diff) < verdict.mde, "fixture drifted — it must be BELOW the floor"
        assert verdict.ci_low < 0.0 < (verdict.ci_high or 0.0), "and its interval must contain zero"
        assert verdict.gate_refusal is None, "below the floor AND consistent with zero is not a refusal"
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is False
        assert headline_line(render_execution_markdown(decided)) == "NOT PROMOTED"

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
        verdict = exec_gate(exec_run_dir(tmp_path, incumbent=incumbent, candidate=candidate))
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
        verdict = exec_gate(exec_run_dir(tmp_path, incumbent=rows, candidate=dict(rows)))
        assert verdict.mean_diff is not None and verdict.effect_size is None
        assert verdict.gate_refusal is not None and "zero variance" in verdict.gate_refusal
        # Subsumed, not printed beside it: one message per finding.
        assert not any("Cohen's d is undefined" in note for note in verdict.notes)


# The shipped outcome template's weights, READ from the template rather than retyped. The exact
# dead-weight share is 0.5121951219…, and hardcoding a rounded constant is what turns a measurement
# into a claim nobody checks — `0.512` belongs in the RENDER assertion, where it is what a reader
# sees, and nowhere else.
def _template_weights() -> list[float]:
    from coder_eval.orchestration.task_loader import load_task

    template = Path(__file__).parent.parent / "plugins" / "coder-eval" / "reference" / "templates" / "outcome.yaml"
    task, _ = load_task(template)
    weights = [criterion.weight for criterion in task.success_criteria]
    assert len(weights) == 3, f"the outcome template no longer ships three criteria: {weights}"
    return weights


def _weighted_result(row_id: str, scores: list[float], weights: list[float | None]) -> EvaluationResult:
    """One replicate carrying one criterion result per (score, weight) pair.

    ``weighted_score`` is computed from them rather than passed in, so the fixture cannot claim a
    blend its criterion results do not support — which is the whole thing the dead-weight reading is
    about.
    """
    assert len(scores) == len(weights)
    known = [w for w in weights if w is not None]
    total = sum(known)
    blended = (
        sum(score * (weight or 0.0) for score, weight in zip(scores, weights, strict=True)) / total if total else 0.0
    )
    return EvaluationResult(
        task_id=f"{SUITE}/{row_id}",
        task_description="row",
        agent_type="claude-code",
        started_at=datetime(2026, 8, 17, 12, 0, 0),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        weighted_score=blended,
        success_criteria_results=[
            CriterionResult(
                criterion_type="run_command",
                description=f"criterion {i} for row {row_id}",
                score=score,
                weight=weight,
            )
            for i, (score, weight) in enumerate(zip(scores, weights, strict=True))
        ],
    )


def _weighted_run_dir(
    tmp_path: Path,
    *,
    incumbent: dict[str, list[list[float]]],
    candidate: dict[str, list[list[float]]],
    weights: list[float | None],
    candidate_weights: list[float | None] | None = None,
) -> Path:
    """A Stage B gate dir whose rows carry PER-CRITERION scores and weights.

    ``incumbent``/``candidate`` map a row id to one list of per-criterion scores per replicate, so
    the replicate reduction the dead-weight computation shares with `paired_comparison` is exercised
    rather than assumed. ``candidate_weights`` overrides the candidate arm's list, which is how a
    tree whose two arms carry DIFFERENT criteria lists is built.
    """
    run_dir = tmp_path / "round1-gate"
    per_replicate: dict[str, dict[str, list[float]]] = {}
    arms = (("incumbent", incumbent, weights), ("candidate", candidate, candidate_weights or weights))
    for variant, per_row, arm_weights in arms:
        per_replicate[variant] = {}
        for row_id, replicates in per_row.items():
            blended: list[float] = []
            for replicate, scores in enumerate(replicates):
                result = _weighted_result(row_id, scores, arm_weights)
                write_row(run_dir, variant, row_id, result, replicate)
                blended.append(result.weighted_score)
            per_replicate[variant][f"{SUITE}/{row_id}"] = blended
    experiment_json(run_dir, ["incumbent", "candidate"], per_replicate)
    return run_dir


class TestDeadWeight:
    """The share of criterion weight that cannot move `weighted_score` — a READING, never a veto.

    `weighted_score` is the execution gate's primary statistic and a weighted mean over every
    criterion, so a criterion identical on both arms on every row contributes its whole weight to
    that mean's denominator and nothing to its difference. The shipped outcome template's engagement
    and `file_check` criteria both saturate by design, so an effect confined to the grader arrives at
    the gate attenuated — and nothing said so.
    """

    @staticmethod
    def _two_constant_one_varying() -> dict[str, dict[str, list[list[float]]]]:
        """Criteria 0 and 1 saturate on both arms; criterion 2 moves.

        The per-criterion SCORES only — the weights are the caller's, since `_weighted_run_dir` stamps
        them. An earlier signature took a `weights` argument and never read it.
        """
        rows = [f"r{i}" for i in range(4)]
        return {
            "incumbent": {rid: [[1.0, 1.0, 0.2], [1.0, 1.0, 0.3]] for rid in rows},
            "candidate": {rid: [[1.0, 1.0, 0.8], [1.0, 1.0, 0.9]] for rid in rows},
        }

    def test_the_shipped_template_configuration_reports_its_own_attenuation(self, tmp_path: Path) -> None:
        weights = _template_weights()
        arms = self._two_constant_one_varying()
        verdict = exec_gate(_weighted_run_dir(tmp_path, **arms, weights=weights))  # type: ignore[arg-type]

        dead = weights[0] + weights[1]
        assert verdict.dead_weight == pytest.approx(dead / sum(weights))

    def test_nothing_is_dead_when_every_criterion_varies(self, tmp_path: Path) -> None:
        weights = _template_weights()
        run_dir = _weighted_run_dir(
            tmp_path,
            incumbent={f"r{i}": [[0.1, 0.2, 0.3]] for i in range(4)},
            candidate={f"r{i}": [[0.4, 0.5, 0.6]] for i in range(4)},
            weights=weights,  # type: ignore[arg-type]
        )
        assert exec_gate(run_dir).dead_weight == 0.0

    def test_an_unrecorded_weight_is_unknown_and_never_zero(self, tmp_path: Path) -> None:
        """`None`, not 0.0 — "no dilution" and "we cannot tell" are the two states this separates.

        Any run predating `CriterionResult.weight` looks like this, which is every run on disk today.
        """
        run_dir = _weighted_run_dir(
            tmp_path,
            incumbent={f"r{i}": [[1.0, 0.2]] for i in range(4)},
            candidate={f"r{i}": [[1.0, 0.8]] for i in range(4)},
            weights=[None, None],
        )
        verdict = exec_gate(run_dir)
        assert verdict.dead_weight is None
        assert any("dead weight is UNKNOWN" in note for note in verdict.notes)
        # The rendered line names NO cause — there are four and only the note knows which.
        assert "- Dead weight: UNKNOWN — see notes for why it could not be computed" in render_execution_markdown(
            verdict
        )

    def test_arms_carrying_different_criteria_lists_refuse_rather_than_guess(self, tmp_path: Path) -> None:
        # One run_dir, one experiment, one suite — so this is a contaminated tree, and the
        # reconciliation refusal owns that diagnosis. The share is not guessed from the shorter list.
        run_dir = _weighted_run_dir(
            tmp_path,
            incumbent={f"r{i}": [[1.0, 0.2, 0.5]] for i in range(4)},
            candidate={f"r{i}": [[1.0, 0.8]] for i in range(4)},
            weights=[1.0, 1.0, 2.0],
            candidate_weights=[1.0, 1.0],
        )
        verdict = exec_gate(run_dir)
        assert verdict.dead_weight is None
        assert any("criteria lists" in note and "disagree" in note for note in verdict.notes)

    def test_a_criterion_no_row_scored_on_both_arms_leaves_both_halves(self) -> None:
        """Neither dead nor alive: with no paired evidence it leaves numerator AND denominator.

        Counting it as varying dilutes the share downward; counting it as dead inflates it — and an
        empty vector satisfies `all(... == 0.0)` vacuously, so without the guard it reads as dead.

        Reached through a RAGGED tree, which is what makes it a real state rather than a defensive
        one: row `a` carries no criterion results on the incumbent and row `c` none on the candidate,
        so the arms' first SCORING results are `b` and `a` respectively — both three criteria long,
        so the length check passes — while criterion 2 is scored by the candidate on no row that the
        incumbent also scored it on.
        """
        weights: list[float | None] = [1.0, 1.0, 2.0]
        incumbent = {
            "a": [_weighted_result("a", [], [])],
            "b": [_weighted_result("b", [1.0, 0.2, 0.5], weights)],
            "c": [_weighted_result("c", [1.0, 0.3, 0.5], weights)],
        }
        candidate = {
            "a": [_weighted_result("a", [1.0, 0.8, 0.5], weights)],
            "b": [_weighted_result("b", [1.0, 0.9], weights[:2])],
            "c": [_weighted_result("c", [], [])],
        }
        share, notes = _dead_weight(incumbent_rows=incumbent, candidate_rows=candidate, row_ids=["a", "b", "c"])
        # Only row `b` pairs, and only on criteria 0 and 1: criterion 0 is constant there and holds
        # 1 of the 2 weight the two usable criteria carry between them.
        assert share == pytest.approx(0.5)
        assert any("scored no row on both arms" in note for note in notes)

    def test_a_zero_total_weight_is_none_rather_than_a_zero_division(self, tmp_path: Path) -> None:
        run_dir = _weighted_run_dir(
            tmp_path,
            incumbent={f"r{i}": [[1.0, 0.2]] for i in range(4)},
            candidate={f"r{i}": [[1.0, 0.8]] for i in range(4)},
            weights=[0.0, 0.0],
        )
        verdict = exec_gate(run_dir)
        assert verdict.dead_weight is None
        assert any("zero total weight" in note for note in verdict.notes)

    def test_fewer_than_two_paired_rows_is_none_with_a_reason(self, tmp_path: Path) -> None:
        run_dir = _weighted_run_dir(
            tmp_path,
            incumbent={"r0": [[1.0, 0.2]]},
            candidate={"r0": [[1.0, 0.8]]},
            weights=[1.0, 1.0],
        )
        verdict = exec_gate(run_dir)
        assert verdict.dead_weight is None
        assert any("fewer than two rows paired" in note for note in verdict.notes)

    def test_the_note_names_the_dead_criteria_by_index_and_description(self, tmp_path: Path) -> None:
        weights = _template_weights()
        arms = self._two_constant_one_varying()
        verdict = exec_gate(_weighted_run_dir(tmp_path, **arms, weights=weights))  # type: ignore[arg-type]

        note = next(n for n in verdict.notes if "of the compared weight is dead" in n)
        assert "[0] 'criterion 0 for row r0'" in note and "[1] 'criterion 1 for row r0'" in note
        assert "[2]" not in note, "the varying criterion must not be named as dead"
        # The attenuation multiplier, so a reader can convert `mean_diff` back into the grader's unit.
        assert f"multiplied by {1.0 - (verdict.dead_weight or 0.0):.3f}" in note

    def test_the_reading_is_reported_on_a_refused_verdict_too(self, tmp_path: Path) -> None:
        # Every return path carries it: the computation happens beside the noise floor, before
        # `_verdict` exists, so a refusal does not silently drop the reading.
        weights = [1.0, 1.0]
        run_dir = _weighted_run_dir(
            tmp_path,
            incumbent={f"r{i}": [[1.0, 0.5]] for i in range(4)},
            candidate={f"r{i}": [[1.0, 0.7]] for i in range(4)},
            weights=weights,  # type: ignore[arg-type]
        )
        verdict = exec_gate(run_dir)
        assert verdict.gate_refusal is not None, "fixture drifted — this is the zero-variance refusal"
        assert verdict.dead_weight == pytest.approx(0.5)

    def test_a_tiny_but_real_paired_difference_is_alive_not_dead(self) -> None:
        """The comparison is `== 0.0` on the raw subtraction, with NO tolerance — pinned.

        Every other case here uses differences of 0.2-0.6 or exact zeros, so swapping the test for
        `abs(diff) < 1e-3` leaves them all green. A criterion that is genuinely constant produces
        exact zeros; a tolerance would silently reclassify a small real effect as dead, which is the
        one direction this reading must never fail in — it would report the effect as unmeasurable
        attenuation rather than as an effect.
        """
        weights: list[float | None] = [1.0, 1.0]
        rows = [f"r{i}" for i in range(4)]
        incumbent = {rid: [_weighted_result(rid, [0.5, 0.2], weights)] for rid in rows}
        # Criterion 0 differs by 1e-9 — nine orders below anything `weighted_score` renders, and
        # still not zero.
        candidate = {rid: [_weighted_result(rid, [0.5 + 1e-9, 0.8], weights)] for rid in rows}
        share, _notes = _dead_weight(incumbent_rows=incumbent, candidate_rows=candidate, row_ids=rows)
        assert share == 0.0, "a non-zero paired difference is alive, however small"

    def test_an_arm_with_no_criterion_results_at_all_is_none_with_a_reason(self) -> None:
        # Every replicate of every paired row errored on one arm, so there is no criteria list to
        # weigh. Distinct from a disagreement: nothing was read, rather than two things read wrong.
        weights: list[float | None] = [1.0, 1.0]
        rows = ["r0", "r1"]
        incumbent = {rid: [_weighted_result(rid, [1.0, 0.2], weights)] for rid in rows}
        candidate = {rid: [_weighted_result(rid, [], [])] for rid in rows}
        share, notes = _dead_weight(incumbent_rows=incumbent, candidate_rows=candidate, row_ids=rows)
        assert share is None
        assert any("no criterion results" in note for note in notes)

    def test_three_samples_that_diverge_are_named_on_the_block(self, tmp_path: Path) -> None:
        """The conversion a reader performs stops being exact, and the block has to say so.

        `mean_diff` comes from `experiment.json`, `primary_mean_diff` from the on-disk results over the
        paired rows, `dead_weight` from the on-disk results over the intersection. Ordinarily those are
        one sample. Here `experiment.json` scores a fifth row that has no `task.json` at all — the
        shape a crashed write or a skipped malformed row leaves — so the paired statistic is computed
        over five rows and both readings over four, and `primary x (1 - dead_weight)` no longer equals
        `mean_diff`. Each number is still correct over its own sample, which is why this is a NOTE.
        """
        weights = _template_weights()
        arms = self._two_constant_one_varying()
        run_dir = _weighted_run_dir(tmp_path, **arms, weights=weights)  # type: ignore[arg-type]
        # Add a row to experiment.json only — nothing on disk for it.
        experiment = run_dir / "experiment.json"
        raw = ExperimentResult.model_validate_json(experiment.read_text(encoding="utf-8"))
        scores = {v: dict(per) for v, per in raw.per_replicate_scores.items()}
        for variant, value in (("incumbent", [0.2]), ("candidate", [0.9])):
            scores[variant][f"{SUITE}/ghost"] = value
        experiment.write_text(copy_with(raw, per_replicate_scores=scores).model_dump_json(), encoding="utf-8")

        verdict = exec_gate(run_dir, primary_criterion_index=2)
        assert verdict.rows_paired == 5, "fixture drifted — experiment.json must pair the ghost row"
        note = next((n for n in verdict.notes if "DIFFERENT numbers of rows" in n), None)
        assert note is not None, "a block whose three magnitudes came from three samples must say so"
        # The primary's count is its USABLE rows (4), not the 5 ids experiment.json named — the ghost
        # row is in `check_row_ids` and contributes no difference, which IS the divergence.
        assert "5 row(s) from experiment.json" in note
        assert "the primary reading over 4 row(s)" in note
        assert "dead weight over 4 row(s)" in note
        # And the identity a reader would apply is indeed false here — which is the point of the note.
        assert verdict.mean_diff is not None and verdict.primary_mean_diff is not None
        assert verdict.mean_diff != pytest.approx(verdict.primary_mean_diff * (1.0 - (verdict.dead_weight or 0.0)))

    def test_the_ordinary_case_carries_no_divergence_note(self, tmp_path: Path) -> None:
        # The anti-over-fire half: the note must be absent when the three samples do coincide, or it
        # prints on every healthy block and stops being read.
        weights = _template_weights()
        arms = self._two_constant_one_varying()
        verdict = exec_gate(_weighted_run_dir(tmp_path, **arms, weights=weights), primary_criterion_index=2)  # type: ignore[arg-type]
        assert not any("DIFFERENT numbers of rows" in note for note in verdict.notes)
        assert verdict.mean_diff == pytest.approx(verdict.primary_mean_diff * (1.0 - (verdict.dead_weight or 0.0)))  # type: ignore[operator]

    def test_the_rendered_block_prints_the_reading(self, tmp_path: Path) -> None:
        """`0.512` appears in no other ASSERTION — it is the RENDERED value a reader sees.

        The share itself is 0.5121951219…, so the computation tests above assert
        `pytest.approx(1.05 / 2.05)` with the weights read from the shipped template. A rounded
        constant standing in for the arithmetic is a claim nobody is checking.

        Note what this fixture is and is not: it makes the template's `file_check` saturate as well as
        its engagement criterion, which is a property of a RUN in which every arm produced the
        artifact — not of the template, whose `file_check` is a graded outcome check with its own
        `mean` threshold. The template's BY-DESIGN dead weight is the engagement criterion alone,
        about 2.4%. The prose surfaces say so; an earlier draft of them called both criteria
        "saturating by design", which overstated it by 20x.
        """
        weights = _template_weights()
        arms = self._two_constant_one_varying()
        verdict = exec_gate(_weighted_run_dir(tmp_path, **arms, weights=weights))  # type: ignore[arg-type]

        block = render_execution_markdown(verdict)
        assert "- Dead weight: 51.2% of the compared weight (see notes)" in block
        assert "UNKNOWN" not in block

    def test_the_replicate_reduction_matches_the_primary_statistic(self, tmp_path: Path) -> None:
        """Replicates collapse by MEAN before pairing, so a criterion that differs only WITHIN a
        row's replicates is not dead — the same reduction `paired_comparison` applies.
        """
        weights = [1.0, 1.0]
        run_dir = _weighted_run_dir(
            tmp_path,
            incumbent={f"r{i}": [[0.0, 0.2], [1.0, 0.3]] for i in range(4)},
            # Criterion 0's per-replicate values differ but its per-row MEAN is identical (0.5), so
            # it is dead. A per-replicate comparison would call it varying.
            candidate={f"r{i}": [[1.0, 0.8], [0.0, 0.9]] for i in range(4)},
            weights=weights,  # type: ignore[arg-type]
        )
        assert exec_gate(run_dir).dead_weight == pytest.approx(0.5)


class TestExecutionGateRefusesAReusedRunDir:
    """The execution track reads the SAME append-only tree, and here contamination flips `promoted`.

    This track has no cross-split refusal — one `run_dir` holds both arms, so they share one split
    by construction — but a re-used `--run-dir` is fully representable, and since Phase 3 folded
    the integrity checks and guardrails into `promoted` a stale replicate does not merely get
    reported: it changes the answer.
    """

    def test_a_stale_replicate_flips_the_verdict_and_is_refused(self, tmp_path: Path) -> None:
        clean = holm_promote_execution([exec_gate(exec_run_dir(tmp_path / "clean", **WINNER))])[0]
        assert clean.promoted is True and clean.gate_refusal is None, "control drifted"

        dirty_dir = exec_run_dir(tmp_path / "dirty", **WINNER)
        for row in ("r1", "r2", "r3", "r4"):
            write_row(dirty_dir, "incumbent", row, scored_result(row, 0.0), 7, record=False)
        dirty = holm_promote_execution([exec_gate(dirty_dir)])[0]

        # Same winning candidate; without the preflight this reported promoted=False on a
        # completion_rate the stale replicates invented, with no refusal and no note.
        assert dirty.gate_refusal is not None
        assert "r1/07" in dirty.gate_refusal and "fresh --run-dir" in dirty.gate_refusal
        assert dirty.promoted is False

    def test_a_contaminated_candidate_arm_is_refused_too(self, tmp_path: Path) -> None:
        # The error runs the other way when the CANDIDATE carries the stale rows, so both arms
        # are reconciled rather than just the incumbent.
        run_dir = exec_run_dir(tmp_path, **WINNER)
        write_row(run_dir, "candidate", "r1", scored_result("r1", 1.0), 7, record=False)
        verdict = exec_gate(run_dir)
        assert verdict.gate_refusal is not None and "candidate" in verdict.gate_refusal

    def test_a_clean_gate_run_dir_is_untouched(self, tmp_path: Path) -> None:
        verdict = exec_gate(exec_run_dir(tmp_path, **WINNER))
        assert verdict.gate_refusal is None

    def test_it_renders_as_not_a_result(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        write_row(run_dir, "incumbent", "r1", scored_result("r1", 0.0), 7, record=False)
        decided = holm_promote_execution([exec_gate(run_dir)])[0]
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT")

    def test_refused_already_is_reachable_as_true(self, tmp_path: Path) -> None:
        """The test whose absence let `_execution_diagnostics`'s docstring call two guards dead.

        That docstring said `refused_already` is "False at the only call site today". It is not:
        the tree-reconciliation cause records a cause and then `break`s rather than returning, so
        control reaches the diagnostics ladder with `gate_refusal` already set. A reader who
        believed the docstring would delete the two `not refused_already` guards as unreachable,
        and this contaminated run would immediately print the MDE and tighter-than-floor advisories
        under a `NOT A RESULT` headline.

        It witnesses ONE of the two advisories — the two branches are mutually exclusive on any
        single fixture (`could not be priced` needs `mde < FLOOR_RESOLUTION`, tighter-than-floor
        needs `mde >= FLOOR_RESOLUTION`), so no fixture can cover both. The other half is covered
        by `TestExecutionDiagnostics::test_refused_already_suppresses_both_advisory_notes`.
        """
        run_dir = exec_run_dir(tmp_path, **WINNER)
        write_row(run_dir, "incumbent", "r1", scored_result("r1", 0.0), 7, record=False)

        seen: list[bool] = []
        real = _execution_diagnostics

        def _spy(**kwargs):
            seen.append(kwargs["refused_already"])
            return real(**kwargs)

        with mock.patch.object(optimize_execution, "_execution_diagnostics", _spy):
            decided = holm_promote_execution([exec_gate(run_dir)])[0]

        assert seen == [True], "the reconciliation cause must reach the ladder already refused"
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT")
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
        verdict = exec_gate(exec_run_dir(tmp_path, **uniform_shift(n_rows)))
        decided = holm_promote_execution([verdict])[0]
        assert decided.p_value == 0.0, "fixture drifted — the degenerate p is what makes this dangerous"
        assert decided.gate_refusal is not None
        assert decided.promoted is not True

    def test_the_gate_sets_it_before_holm_runs(self, tmp_path: Path) -> None:
        # Pins the setter's location: `execution_gate` already evaluates this predicate, so moving
        # the detection into `holm_promote_execution` would be a second declaration of it.
        verdict = exec_gate(exec_run_dir(tmp_path, **uniform_shift(4)))
        assert verdict.promoted is None
        assert verdict.gate_refusal is not None

    def test_the_message_names_the_row_count_and_the_constant_difference(self, tmp_path: Path) -> None:
        verdict = exec_gate(exec_run_dir(tmp_path, **uniform_shift(4, shift=0.3)))
        assert verdict.gate_refusal is not None
        assert "0.300" in verdict.gate_refusal and "4 paired row" in verdict.gate_refusal

    def test_identical_arms_get_their_own_message_and_remedy(self, tmp_path: Path) -> None:
        """The strongest form of the case, split out the way the activation track splits its own.

        `paired_t_test` returns 1.0 — not 0.0 — for a constant difference of exactly zero, so one
        message covering both shapes would state a p the block four lines below it contradicts. And
        the remedy differs: identical arms are a finding about the CANDIDATE (a wrong `plugins:`
        path gives exactly this shape), so "add rows the arms disagree on" is the wrong advice.
        """
        verdict = exec_gate(exec_run_dir(tmp_path, **uniform_shift(4, shift=0.0)))
        assert verdict.mean_diff == 0.0 and verdict.p_value == 1.0
        assert verdict.gate_refusal is not None
        assert "p = 1.0000" in verdict.gate_refusal
        assert "identical per-row score" in verdict.gate_refusal
        assert "adding rows cannot change it" in verdict.gate_refusal
        # And it must not carry the other shape's claim or its remedy.
        assert "p = 0.0000" not in verdict.gate_refusal
        assert "do NOT agree on" not in verdict.gate_refusal

    def test_a_healthy_sample_is_not_refused(self, tmp_path: Path) -> None:
        # The anti-over-fire test. `WINNER` has within-row spread, so the paired t has variance.
        decided = holm_promote_execution([exec_gate(exec_run_dir(tmp_path, **WINNER))])[0]
        assert decided.gate_refusal is None
        assert decided.promoted is True

    def test_zero_variance_favouring_the_incumbent_carries_no_negative_result_note(self, tmp_path: Path) -> None:
        # `promoted` was already False here; what changes is the headline — and an unguarded note
        # would print an ordinary negative result directly under a refusal.
        decided = holm_promote_execution([exec_gate(exec_run_dir(tmp_path, **uniform_shift(4, shift=-0.3)))])[0]
        assert decided.mean_diff is not None and decided.mean_diff < 0.0
        assert decided.gate_refusal is not None and decided.promoted is False
        assert not any("favours the incumbent" in note for note in decided.notes)


def _train_verdict(tmp_path: Path, **kwargs) -> ExecutionGateVerdict:
    return holm_promote_execution([exec_gate(confirm_dir(tmp_path, split="train"), **kwargs)])[0]


def _confirm(tmp_path: Path, train: ExecutionGateVerdict, *, split: str | None = "test", **overrides) -> ConfirmVerdict:
    return confirm_gate_execution(
        train_verdict=train,
        confirm_run_dir=confirm_dir(tmp_path, split=split),
        incumbent_variant="incumbent",
        candidate_variant="candidate",
        suite_id=EXEC_SUITE,
        n_resamples=FAST_RESAMPLES,
        **overrides,
    )


class TestConfirmGateExecution:
    """Stage C had no computed verdict — it was prose telling the reader to eyeball two intervals."""

    def test_a_deterministic_confirm_is_undecided_and_still_reports_its_numbers(self, tmp_path: Path) -> None:
        train = _train_verdict(tmp_path / "train")
        confirm = _confirm(tmp_path / "test", train)
        # `WINNER`'s replicates agree within each row, so its null split returns RESIDUE rather than
        # a floor — which is why the outcome is UNDECIDED. Asserted directly rather than worked
        # around: it is what a real confirm over a deterministic suite produces, and `== 0.0` would
        # not have seen it.
        assert confirm.test_mde is not None and confirm.test_mde < FLOOR_RESOLUTION
        assert confirm.outcome == "undecided"
        assert confirm.train_effect is not None and confirm.test_effect is not None
        assert confirm.delta == pytest.approx(confirm.test_effect - confirm.train_effect)

    def test_the_train_effect_is_read_off_the_train_verdict(self, tmp_path: Path) -> None:
        # Never recomputed: the two numbers this block compares must not be able to disagree with the
        # blocks they were each reported in.
        train = _train_verdict(tmp_path / "train")
        assert _confirm(tmp_path / "test", train).train_effect == train.mean_diff

    def test_a_confirm_run_recorded_under_split_train_is_refused(self, tmp_path: Path) -> None:
        """The failure the skill already warns about in prose, at full price with no error anywhere."""
        train = _train_verdict(tmp_path / "train")
        confirm = _confirm(tmp_path / "test", train, split="train")
        assert confirm.confirm_refusal is not None
        assert "--split 'train'" in confirm.confirm_refusal
        assert "reproduces by construction" in confirm.confirm_refusal
        assert confirm.outcome == "undecided"

    def test_a_full_suite_confirm_is_refused_too(self, tmp_path: Path) -> None:
        # A recorded `null` split means no `--split` was passed, so the confirm scored the rows the
        # candidate was proposed against as well.
        train = _train_verdict(tmp_path / "train")
        confirm = _confirm(tmp_path / "test", train, split=None)
        assert confirm.confirm_refusal is not None and "held-out split" in confirm.confirm_refusal

    def test_an_unrecorded_split_is_a_note_not_a_refusal(self, tmp_path: Path) -> None:
        """A run predating the provenance field is an expected input, not a wiring fault."""
        train = _train_verdict(tmp_path / "train")
        run_dir = exec_run_dir(tmp_path / "test", **WINNER)
        (run_dir / "run.json").unlink()
        confirm = confirm_gate_execution(
            train_verdict=train,
            confirm_run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert confirm.confirm_refusal is None
        assert any("provenance is missing" in note for note in confirm.notes)

    def test_a_refused_train_verdict_is_undecided_and_names_the_train_cause(self, tmp_path: Path) -> None:
        refused = holm_promote_execution(
            [exec_gate(confirm_dir(tmp_path / "train", split="train", **uniform_shift(4)))]
        )[0]
        assert refused.gate_refusal is not None, "fixture drifted — the train verdict must refuse"
        confirm = _confirm(tmp_path / "test", refused)
        assert confirm.outcome == "undecided"
        assert confirm.confirm_refusal is not None and "TRAIN verdict is not a result" in confirm.confirm_refusal

    def test_naming_more_than_one_candidate_raises(self, tmp_path: Path) -> None:
        # Confirming a shortlist spends the held-out split on SELECTION.
        train = _train_verdict(tmp_path / "train")
        with pytest.raises(TypeError, match="ONE variant id"):
            confirm_gate_execution(
                train_verdict=train,
                confirm_run_dir=confirm_dir(tmp_path / "test", split="test"),
                incumbent_variant="incumbent",
                candidate_variant=["candidate", "cand-b"],  # type: ignore[arg-type]
                suite_id=EXEC_SUITE,
            )

    def test_a_train_verdict_that_did_not_promote_is_noted(self, tmp_path: Path) -> None:
        """Stage C confirms the Stage B WINNER, and confirming anything else is worth saying.

        A note rather than a refusal: a reader may legitimately want to confirm a candidate that
        separated and was then vetoed by a guardrail. What it prevents is the resulting UNDECIDED —
        `classify_confirm` will not classify a train effect that is not a win — reading as a tooling
        failure rather than as the wrong verdict having been passed.
        """
        # A candidate that SEPARATED and was then vetoed — the legitimate reason to reach here. These
        # rows' labels derive from their scores, so the incumbent's low rows read `no` and the
        # engagement integrity check fails, which is exactly that shape.
        train = holm_promote_execution(
            [exec_gate(confirm_dir(tmp_path / "train", split="train", **shifted_replicate_arms(0.30)))]
        )[0]
        assert (train.separated, train.promoted) == (True, False), "fixture drifted — need a vetoed winner"
        confirm = _confirm(tmp_path / "test", train)
        assert confirm.confirm_refusal is None
        assert any("not True — Stage C confirms the Stage B WINNER" in note for note in confirm.notes)

    def test_it_measures_no_floor_of_its_own(self, tmp_path: Path) -> None:
        # `execution_gate` already measures the replicate floor unconditionally, so `test_mde` is
        # simply `test_verdict.mde`. A second estimator would double every confirm's bootstrap cost.
        train = _train_verdict(tmp_path / "train")
        confirm = _confirm(tmp_path / "test", train)
        assert confirm.test_mde == confirm.test_verdict.mde
        source = module_source("optimize.execution")
        body = source[source.index("def confirm_gate_execution(") : source.index("def holm_promote_execution(")]
        assert "measure_execution_noise_floor" not in body

    def test_the_carried_block_is_decided_rather_than_undecided(self, tmp_path: Path) -> None:
        # Holm at m=1: there is no multiplicity at Stage C, and applying it is what stops the carried
        # block rendering as UNDECIDED — which would read as "the gate never ran".
        train = _train_verdict(tmp_path / "train")
        assert _confirm(tmp_path / "test", train).test_verdict.promoted is not None


class TestConfirmGateOutcomesEndToEnd:
    """All four outcomes reachable through the real gate, with a measurable confirm floor.

    The `WINNER` fixture's replicates agree within each row, so its null split reduces to a floor of
    exactly 0.000 and every confirm over it is UNDECIDED — correctly. These fixtures give the confirm
    run a MEASURABLE floor by varying the replicate spread per row, which is what a real
    `--repeats 3` confirm produces.
    """

    def _confirm_with(
        self, tmp_path: Path, *, train_shift: float, test_shift: float, swap_test: bool = False
    ) -> ConfirmVerdict:
        train = holm_promote_execution(
            [exec_gate(confirm_dir(tmp_path / "train", split="train", **shifted_replicate_arms(train_shift)))]
        )[0]
        return confirm_gate_execution(
            train_verdict=train,
            confirm_run_dir=confirm_dir(
                tmp_path / "test", split="test", **shifted_replicate_arms(test_shift, swap=swap_test)
            ),
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )

    def test_a_sign_flip_is_reversed(self, tmp_path: Path) -> None:
        confirm = self._confirm_with(tmp_path, train_shift=0.30, test_shift=0.30, swap_test=True)
        assert confirm.outcome == "reversed"
        assert confirm.test_effect is not None and confirm.test_effect < 0.0

    def test_the_same_effect_reproduces(self, tmp_path: Path) -> None:
        confirm = self._confirm_with(tmp_path, train_shift=0.30, test_shift=0.30)
        assert confirm.outcome == "reproduced"

    def test_a_much_smaller_effect_shrank(self, tmp_path: Path) -> None:
        """A test effect ABOVE the confirm floor but far below the train one.

        The shifts are constrained from both sides, which is worth stating because the obvious
        fixture does not work: a test effect below the confirm split's own MDE makes the confirm GATE
        refuse (see the test below), so SHRANK needs a test effect above that floor whose shortfall
        from the train effect still exceeds it. Measured floor for these rows: 0.125.
        """
        confirm = self._confirm_with(tmp_path, train_shift=0.45, test_shift=0.20)
        assert confirm.confirm_refusal is None, "fixture drifted — the confirm gate must not refuse here"
        assert confirm.outcome == "shrank"

    def test_a_confirm_effect_below_the_confirm_floor_is_undecided(self, tmp_path: Path) -> None:
        # The confirm gate's own below-MDE refusal propagates: an effect the confirm split cannot
        # resolve is not a reproduction, and it is not a shrinkage either — it is not a measurement.
        confirm = self._confirm_with(tmp_path, train_shift=0.45, test_shift=0.02)
        assert confirm.confirm_refusal is not None and "confirm gate is not a result" in confirm.confirm_refusal
        assert confirm.outcome == "undecided"

    def test_the_confirm_floor_is_measurable_on_these_fixtures(self, tmp_path: Path) -> None:
        # The precondition every test above depends on — asserted, so a drifted fixture reports
        # itself rather than silently collapsing all three into UNDECIDED.
        confirm = self._confirm_with(tmp_path, train_shift=0.30, test_shift=0.30)
        assert confirm.test_mde is not None and confirm.test_mde > 0.0


class TestPrimaryCriterionIndex:
    """The PREDECLARED primary, as a reading. It never moves `promoted`."""

    @staticmethod
    def _dead_weight_arms() -> dict[str, dict[str, list[list[float]]]]:
        rows = [f"r{i}" for i in range(4)]
        return {
            "incumbent": {rid: [[1.0, 1.0, 0.2], [1.0, 1.0, 0.3]] for rid in rows},
            "candidate": {rid: [[1.0, 1.0, 0.8], [1.0, 1.0, 0.9]] for rid in rows},
        }

    def test_the_primary_effect_differs_from_the_blended_one_under_dead_weight(self, tmp_path: Path) -> None:
        """The whole point: the blended `mean_diff` is the primary effect times (1 - dead_weight).

        On the shipped template's weights, an effect confined to the grader arrives at the gate
        multiplied by 1/2.05 in a run where the `file_check` saturates too — and `primary_mean_diff`
        is what converts it back.
        """
        weights = _template_weights()
        run_dir = _weighted_run_dir(tmp_path, **self._dead_weight_arms(), weights=weights)  # type: ignore[arg-type]
        verdict = exec_gate(run_dir, primary_criterion_index=2)

        assert verdict.primary_mean_diff is not None and verdict.mean_diff is not None
        assert verdict.primary_mean_diff != pytest.approx(verdict.mean_diff)
        # The blended difference IS the primary one attenuated by the dead weight.
        assert verdict.mean_diff == pytest.approx(verdict.primary_mean_diff * (1.0 - (verdict.dead_weight or 0.0)))

    def test_setting_it_changes_no_decision(self, tmp_path: Path) -> None:
        weights = _template_weights()
        arms = self._dead_weight_arms()
        plain = holm_promote_execution([exec_gate(_weighted_run_dir(tmp_path / "a", **arms, weights=weights))])[0]  # type: ignore[arg-type]
        primary = holm_promote_execution(
            [exec_gate(_weighted_run_dir(tmp_path / "b", **arms, weights=weights), primary_criterion_index=2)]  # type: ignore[arg-type]
        )[0]
        assert (plain.promoted, plain.holm_rejected, plain.separated) == (
            primary.promoted,
            primary.holm_rejected,
            primary.separated,
        )
        assert plain.mean_diff == pytest.approx(primary.mean_diff)

    def test_an_over_range_index_is_refused_rather_than_reported_as_empty(self, tmp_path: Path) -> None:
        """`require_valid_criterion_index` bounds only BELOW, deliberately — and that is wrong here.

        An over-long index makes `row_score` return None on every row, so the vector is EMPTY and
        indistinguishable from a suite whose rows all errored on that criterion. Refused explicitly.
        """
        verdict = exec_gate(exec_run_dir(tmp_path, **WINNER), primary_criterion_index=7)
        assert verdict.primary_mean_diff is None
        assert verdict.gate_refusal is not None and "selected no usable row" in verdict.gate_refusal
        assert holm_promote_execution([verdict])[0].promoted is False

    def test_a_negative_index_raises_at_the_boundary(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="criterion_index must be >= 0"):
            exec_gate(exec_run_dir(tmp_path, **WINNER), primary_criterion_index=-1)

    def test_it_defaults_to_absent(self, tmp_path: Path) -> None:
        verdict = exec_gate(exec_run_dir(tmp_path, **WINNER))
        assert verdict.primary_criterion_index is None and verdict.primary_mean_diff is None

    def test_the_rendered_block_prints_it_only_when_predeclared(self, tmp_path: Path) -> None:
        """It has to be on the BLOCK, not only on the model — the skill tells the user to read it.

        And only when predeclared: a permanent `primary: —` line sends the reader looking for a number
        nobody asked for, and it would churn every pinned render for a field no fixture sets.
        """
        weights = _template_weights()
        arms = self._dead_weight_arms()
        plain = exec_gate(_weighted_run_dir(tmp_path / "a", **arms, weights=weights))  # type: ignore[arg-type]
        primary = exec_gate(
            _weighted_run_dir(tmp_path / "b", **arms, weights=weights),  # type: ignore[arg-type]
            primary_criterion_index=2,
        )
        assert "Predeclared primary" not in render_execution_markdown(plain)
        block = render_execution_markdown(primary)
        assert primary.primary_mean_diff is not None
        assert f"- Predeclared primary (criterion 2): {primary.primary_mean_diff:.3f}" in block
        assert "gates nothing" in block


class TestHolmPromoteExecution:
    def _verdict(self, p: float, **overrides) -> ExecutionGateVerdict:
        base = {
            "incumbent_variant": "incumbent",
            "candidate_variant": "cand",
            "suite_id": EXEC_SUITE,
            "confidence": 0.95,
            "n_resamples": FAST_RESAMPLES,
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
        assert headline_line(render_execution_markdown(decided)).startswith("BLOCKED BY A GUARDRAIL")


class TestExecutionDiagnostics:
    """`_execution_diagnostics` called directly, one test per finding plus the ordering rule."""

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
        rows = {"r1": [scored_result("r1", 1.0)]}
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

    def test_the_below_mde_negative_result_note_is_suppressed_under_a_refusal(self, tmp_path: Path) -> None:
        """The ONE note in this ladder that was not guarded, and it calls itself a negative result.

        Below the floor with an interval CONTAINING zero is the ordinary "it did not help" outcome,
        so the note says "this is an ordinary negative result and not a measurement problem" —
        which contradicts a `NOT A RESULT` headline directly above it. Reproduced through the real
        gate on a zero-variance refusal; `promoted` was unaffected, so it was prose only, on the
        page a user pastes into a promotion ledger.

        The existing suppression tests could not reach it: their contamination fixture also nulls
        the floor, so `mde is None` and this branch is skipped before the guard matters.
        """
        below_mde = {"mean_diff": 0.05, "bounds": [-0.2, 0.3], "mde": 0.5}
        # Unrefused, the note fires — otherwise the assertion below is vacuous.
        _refusal, notes = self._run(tmp_path, **below_mde)
        assert any("ordinary negative result" in n for n in notes), notes

        # Refused BY THE CALLER (the tree-reconciliation cause), and again by a cause found here.
        for refusing in ({"refused_already": True}, {"effect_size": None, "mean_diff": 0.0}):
            refusal, notes = self._run(tmp_path, **{**below_mde, **refusing})
            assert refusing.get("refused_already") or refusal is not None, refusing
            assert not any("ordinary negative result" in n for n in notes), (refusing, notes)

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
        """Two setters for one field is the state `FirstCause` collapsed.

        It RETURNS its first cause and lets the gate rank it, rather than writing the gate's own
        sink. A helper that wrote into the caller's sink could not be tested without building a gate
        around it, so this pins that it does neither: no `_verdict(` call, and no mention of the
        field name the gate assigns.

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
        # Anti-vacuity: the scan must still SEE the body it is checking. Keyed on this function's
        # OWN sink, which is a `FirstCause` of its own — it is module-level, not nested in
        # `execution_gate`, so there is no closure here to reach into in the first place.
        assert "cause.record(" in code
        assert "return (cause.reason, notes)" in code


class TestExecutionGateCannotBeQuietlyMisread:
    """Every way this gate could report a confident verdict about nothing."""

    def test_a_mistyped_suite_id_is_refused_and_the_message_names_the_suite(self, tmp_path: Path) -> None:
        # The statistic comes from experiment.json and every CHECK comes from the row tree, so a
        # mistyped variant/suite/run-dir leaves a perfectly good p beside four `— -> —` passes.
        # Measured before the refusal existed: headline PROMOTED, every check green.
        #
        # A wrong SUITE id also empties both arms, but the more specific cause fires first: no row
        # of that suite scored on both arms, and naming the suite is what the reader has to fix.
        run_dir = exec_run_dir(tmp_path, **WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id="a-suite-that-was-never-run",
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        # Pin WHICH cause: the zero-row message interpolates the suite id too, so asserting the id
        # alone would pass under either and the precedence claim above would go unwitnessed.
        assert "no paired comparison" in verdict.gate_refusal
        assert "a-suite-that-was-never-run" in verdict.gate_refusal
        assert "loaded ZERO rows" not in verdict.gate_refusal
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is not True
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT — ")

    def test_both_arms_empty_produce_exactly_one_refusal_naming_both(self, tmp_path: Path) -> None:
        # Every id correct and the experiment file valid — only the row tree is gone. That is the
        # case no other cause can see, and it is where the zero-row message earns its place. ONE
        # refusal naming both arms: the loop this replaced appended the same finding twice.
        run_dir = exec_run_dir(tmp_path, **WINNER)
        shutil.rmtree(run_dir / "incumbent")
        shutil.rmtree(run_dir / "candidate")
        verdict = exec_gate(run_dir)
        assert verdict.gate_refusal is not None and "loaded ZERO rows" in verdict.gate_refusal
        assert "the incumbent arm ('incumbent')" in verdict.gate_refusal
        assert "the candidate arm ('candidate')" in verdict.gate_refusal
        assert not any("loaded ZERO rows" in note for note in verdict.notes), "one message, not one per arm"
        assert headline_line(render_execution_markdown(holm_promote_execution([verdict])[0])).startswith("NOT A RESULT")

    def test_one_empty_arm_is_refused_where_the_variant_check_does_not_fire(self, tmp_path: Path) -> None:
        # A VALID incumbent id whose rows are simply not on disk (right id, wrong run dir). The
        # variant-mismatch return cannot see this — the experiment file names the arm perfectly
        # well — so the zero-row refusal is the only thing standing between it and PROMOTED.
        run_dir = exec_run_dir(tmp_path, **WINNER)
        shutil.rmtree(run_dir / "incumbent")
        verdict = exec_gate(run_dir)
        assert verdict.mean_diff is not None, "the statistic still computes — that is the whole hazard"
        assert verdict.gate_refusal is not None
        assert "the incumbent arm" in verdict.gate_refusal
        assert "the candidate arm" not in verdict.gate_refusal, "only the empty arm may be named"
        decided = holm_promote_execution([verdict])[0]
        assert decided.promoted is False
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT — ")

    def test_a_wiring_refusal_outranks_a_zero_variance_one(self, tmp_path: Path) -> None:
        # Both causes at once. If the rows never loaded, whether their differences vary is moot —
        # so the wiring message is what renders, and its remedy is the one the reader needs.
        run_dir = exec_run_dir(tmp_path, **uniform_shift(4))
        shutil.rmtree(run_dir / "incumbent")
        verdict = exec_gate(run_dir)
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
        run_dir = exec_run_dir(tmp_path, **WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="incumbent",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert (verdict.mean_diff, verdict.p_value, verdict.ci_low) == (None, None, None)
        assert verdict.gate_refusal is not None and "both 'incumbent'" in verdict.gate_refusal

    def test_a_row_that_vanished_from_one_arm_lowers_its_completion_rate(self, tmp_path: Path) -> None:
        # Computed over the paired intersection, this check reported 8/8 against 8/8 and PASSED
        # while two of the incumbent's rows were missing from the candidate entirely.
        incumbent = {**WINNER["incumbent"], "r5": [0.4, 0.5], "r6": [0.4, 0.5]}
        run_dir = exec_run_dir(tmp_path, incumbent=incumbent, candidate=WINNER["candidate"])
        verdict = exec_gate(run_dir)
        completion = next(c for c in verdict.integrity_checks if c.name == "completion_rate")
        assert completion.incumbent == 1.0
        assert completion.candidate is not None and completion.candidate < 1.0
        assert not completion.passed
        assert any("scored for one arm only" in note for note in verdict.notes)

    def test_the_checks_are_computed_over_the_rows_the_statistic_paired(self, tmp_path: Path) -> None:
        # A row on disk for both arms but carrying no score for one is IN the disk intersection and
        # OUT of the pairing, so the two sets differ — and a guardrail must guard its own sample.
        run_dir = exec_run_dir(tmp_path, **WINNER)
        raw = (run_dir / "experiment.json").read_text(encoding="utf-8")
        result = ExperimentResult.model_validate_json(raw)
        scores = {v: dict(per) for v, per in result.per_replicate_scores.items()}
        scores["candidate"][f"{EXEC_SUITE}/r4"] = []
        (run_dir / "experiment.json").write_text(
            result.model_copy(update={"per_replicate_scores": scores}).model_dump_json(), encoding="utf-8"
        )
        verdict = exec_gate(run_dir)
        assert verdict.rows_paired == 3
        assert verdict.rows_excluded == 1
        assert any("scored for one arm only" in note for note in verdict.notes)


class TestExecutionGateSplitNote:
    """The execution track NOTES its split and never refuses on it.

    That asymmetry with `activation_gate` is a consequence of the data sources: this track takes
    ONE run_dir holding BOTH variants, so the arms share one run.json and one split by
    construction, and a cross-split pair is unrepresentable here.
    """

    def test_the_note_names_the_recorded_split(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, incumbent=WINNER["incumbent"], candidate=WINNER["candidate"])
        set_split(run_dir, "test")
        verdict = exec_gate(run_dir)
        assert any("--split 'test'" in note for note in verdict.notes)
        assert verdict.gate_refusal is None

    def test_a_full_suite_run_says_nothing(self, tmp_path: Path) -> None:
        """A recorded `split: null` is the ordinary case; silence is the right output."""
        run_dir = exec_run_dir(tmp_path, incumbent=WINNER["incumbent"], candidate=WINNER["candidate"])
        verdict = exec_gate(run_dir)
        assert not any("--split" in note or "provenance" in note for note in verdict.notes)

    def test_an_unrecorded_run_dir_says_so(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, incumbent=WINNER["incumbent"], candidate=WINNER["candidate"])
        (run_dir / "run.json").unlink()
        verdict = exec_gate(run_dir)
        assert any("provenance is missing" in note for note in verdict.notes)
        assert verdict.gate_refusal is None, "provenance is never a refusal on this track"


class TestTheGateStagesAreCalledInPrecedenceOrder:
    """`execution_gate` is an orchestration function now, and the ORDER of its calls IS the rule.

    Each stage returns its message and the gate's `FirstCause` keeps the first, so nothing else in
    the tree declares the precedence. These are the unit tests for the stages plus one test per
    ADJACENT pair in the cascade — a later cause is usually an earlier one's consequence, so a
    reordering reports a symptom and sends the reader to the wrong remedy.
    """

    def test_the_same_variant_stage_refuses_only_on_a_match(self) -> None:
        assert _refuse_no_comparison("a", "b") is None
        refusal = _refuse_no_comparison("only-arm", "only-arm")
        assert refusal is not None
        assert "both 'only-arm'" in refusal and "no sign to resolve" in refusal

    def test_the_stale_tree_stage_names_the_first_contaminated_arm(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        assert _refuse_stale_tree(run_dir=run_dir, variants=("incumbent", "candidate"), suite_id=EXEC_SUITE) is None
        write_row(run_dir, "candidate", "ghost", scored_result("ghost", 1.0), record=False)
        refusal = _refuse_stale_tree(run_dir=run_dir, variants=("incumbent", "candidate"), suite_id=EXEC_SUITE)
        assert refusal is not None
        assert "run.json never wrote" in refusal and "candidate" in refusal
        # ONE fault, named once: the loop stops at the first arm rather than reporting both.
        assert refusal.count("run.json never wrote") == 1

    def test_the_same_variant_cause_outranks_the_stale_tree(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        write_row(run_dir, "incumbent", "ghost", scored_result("ghost", 1.0), record=False)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="incumbent",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        assert "no sign to resolve" in verdict.gate_refusal
        assert "run.json never wrote" not in verdict.gate_refusal

    def test_the_stale_tree_cause_outranks_the_missing_experiment_file(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        (run_dir / "experiment.json").unlink()
        write_row(run_dir, "incumbent", "ghost", scored_result("ghost", 1.0), record=False)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        assert "run.json never wrote" in verdict.gate_refusal
        assert "there is no experiment file" not in verdict.gate_refusal

    def test_the_experiment_cause_outranks_the_zero_row_cause(self, tmp_path: Path) -> None:
        """A mistyped variant id makes that arm load zero rows as a CONSEQUENCE.

        Refusing on the consequence would replace a message naming the two ids the experiment
        actually carries with one that can only say "a wrong variant id, suite id or run directory".
        """
        run_dir = exec_run_dir(tmp_path, **WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="typo",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        assert "is not one of the two variants" in verdict.gate_refusal
        assert "loaded ZERO rows" not in verdict.gate_refusal


class TestTheDiagnosticStages:
    """Each post-statistic stage, unit-tested for every cause it owns."""

    @staticmethod
    def _comparison(task_count: int = 4) -> PairedComparison:
        return PairedComparison(
            vid_a="candidate",
            vid_b="incumbent",
            task_count=task_count,
            excluded_count=0,
            mean_diff=0.2,
            ci_low=0.1,
            ci_high=0.3,
            effect_size=1.0,
            p_value=0.01,
        )

    def test_an_empty_arm_outranks_a_short_pairing(self, tmp_path: Path) -> None:
        """An arm that loaded nothing is WHY the pairing is short, so it must be named first."""
        refusal = _refuse_unusable_sample(
            incumbent_rows={},
            candidate_rows={},
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            run_dir=tmp_path,
            comparison=self._comparison(task_count=1),
        )
        assert refusal is not None
        assert "loaded ZERO rows" in refusal
        assert "fewer than the 2 a paired interval needs" not in refusal
        # ONE message naming BOTH empty arms, not one per arm.
        assert refusal.count("loaded ZERO rows") == 1
        assert "the incumbent arm ('incumbent') and the candidate arm ('candidate')" in refusal

    def test_a_short_pairing_refuses_once_both_arms_loaded(self, tmp_path: Path) -> None:
        rows = {"r1": [scored_result("r1", 1.0)]}
        refusal = _refuse_unusable_sample(
            incumbent_rows=rows,
            candidate_rows=rows,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            run_dir=tmp_path,
            comparison=self._comparison(task_count=1),
        )
        assert refusal is not None and "fewer than the 2 a paired interval needs" in refusal

    def test_a_healthy_sample_refuses_nothing_here(self, tmp_path: Path) -> None:
        rows = {"r1": [scored_result("r1", 1.0)]}
        assert (
            _refuse_unusable_sample(
                incumbent_rows=rows,
                candidate_rows=rows,
                incumbent_variant="incumbent",
                candidate_variant="candidate",
                suite_id=EXEC_SUITE,
                run_dir=tmp_path,
                comparison=self._comparison(),
            )
            is None
        )

    @pytest.mark.parametrize(
        ("mean_diff", "expected"),
        [
            # The split `holm_promote`'s discreteness refusal makes, for the same reason: at a
            # constant difference of ZERO the arms behaved identically, and the paired t reports
            # p = 1.0 there rather than the 0.0 a non-zero constant shift gives.
            (0.0, "identical per-row score"),
            (0.4, "differed by exactly 0.400"),
        ],
    )
    def test_zero_variance_splits_its_message_on_the_difference(self, mean_diff, expected) -> None:
        refusal = _refuse_zero_variance(mean_diff=mean_diff, effect_size=None, task_count=5)
        assert refusal is not None and expected in refusal

    def test_zero_variance_is_silent_when_d_is_defined_or_there_is_no_difference(self) -> None:
        assert _refuse_zero_variance(mean_diff=0.4, effect_size=1.2, task_count=5) is None
        assert _refuse_zero_variance(mean_diff=None, effect_size=None, task_count=5) is None

    def test_the_below_mde_continuum_refuses_only_when_the_interval_excludes_zero(self) -> None:
        """The conjunct that keeps the commonest honest outcome from becoming a refusal.

        Under the null a candidate's difference is small, so `abs(mean_diff) < mde` is true for
        nearly every candidate that simply does not work. An interval CONTAINING zero is the data
        agreeing it is null — an ordinary negative result, which stays one.
        """
        refusal, note = _below_mde_findings(mean_diff=0.01, mde=0.05, bounds=[0.005, 0.015])
        assert refusal is not None and "yet the interval excludes zero" in refusal
        assert note is None

        refusal, note = _below_mde_findings(mean_diff=0.01, mde=0.05, bounds=[-0.02, 0.04])
        assert refusal is None
        assert note is not None and "ordinary negative result" in note

    def test_the_below_mde_continuum_is_inert_without_a_usable_floor(self) -> None:
        assert _below_mde_findings(mean_diff=0.01, mde=None, bounds=[0.005, 0.015]) == (None, None)
        assert _below_mde_findings(mean_diff=0.01, mde=0.0, bounds=[0.005, 0.015]) == (None, None)
        assert _below_mde_findings(mean_diff=None, mde=0.05, bounds=[0.005, 0.015]) == (None, None)
        # At or above the floor there is nothing to say.
        assert _below_mde_findings(mean_diff=0.05, mde=0.05, bounds=[0.04, 0.06]) == (None, None)

    def test_the_unpriced_floor_advisory_fires_on_both_of_its_causes(self) -> None:
        """A floor of exactly 0.000 and an unmeasurable one are the same finding, worded apart."""
        unavailable = _note_unpriced_floor(mean_diff=0.2, mde=None)
        assert unavailable is not None and "came back unavailable" in unavailable
        zero = _note_unpriced_floor(mean_diff=0.2, mde=0.0)
        assert zero is not None and "came back 0.000" in zero
        assert _note_unpriced_floor(mean_diff=0.2, mde=0.5) is None
        assert _note_unpriced_floor(mean_diff=None, mde=None) is None

    def test_the_tight_interval_caveat_is_a_note_and_needs_a_real_floor(self) -> None:
        note = _note_tight_interval(bounds=[0.29, 0.31], mde=0.1)
        assert note is not None and "tighter than this suite's own noise floor" in note
        # Wider than the floor, no floor at all, or no interval: nothing to say.
        assert _note_tight_interval(bounds=[0.0, 0.5], mde=0.1) is None
        assert _note_tight_interval(bounds=[0.29, 0.31], mde=None) is None
        assert _note_tight_interval(bounds=[], mde=0.1) is None


class TestTheGateReadingStages:
    """The stages that produce a READING rather than a refusal."""

    def test_the_provenance_note_says_which_split_or_that_it_is_unrecorded(self, tmp_path: Path) -> None:
        run_dir = exec_run_dir(tmp_path, **WINNER)
        set_split(run_dir, "train")
        notes = _provenance_notes(run_dir)
        assert len(notes) == 1 and "--split 'train'" in notes[0]

        (run_dir / "run.json").unlink()
        unrecorded = _provenance_notes(run_dir)
        assert len(unrecorded) == 1 and "provenance is missing" in unrecorded[0]

    def test_the_paired_row_ids_stage_strips_the_suite_prefix_and_drops_empty_scores(self) -> None:
        scoped = {
            "candidate": {f"{EXEC_SUITE}/r1": [1.0], f"{EXEC_SUITE}/r2": [1.0], f"{EXEC_SUITE}/r3": []},
            "incumbent": {f"{EXEC_SUITE}/r1": [0.5], f"{EXEC_SUITE}/r3": [0.5]},
        }
        comparison = PairedComparison(
            vid_a="candidate",
            vid_b="incumbent",
            task_count=1,
            excluded_count=2,
            mean_diff=0.5,
            ci_low=0.4,
            ci_high=0.6,
            effect_size=1.0,
            p_value=0.01,
        )
        row_ids, note = _paired_row_ids(scoped_scores=scoped, comparison=comparison, suite_id=EXEC_SUITE)
        # r2 is candidate-only; r3 carries an empty score list on the candidate side.
        assert row_ids == ["r1"]
        assert note is not None and "2 row(s) scored for one arm only" in note

    def test_the_paired_row_ids_stage_is_silent_with_nothing_excluded(self) -> None:
        scoped = {"candidate": {f"{EXEC_SUITE}/r1": [1.0]}, "incumbent": {f"{EXEC_SUITE}/r1": [0.5]}}
        comparison = PairedComparison(
            vid_a="candidate",
            vid_b="incumbent",
            task_count=1,
            excluded_count=0,
            mean_diff=0.5,
            ci_low=0.4,
            ci_high=0.6,
            effect_size=1.0,
            p_value=0.01,
        )
        assert _paired_row_ids(scoped_scores=scoped, comparison=comparison, suite_id=EXEC_SUITE) == (["r1"], None)

    @pytest.mark.parametrize("sign", [1.0, -1.0])
    def test_the_signed_statistic_reverses_the_interval_rather_than_inverting_it(self, sign) -> None:
        """Negating an interval reverses it, so a naive sign leaves a "low" above its "high"."""
        comparison = PairedComparison(
            vid_a="candidate",
            vid_b="incumbent",
            task_count=4,
            excluded_count=0,
            mean_diff=0.2,
            ci_low=0.1,
            ci_high=0.3,
            effect_size=1.1,
            p_value=0.01,
        )
        signed = _signed_statistic(comparison, sign)
        assert signed.mean_diff == pytest.approx(sign * 0.2)
        assert signed.effect_size == pytest.approx(sign * 1.1)
        assert signed.bounds == sorted(signed.bounds), "ci_low <= ci_high must hold on both signs"
        assert signed.bounds == pytest.approx(sorted([sign * 0.1, sign * 0.3]))

    def test_a_missing_statistic_signs_to_nothing_rather_than_to_zero(self) -> None:
        comparison = PairedComparison(
            vid_a="candidate",
            vid_b="incumbent",
            task_count=1,
            excluded_count=0,
            mean_diff=None,
            ci_low=None,
            ci_high=None,
            effect_size=None,
            p_value=None,
        )
        signed = _signed_statistic(comparison, -1.0)
        assert (signed.mean_diff, signed.effect_size, signed.bounds) == (None, None, [])

    def test_the_primary_reading_reports_nothing_when_none_was_predeclared(self) -> None:
        assert _primary_reading(
            incumbent_rows={}, candidate_rows={}, check_row_ids=["r1"], primary_criterion_index=None
        ) == (None, None, None)

    def test_the_primary_reading_refuses_an_index_that_selects_no_row(self) -> None:
        rows = {"r1": [scored_result("r1", 1.0)]}
        reading = _primary_reading(
            incumbent_rows=rows, candidate_rows=rows, check_row_ids=["r1"], primary_criterion_index=7
        )
        assert reading.mean_diff is None
        assert reading.refusal is not None and "selected no usable row" in reading.refusal

    def test_the_primary_reading_does_not_refuse_with_no_paired_rows_to_read(self) -> None:
        """Nothing paired is a cause the sample stages own; this one must not restate it."""
        reading = _primary_reading(incumbent_rows={}, candidate_rows={}, check_row_ids=[], primary_criterion_index=7)
        assert reading.refusal is None

    def test_a_variant_id_refusal_still_reports_the_row_counts(self, tmp_path: Path) -> None:
        """`_GateExperiment.rows` exists so a refusal cannot hide an eroded sample.

        Two of the five causes know the paired and excluded counts — the ones where a comparison WAS
        computed and only the sign could not be resolved — and three cannot. Dropping the field left
        the whole suite green, so the claim its docstring makes was unasserted.
        """
        run_dir = exec_run_dir(tmp_path, **WINNER)
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="typo",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None and "is not one of the two variants" in verdict.gate_refusal
        assert verdict.rows_paired == len(WINNER["incumbent"]), "the pairing happened; only the sign failed"
        assert verdict.rows_excluded == 0

    def test_a_cause_with_no_comparison_at_all_reports_no_counts(self, tmp_path: Path) -> None:
        """The other side of the same field: nothing was paired, so nothing may be claimed."""
        run_dir = exec_run_dir(tmp_path, **WINNER)
        (run_dir / "experiment.json").unlink()
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None and "there is no experiment file" in verdict.gate_refusal
        assert (verdict.rows_paired, verdict.rows_excluded) == (0, 0)

    def test_zero_variance_outranks_the_below_mde_refusal(self) -> None:
        """The adjacency both stages can reach at once, which no other test covers.

        A constant NON-ZERO difference collapses the interval to a point, so if that difference also
        sits under the floor the below-MDE refusal fires too — its "interval excludes zero" conjunct
        holds on a zero-width interval away from zero. Zero variance is the more specific finding and
        its remedy is different (add rows the arms disagree on, not lower the floor), so it must win.
        """
        rows = {"r1": [scored_result("r1", 1.0)]}
        refusal, notes = _execution_diagnostics(
            incumbent_rows=rows,
            candidate_rows=rows,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            run_dir=Path("unused"),
            comparison=PairedComparison(
                vid_a="candidate",
                vid_b="incumbent",
                task_count=4,
                excluded_count=0,
                mean_diff=0.01,
                ci_low=0.01,
                ci_high=0.01,
                effect_size=None,
                p_value=0.0,
            ),
            mean_diff=0.01,
            effect_size=None,
            mde=0.05,
            bounds=[0.01, 0.01],
            refused_already=False,
        )
        assert refusal is not None
        assert "carry zero variance" in refusal
        assert "minimum detectable effect" not in refusal
        # And every note is withheld, because something refused.
        assert notes == []

    def test_a_primary_index_refusal_outranks_every_diagnostic(self, tmp_path: Path) -> None:
        """The adjacency the gate's own comment calls load-bearing, driven through the real gate.

        The primary-index cause is recorded BEFORE `_execution_diagnostics` runs, and it is that
        function's `refused_already=True` argument which suppresses the notes. Recorded after, it
        produced a `NOT A RESULT` headline above notes reading "this is an ordinary negative result
        and not a measurement problem" — measured.
        """
        run_dir = exec_run_dir(tmp_path, **uniform_shift(4))
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            primary_criterion_index=7,
            n_resamples=FAST_RESAMPLES,
        )
        assert verdict.gate_refusal is not None
        assert "selected no usable row" in verdict.gate_refusal
        assert not any("ordinary negative result" in note for note in verdict.notes), verdict.notes
        assert not any("tighter than this suite's own noise floor" in note for note in verdict.notes)

    def test_a_note_a_stage_produced_reaches_the_verdict(self, tmp_path: Path) -> None:
        """The pydantic-copy trap, asserted end to end after the decomposition.

        `_verdict` passes `notes` to the model, which COPIES the list — so a stage whose note is
        appended after construction is silently discarded. Every stage therefore RETURNS its notes
        and the gate extends the list before building.

        Two notes from OPPOSITE ends of the cascade, which is what makes this more than one
        sample: the provenance note is written before the refusal sink even exists, and the
        unpriced-floor advisory is the LAST thing any stage produces, after the statistic. A
        fixture producing only one of them cannot tell "the list was passed" from "the list was
        still being filled".
        """
        run_dir = exec_run_dir(tmp_path, **WINNER)
        set_split(run_dir, "train")
        verdict = execution_gate(
            run_dir=run_dir,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            n_resamples=FAST_RESAMPLES,
        )
        assert any("--split 'train'" in note for note in verdict.notes), verdict.notes
        assert any("came back 0.000" in note for note in verdict.notes), verdict.notes
