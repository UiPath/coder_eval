"""The composites in `coder_eval.optimize.api` — the surface `SKILL.md` imports.

Every assertion on a NUMBER compares against a direct call to the library function the composite
composes, never a hardcoded float: the composite's job is the guards, the fallbacks and the order,
and a literal here would pin the estimator instead — which is what `tests/lint/estimator_ledger.py`
exists to govern.

A `NoiseFloor` is deliberately never compared by `model_dump()`: `computed_at` is
`datetime.now(UTC)`, so the dump moves between two calls over the same tree.
"""

import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

import coder_eval.optimize.api
from coder_eval.models import (
    ACTIVATION_FLOOR_METRIC,
    EXECUTION_FLOOR_METRIC,
    ActivationGateVerdict,
    ExecutionGateVerdict,
    NoiseFloor,
    RegressionRow,
    RoundScores,
)
from coder_eval.optimize.activation import (
    SeedStability,
    activation_gate,
    confirm_gate,
    gate_seed_stability,
    holm_promote,
    min_discordant_rows,
    noise_floor_mde,
)
from coder_eval.optimize.api import (
    activation_floor_report,
    activation_gate_report,
    confirm_report_activation,
    confirm_report_execution,
    corpus_report,
    cost_quality_report,
    discreteness_report,
    execution_floor_report,
    execution_gate_report,
    headroom_report,
    leak_report,
    replicates_report,
    row_matrix_report,
    search_report,
    seed_stability_report,
)
from coder_eval.optimize.execution import (
    confirm_gate_execution,
    execution_gate,
    holm_promote_execution,
    measure_execution_noise_floor,
)
from coder_eval.optimize.store import (
    UNRESOLVED_MODEL,
    append_regression_rows,
    load_measurements,
    record_round_scores,
)
from coder_eval.reports_optimize import (
    LEAK_SCAN_BOUNDARY,
    SINGLE_REPLICATE_CAVEAT,
    render_execution_markdown,
    render_markdown,
    render_seed_stability,
)
from coder_eval.reports_stats import DEFAULT_ALPHA
from tests.optimize_fixtures import (
    EXEC_SUITE,
    SUITE,
    WINNER,
    arm_row_scores_for,
    assert_matches_render_pin,
    confirm_dir,
    cost_quality_arm,
    costed_result,
    eval_result,
    exec_run_dir,
    grader_result,
    headline_line,
    scored_result,
    set_split,
    shared_dirs,
    shifted_replicate_arms,
    split_labelled_arms,
    tiny_suite,
    weighted_arm,
    write_row,
)


def _sidecar(tmp_path: Path) -> Path:
    """A sidecar path under a `my-skill` directory — `load_measurements` keys on the parent name."""
    return tmp_path / "my-skill" / "measurements.json"


def _baseline(tmp_path: Path, *, invocations: int = 2) -> list[Path]:
    """One arm over `invocations` run directories, whose halves DISAGREE on two rows.

    Not `write_arm`, and that is the whole point: it writes the identical result into every
    invocation, so both halves of the null split are byte-identical and the floor comes back exactly
    0.000. A test asserting on that number is satisfied by any composition that reaches the
    estimator at all — it cannot see a mis-threaded seed, resample count or statistic. Here the
    later invocations flip two rows, so the measured floor is a number the arithmetic produced.
    """
    labels: dict[str, list[tuple[str, str]]] = {
        "r1": [("yes", "yes")],
        "r2": [("yes", "yes")],
        "r3": [("yes", "no")],
        "r4": [("no", "no")],
        "r5": [("no", "no")],
    }
    flipped = {**labels, "r1": [("yes", "no")], "r4": [("no", "yes")]}
    run_dirs: list[Path] = []
    for i in range(invocations):
        run_dir = tmp_path / f"run-{i}"
        for row_id, pairs in (labels if i == 0 else flipped).items():
            write_row(run_dir, "default", row_id, eval_result(row_id, pairs))
        run_dirs.append(run_dir)
    return run_dirs


class TestActivationFloorReport:
    def test_the_block_states_the_floor_the_estimator_measured(self, tmp_path: Path) -> None:
        run_dirs = _baseline(tmp_path)
        block = activation_floor_report(
            run_dirs=run_dirs, suite_id=SUITE, criterion_index=0, sidecar=_sidecar(tmp_path)
        )

        expected = noise_floor_mde(run_dirs=run_dirs, variant_id="default", suite_id=SUITE, criterion_index=0)
        assert expected is not None
        # Non-degenerate, or the assertion below cannot fail: a floor of 0.000 is what an identical
        # pair of halves measures, and every wiring mistake also produces it.
        assert expected > 0.0, "the fixture's halves must disagree, or this test pins nothing"
        assert f"{expected:.4f}" in block
        assert ACTIVATION_FLOOR_METRIC in block

    def test_one_invocation_says_the_sample_cannot_support_a_floor(self, tmp_path: Path) -> None:
        block = activation_floor_report(
            run_dirs=_baseline(tmp_path, invocations=1),
            suite_id=SUITE,
            criterion_index=0,
            sidecar=_sidecar(tmp_path),
        )

        assert "No noise floor" in block
        # The cause, not just the absence: "at least 2 invocations" is a different remedy from
        # "too few rows scored", and a block that only said "no floor" sends a reader to buy rows.
        assert "at least 2 invocations" in block
        assert "NOT the same as a floor of zero" in block

    def test_a_wrong_criterion_index_names_the_precondition_that_failed(self, tmp_path: Path) -> None:
        block = activation_floor_report(
            run_dirs=_baseline(tmp_path), suite_id=SUITE, criterion_index=7, sidecar=_sidecar(tmp_path)
        )

        assert "No noise floor" in block
        assert "criterion 7" in block
        assert "scored a classification result" in block

    def test_a_missing_sidecar_is_an_empty_cache_rather_than_a_raise(self, tmp_path: Path) -> None:
        sidecar = _sidecar(tmp_path)
        assert not sidecar.exists(), "round 1 always hits this path"

        block = activation_floor_report(
            run_dirs=_baseline(tmp_path), suite_id=SUITE, criterion_index=0, sidecar=sidecar
        )

        assert "Noise floor" in block

    def test_a_negative_criterion_index_still_raises_at_the_boundary(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="criterion_index must be >= 0"):
            activation_floor_report(
                run_dirs=_baseline(tmp_path), suite_id=SUITE, criterion_index=-1, sidecar=_sidecar(tmp_path)
            )


class TestExecutionFloorReport:
    def test_the_block_states_the_floor_over_a_replicated_control_arm(self, tmp_path: Path) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.2, 0.6], "r2": [0.4, 0.9]})

        block = execution_floor_report(
            run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, sidecar=_sidecar(tmp_path)
        )

        # The NUMBER, against a direct call — otherwise `floor.mde` could be swapped for any other
        # float field on the record and this stays green.
        expected = measure_execution_noise_floor(
            run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, model=UNRESOLVED_MODEL
        )
        assert expected is not None and expected.mde > 0.0
        assert f"{expected.mde:.4f}" in block
        assert EXECUTION_FLOOR_METRIC in block
        # And the sample shape the skill's prose says to read before quoting the floor.
        assert f"{expected.n_rows} row(s) scored in both halves" in block
        assert f"{expected.n_replicates} replicate(s) per row" in block

    def test_an_unresolvable_model_is_recorded_as_the_sentinel(self, tmp_path: Path, monkeypatch) -> None:
        """The one place the two tracks deliberately differ, so it is asserted rather than assumed.

        `NoiseFloor.model` is `str` with `min_length=1`, so this track cannot pass `None` through
        the way the activation composite does — and substituting a real-looking id would key a
        cache entry under a model nobody resolved.
        """
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.2, 0.6], "r2": [0.4, 0.9]})
        captured: dict[str, object] = {}

        def spy(**kwargs: object) -> NoiseFloor | None:
            captured.update(kwargs)
            return measure_execution_noise_floor(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("coder_eval.optimize.api.measure_execution_noise_floor", spy)
        block = execution_floor_report(
            run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, sidecar=_sidecar(tmp_path)
        )

        assert captured["model"] == UNRESOLVED_MODEL, "the fixture rows record no model_used"
        assert "Noise floor" in block

    def test_a_single_replicate_tree_says_no_floor(self, tmp_path: Path) -> None:
        block = execution_floor_report(
            run_dirs=weighted_arm(tmp_path, "incumbent", {"r1": [0.2], "r2": [0.4]}),
            variant_id="incumbent",
            suite_id=SUITE,
            sidecar=_sidecar(tmp_path),
        )

        assert "No noise floor" in block
        assert "NOT the same as a floor of zero" in block


# A directory that does not exist, so a read would fail DIFFERENTLY from the guard's message —
# which is what proves the guard runs before any filesystem access rather than merely resembling it.
_MISSING = "/nonexistent/optimize-api-guard"


def _activation_call(run_dirs) -> str:
    return activation_floor_report(
        run_dirs=run_dirs, suite_id=SUITE, criterion_index=0, sidecar=Path(_MISSING) / "measurements.json"
    )


def _execution_call(run_dirs) -> str:
    return execution_floor_report(
        run_dirs=run_dirs,
        variant_id="incumbent",
        suite_id=SUITE,
        sidecar=Path(_MISSING) / "measurements.json",
    )


def _row_matrix_call(run_dirs) -> str:
    return row_matrix_report(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE)


def _cost_quality_call(run_dirs) -> str:
    return cost_quality_report(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE)


def _headroom_call(run_dirs) -> str:
    return headroom_report(
        run_dirs=run_dirs,
        variant_id="incumbent",
        suite_id=SUITE,
        grader_index=0,
        sidecar=Path(_MISSING) / "measurements.json",
    )


def _replicates_call(run_dirs) -> str:
    return replicates_report(
        run_dirs=run_dirs, incumbent_variant="incumbent", candidate_variant="candidate", suite_id=SUITE
    )


def _search_call(run_dirs) -> str:
    return search_report(
        run_dirs=run_dirs,
        variant_id="incumbent",
        suite_id=SUITE,
        sidecar=Path(_MISSING) / "measurements.json",
    )


def _activation_gate_call(run_dirs) -> str:
    return activation_gate_report(
        gate_dirs=run_dirs,
        incumbent_variant="incumbent",
        candidate_variants=["cand-a"],
        suite_id=SUITE,
        criterion_index=0,
    )


def _seed_stability_call(run_dirs) -> str:
    return seed_stability_report(
        gate_dirs=run_dirs,
        incumbent_variant="incumbent",
        candidate_variant="cand-a",
        suite_id=SUITE,
        criterion_index=0,
    )


def _confirm_gate_dirs_call(run_dirs) -> str:
    return confirm_report_activation(
        gate_dirs=run_dirs,
        confirm_dirs=[Path(_MISSING)],
        incumbent_variant="incumbent",
        candidate_variants=["cand-a"],
        candidate_variant="cand-a",
        suite_id=SUITE,
        criterion_index=0,
    )


def _confirm_confirm_dirs_call(run_dirs) -> str:
    # The SECOND run-dir argument, which no other composite has — so it needs its own entry.
    return confirm_report_activation(
        gate_dirs=[Path(_MISSING)],
        confirm_dirs=run_dirs,
        incumbent_variant="incumbent",
        candidate_variants=["cand-a"],
        candidate_variant="cand-a",
        suite_id=SUITE,
        criterion_index=0,
    )


def _corpus_call(run_dirs) -> str:
    return corpus_report(
        run_dirs=run_dirs,
        variant_ids=["incumbent"],
        suite_id=SUITE,
        criterion_index=None,
        sidecar=Path(_MISSING) / "measurements.json",
    )


# Every composite that computes a reported number from rows it READ, and therefore owes its block a
# staleness note. Declared once so the two directions below cannot cover different sets.
_REPORTERS = ["row-matrix", "cost-quality", "corpus", "headroom", "replicates", "search"]


# EVERY composite taking `run_dirs`, so the guard is asserted on the SURFACE rather than on the
# private helper they share: a composite that forgot to call it is exactly the regression this
# catches, and it is invisible to any test of the helper itself. Grows with each phase.
_ENTRY_POINTS = [
    pytest.param(_activation_call, id="activation-floor"),
    pytest.param(_execution_call, id="execution-floor"),
    pytest.param(_row_matrix_call, id="row-matrix"),
    pytest.param(_cost_quality_call, id="cost-quality"),
    pytest.param(_headroom_call, id="headroom"),
    pytest.param(_corpus_call, id="corpus"),
    pytest.param(_replicates_call, id="replicates"),
    pytest.param(_search_call, id="search"),
    pytest.param(_activation_gate_call, id="activation-gate"),
    pytest.param(_seed_stability_call, id="seed-stability"),
    pytest.param(_confirm_gate_dirs_call, id="confirm-gate-dirs"),
    pytest.param(_confirm_confirm_dirs_call, id="confirm-confirm-dirs"),
]


@pytest.mark.parametrize("call", _ENTRY_POINTS)
class TestRunDirsAreRejectedAtTheBoundary:
    def test_a_bare_string_raises_type_error_naming_the_argument(self, call) -> None:
        with pytest.raises(TypeError, match="run_dirs must be a sequence of pathlib"):
            call(_MISSING)

    def test_a_list_of_strings_raises_type_error(self, call) -> None:
        with pytest.raises(TypeError, match="string"):
            call([_MISSING])

    def test_a_bare_path_names_the_argument_rather_than_failing_downstream(self, call) -> None:
        # The likeliest of the three: every composite takes a LIST where the gate below it takes one
        # directory. Without the guard this raises "'PosixPath' object is not iterable" from
        # whichever comprehension reaches it first, naming neither the argument nor the fix.
        with pytest.raises(TypeError, match=r"pass \[dir\], not dir"):
            call(Path(_MISSING))

    def test_an_empty_sequence_is_a_caller_error_not_a_measurement(self, call) -> None:
        # Distinct from "dirs given, no rows found", which renders as a no-floor block: nothing was
        # named here, so there is no suite to report a reading about.
        with pytest.raises(ValueError, match="run_dirs is empty"):
            call([])


class TestDiscretenessReport:
    def test_the_block_states_the_count_the_estimator_computed(self) -> None:
        block = discreteness_report(rows=12, survivors=3)

        expected = min_discordant_rows(12, DEFAULT_ALPHA / 3)
        assert expected is not None
        assert f"**{expected} of 12 row(s)" in block
        # The threshold the composite divided, not a restated alpha.
        assert f"{DEFAULT_ALPHA / 3:.5f}" in block

    def test_no_survivors_is_a_caller_error(self) -> None:
        # `DEFAULT_ALPHA / 0` is a ZeroDivisionError today. A Stage B with no candidates has no
        # threshold to state — it is not an easier test.
        with pytest.raises(ValueError, match="survivors must be at least 1"):
            discreteness_report(rows=12, survivors=0)

    def test_an_empty_suite_is_a_caller_error_rather_than_an_unachievable_size(self) -> None:
        """The two causes of a `None` from `min_discordant_rows`, kept apart.

        An empty suite also returns `None`, and the block's remedy — shrink the family, raise the
        draw count — is wrong for it. Rejecting it here is what lets the rendered `None` name one
        cause honestly.
        """
        assert min_discordant_rows(0, DEFAULT_ALPHA) is None, "the other cause of a None"
        with pytest.raises(ValueError, match="rows must be at least 1"):
            discreteness_report(rows=0, survivors=3)

    def test_an_unachievable_threshold_says_so_and_names_the_lever(self) -> None:
        # A family so large that its Holm threshold drops below the estimator's own p floor, so no
        # discordant count clears it. The remedy is the family or the draw count — never rows, which
        # is the whole point of the reading.
        block = discreteness_report(rows=8, survivors=1_000)

        assert min_discordant_rows(8, DEFAULT_ALPHA / 1_000) is None, "the fixture must be unachievable"
        assert "No candidate can promote at this size" in block
        assert "buying rows cannot fix this one" in block


class TestRowMatrixReport:
    def test_the_matrix_is_pinned(self, tmp_path: Path) -> None:
        # Both arms into the SAME run dir, as a real experiment produces them — so the second
        # builder's return value is the same list and is deliberately discarded. The two arms win
        # opposite rows, which is the disjoint-winners shape the block exists to make visible.
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.4], "r2": [0.9]})
        weighted_arm(tmp_path, "cand-a", {"r1": [0.9], "r2": [0.4]})

        block = row_matrix_report(run_dirs=run_dirs, variant_ids=["incumbent", "cand-a"], suite_id=SUITE)

        assert_matches_render_pin(block, "row_matrix_report")

    def test_criterion_index_none_reads_the_rows_weighted_score(self, tmp_path: Path) -> None:
        # `weighted_arm` sets `weighted_score` and no classification result, so a block carrying
        # these numbers proves the None path rather than the criterion path.
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.375], "r2": [0.625]})

        block = row_matrix_report(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE)

        assert "0.375" in block and "0.625" in block

    def test_a_variant_absent_from_the_run_dirs_is_named_rather_than_omitted(self, tmp_path: Path) -> None:
        # Silently rendering a short table is how a wrong suite_id or variant reads as a result.
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.4], "r2": [0.9]})

        block = row_matrix_report(run_dirs=run_dirs, variant_ids=["incumbent", "ghost"], suite_id=SUITE)

        assert "scored no rows at all" in block
        assert "ghost" in block

    def test_a_single_replicate_round_carries_the_ranking_caveat(self, tmp_path: Path) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.4], "r2": [0.9]})

        block = row_matrix_report(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE)

        assert SINGLE_REPLICATE_CAVEAT in block

    def test_a_replicated_round_does_not(self, tmp_path: Path) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.4, 0.5], "r2": [0.9, 0.8]})

        block = row_matrix_report(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE, n_replicates=2)

        assert SINGLE_REPLICATE_CAVEAT not in block
        assert "mean of 2 replicate(s)" in block


class TestCostQualityReport:
    def test_a_costless_arm_is_named_as_excluded(self, tmp_path: Path) -> None:
        """ "Correct rather than a bug", in the fence's own words — so it is pinned as behaviour.

        An unmeasured cost is not a free one, so the arm is excluded from the front and named.
        """
        cost_quality_arm(tmp_path, "incumbent", {"r1": (0.90, 1.00), "r2": (0.90, 1.00)})
        cost_quality_arm(tmp_path, "cand-free", {"r1": (0.95, None), "r2": (0.95, None)})

        block = cost_quality_report(
            run_dirs=[tmp_path / "run-0"], variant_ids=["incumbent", "cand-free"], suite_id=SUITE
        )

        assert "NOT on the front: cand-free" in block

    def test_a_variant_absent_from_the_run_dirs_is_named_as_off_the_front(self, tmp_path: Path) -> None:
        # `"ghost" in block` would pass on the table row alone (`| ghost | 0 | — | — |`), which is
        # what a wrong suite_id looks like when it reads as a result. The NAMING clause is the guard.
        cost_quality_arm(tmp_path, "incumbent", {"r1": (0.90, 1.00)})

        block = cost_quality_report(run_dirs=[tmp_path / "run-0"], variant_ids=["incumbent", "ghost"], suite_id=SUITE)

        assert "missing a coordinate and therefore NOT on the front" in block
        assert "ghost" in block


def _graded_arm(
    tmp_path: Path, per_row: dict[str, tuple[float, dict[str, str] | None]], *, variant: str = "incumbent"
) -> list[Path]:
    """One arm of grader-scored rows, each carrying its own `RULES` attribution (or none)."""
    run_dir = tmp_path / "run-0"
    for row_id, (score, rules) in per_row.items():
        write_row(run_dir, variant, row_id, grader_result(row_id, score, rules))
    return [run_dir]


class TestHeadroomReport:
    """The one block that can say STOP before a candidate is written."""

    def test_every_rule_the_grader_mentioned_gets_a_row_including_one_that_never_fails(self, tmp_path: Path) -> None:
        # `R2` passes on every row, so its ceiling is a real 0.0 — "no candidate for it can show
        # anything here". Leaving it out is the difference between an answer and a missing row.
        run_dirs = _graded_arm(
            tmp_path,
            {
                "r1": (0.5, {"R1": "fail", "R2": "pass"}),
                "r2": (1.0, {"R1": "pass", "R2": "pass"}),
                "r3": (0.8, {"R1": "fail", "R2": "pass"}),
            },
        )

        block = headroom_report(
            run_dirs=run_dirs,
            variant_id="incumbent",
            suite_id=SUITE,
            grader_index=0,
            sidecar=_sidecar(tmp_path),
        )

        assert "`R1`" in block
        assert "`R2`" in block, "a rule the suite always passes has a real ceiling of 0.0"

    def test_a_stale_row_is_named_in_the_returned_block(self, tmp_path: Path) -> None:
        """The regression test for the bug the shipped fence has: a ceilings table over a dirty tree.

        Asserted on the BLOCK, not on caplog. `arm_row_scores` already logs this, and a warning the
        skill session never sees is precisely the failure mode being fixed — the number reaches the
        ledger, so the doubt has to reach it too.
        """
        run_dirs = _graded_arm(tmp_path, {"r1": (0.5, {"R1": "fail"}), "r2": (1.0, {"R1": "pass"})})
        # A row left behind by an earlier invocation of a re-used `--run-dir`: on disk, absent from
        # `run.json`. It loads, parses and is pooled into the ceilings, silently.
        write_row(run_dirs[0], "incumbent", "leftover", grader_result("leftover", 0.0, {"R1": "fail"}), record=False)

        block = headroom_report(
            run_dirs=run_dirs,
            variant_id="incumbent",
            suite_id=SUITE,
            grader_index=0,
            sidecar=_sidecar(tmp_path),
        )

        assert "may be over a contaminated tree" in block
        assert "leftover" in block
        assert "Re-run into a fresh --run-dir" in block

    def test_absent_attribution_falls_back_to_the_suite_ceiling_and_names_both_causes(self, tmp_path: Path) -> None:
        # No `RULES` line at all — an older grader, or a `grader_index` pointing elsewhere. An empty
        # table would read as "no rule has any headroom" when it means "nobody asked".
        run_dirs = _graded_arm(tmp_path, {"r1": (0.5, None), "r2": (0.8, None)})

        block = headroom_report(
            run_dirs=run_dirs,
            variant_id="incumbent",
            suite_id=SUITE,
            grader_index=0,
            sidecar=_sidecar(tmp_path),
        )

        assert "Rule attribution was unavailable" in block
        assert "whole suite" in block
        # All THREE causes: `rule_row_map` returns an empty `failed` for an absent line, a wrong
        # index AND an unparseable line, and they land in this one branch together.
        assert "predates that contract" in block
        assert "points at a different criterion" in block
        assert "did not parse" in block

    def test_no_rows_for_the_incumbent_raises_rather_than_exiting_the_interpreter(self, tmp_path: Path) -> None:
        # `SystemExit` from a library kills an interpreter that had other work to do — and the
        # skill's session is exactly that interpreter.
        run_dirs = _graded_arm(tmp_path, {"r1": (0.5, {"R1": "fail"})})

        # `pytest.raises(ValueError)` IS the "not SystemExit" assertion — `SystemExit` derives from
        # `BaseException`, so it would propagate through and fail the test rather than satisfy it.
        with pytest.raises(ValueError, match="scored no rows") as excinfo:
            headroom_report(
                run_dirs=run_dirs,
                variant_id="ghost",
                suite_id=SUITE,
                grader_index=0,
                sidecar=_sidecar(tmp_path),
            )

        assert "wrong suite_id, variant id or run dir" in str(excinfo.value)

    def test_a_single_replicate_round_renders_with_no_floor_rather_than_a_zero_one(self, tmp_path: Path) -> None:
        # Round 1 by construction: one replicate cannot split against itself. A substituted 0.0
        # would turn every ceiling into a verdict.
        run_dirs = _graded_arm(tmp_path, {"r1": (0.5, {"R1": "fail"}), "r2": (1.0, {"R1": "pass"})})

        block = headroom_report(
            run_dirs=run_dirs,
            variant_id="incumbent",
            suite_id=SUITE,
            grader_index=0,
            sidecar=_sidecar(tmp_path),
        )

        assert "No noise floor was measured, so no verdict is rendered" in block
        assert "x floor" not in block, "no floor means no ratio column"


class TestCorpusReport:
    def _sidecar_with_corpus(self, tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
        sidecar = _sidecar(tmp_path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        append_regression_rows(
            sidecar,
            [RegressionRow(row_id=row_id, promoted_in_round=1, reason=reason) for row_id, reason in rows],
        )
        return sidecar

    def test_an_empty_corpus_says_there_is_nothing_to_check(self, tmp_path: Path) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [1.0]})

        block = corpus_report(
            run_dirs=run_dirs,
            variant_ids=["incumbent"],
            suite_id=SUITE,
            criterion_index=None,
            sidecar=_sidecar(tmp_path),
        )

        assert "No regression corpus yet" in block

    def test_a_missing_score_is_a_hole_with_both_causes_named(self, tmp_path: Path) -> None:
        # Not a loss: the row errored in this run, or it belongs to this skill's OTHER suite. The
        # corpus cannot tell them apart, so the block must not pick one.
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [1.0]})
        sidecar = self._sidecar_with_corpus(tmp_path, [("other-suite-row", "promoted on it")])

        block = corpus_report(
            run_dirs=run_dirs,
            variant_ids=["incumbent"],
            suite_id=SUITE,
            criterion_index=None,
            sidecar=sidecar,
        )

        assert "**hole** `other-suite-row`" in block
        assert "errored in this run" in block and "OTHER suite" in block
        assert "**lost**" not in block, "a hole is not a loss — the two have different remedies"
        assert "0 measured loss(es), 1 hole(s)" in block

    def test_a_sub_threshold_score_is_a_measured_loss_and_the_threshold_moves_it(self, tmp_path: Path) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.6]})
        sidecar = self._sidecar_with_corpus(tmp_path, [("r1", "promoted on it")])
        call = {
            "run_dirs": run_dirs,
            "variant_ids": ["incumbent"],
            "suite_id": SUITE,
            "criterion_index": None,
            "sidecar": sidecar,
        }

        strict = corpus_report(**call)
        assert "**lost** `r1` at 0.600" in strict

        # The same row at a bar a fractional execution suite would set: no longer a loss.
        lenient = corpus_report(**call, threshold=0.5)
        assert "clears the corpus" in lenient
        assert "**lost**" not in lenient

    def test_the_threshold_is_stated_in_the_block(self, tmp_path: Path) -> None:
        # It changes what "lost" MEANS, so a ledger entry that omitted it would not be readable.
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [1.0]})
        sidecar = self._sidecar_with_corpus(tmp_path, [("r1", "promoted on it")])

        block = corpus_report(
            run_dirs=run_dirs,
            variant_ids=["incumbent"],
            suite_id=SUITE,
            criterion_index=None,
            sidecar=sidecar,
            threshold=0.5,
        )

        assert "below 0.500 is a row" in block


class TestEveryReportingCompositeNamesAContaminatedTree:
    """The rule the whole module follows, asserted on the SURFACE rather than on `_staleness_note`.

    A stale tree loads, parses and returns a confident number — there is nothing to notice. The
    primitives below already detect it, and every one of them can only `logger.warning`, which a
    skill session never sees. These composites return markdown, which HAS somewhere to put it, so a
    composite that forgot the note would ship the same silent wrong number the rule exists to stop.

    **Both directions are parametrized over the same list.** A note that fired on every block would
    stop being read, and it would have been baked into the committed matrix pin. A new reporting
    composite is one entry in `_REPORTERS` and is then covered both ways at once.
    """

    STALE = "may be over a contaminated tree"

    def _clean(self, tmp_path: Path) -> list[Path]:
        return weighted_arm(tmp_path, "incumbent", {"r1": [1.0], "r2": [0.5]})

    def _contaminate(self, run_dir: Path) -> None:
        # On disk, absent from `run.json` — a row left behind by an earlier `--run-dir` re-use.
        write_row(run_dir, "incumbent", "leftover", scored_result("leftover", 0.0), record=False)

    def _corpus_sidecar(self, tmp_path: Path) -> Path:
        sidecar = _sidecar(tmp_path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        append_regression_rows(sidecar, [RegressionRow(row_id="r1", promoted_in_round=1, reason="promoted on it")])
        return sidecar

    def _call(self, name: str, tmp_path: Path, run_dirs: list[Path]) -> str:
        if name == "row-matrix":
            return row_matrix_report(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE)
        if name == "cost-quality":
            return cost_quality_report(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE)
        if name == "corpus":
            return corpus_report(
                run_dirs=run_dirs,
                variant_ids=["incumbent"],
                suite_id=SUITE,
                criterion_index=None,
                sidecar=self._corpus_sidecar(tmp_path),
            )
        if name == "replicates":
            return replicates_report(
                run_dirs=run_dirs, incumbent_variant="incumbent", candidate_variant="candidate", suite_id=SUITE
            )
        if name == "search":
            sidecar = _sidecar(tmp_path)
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            record_round_scores(
                sidecar,
                RoundScores(
                    round=1,
                    arm_row_scores=[arm_row_scores_for("head-arm", {"r1": 0.5, "r2": 0.5})],
                    lineage_head="head-arm",
                ),
            )
            return search_report(run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, sidecar=sidecar)
        if name == "headroom":
            return headroom_report(
                run_dirs=run_dirs,
                variant_id="incumbent",
                suite_id=SUITE,
                grader_index=0,
                sidecar=_sidecar(tmp_path),
            )
        raise AssertionError(f"unknown reporter {name!r}")

    @pytest.mark.parametrize("name", _REPORTERS)
    def test_the_block_names_the_staleness(self, tmp_path: Path, name: str) -> None:
        run_dirs = self._clean(tmp_path)
        self._contaminate(run_dirs[0])

        block = self._call(name, tmp_path, run_dirs)

        assert self.STALE in block
        assert "leftover" in block

    @pytest.mark.parametrize("name", _REPORTERS)
    def test_a_clean_tree_carries_no_note(self, tmp_path: Path, name: str) -> None:
        assert self.STALE not in self._call(name, tmp_path, self._clean(tmp_path))


class TestReplicatesReport:
    def test_a_reproducible_row_beside_a_cancelling_one_is_surfaced(self, tmp_path: Path) -> None:
        """The fence's own prose calls this "the most informative row in the run", so it is pinned.

        Two zero-variance rows with opposite signs are what an aggregate hides: on the round this
        fixture is taken from they cancelled to a suite delta of +0.0001. The verdict block has no
        channel for either row, which is why this block exists.
        """
        # The two rows from the round the skill's prose cites. On a 15-row suite they cancelled to a
        # suite delta of +0.0001; here they are the whole suite, so the point is that the BLOCK shows
        # both individually where no aggregate could.
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.76, 0.76, 0.76], "r2": [0.86, 0.86, 0.86]})
        weighted_arm(tmp_path, "candidate", {"r1": [1.0, 1.0, 1.0], "r2": [0.59, 0.59, 0.59]})

        block = replicates_report(
            run_dirs=run_dirs, incumbent_variant="incumbent", candidate_variant="candidate", suite_id=SUITE
        )

        assert "Zero variance on BOTH arms" in block
        assert "r1" in block and "r2" in block
        # Not estimator constants: these are the fixture's own arithmetic (1.00-0.76, 0.59-0.86),
        # and the fixture reproduces the exact round the skill's prose cites — so the block, the
        # prose and the renderer's docstring cannot drift apart in pairs.
        assert "+0.240" in block and "-0.270" in block

    def test_both_arms_are_rendered(self, tmp_path: Path) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.5, 0.5]})
        weighted_arm(tmp_path, "candidate", {"r1": [0.9, 0.9]})

        block = replicates_report(
            run_dirs=run_dirs, incumbent_variant="incumbent", candidate_variant="candidate", suite_id=SUITE
        )

        assert "| incumbent | candidate |" in block
        assert "0.500" in block and "0.900" in block

    def test_a_contaminated_tree_is_named_once_for_both_arms(self, tmp_path: Path) -> None:
        """The second half of the CE053 correctness fix, and the one-sweep contract.

        A dir carrying both arms is a single re-used `--run-dir`, so it is one fault and gets one
        sentence naming both locations — not one per arm.
        """
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.5, 0.5]})
        weighted_arm(tmp_path, "candidate", {"r1": [0.9, 0.9]})
        write_row(run_dirs[0], "incumbent", "leftover", scored_result("leftover", 0.0), record=False)
        write_row(run_dirs[0], "candidate", "leftover", scored_result("leftover", 1.0), record=False)

        block = replicates_report(
            run_dirs=run_dirs, incumbent_variant="incumbent", candidate_variant="candidate", suite_id=SUITE
        )

        assert block.count("may be over a contaminated tree") == 1, "one fault, one sentence"
        assert "/incumbent" in block and "/candidate" in block, "and it names both arms"

    def test_the_criterion_index_reaches_both_arms(self, tmp_path: Path) -> None:
        """Threaded at two call sites, so dropping it from one is a silent wrong delta.

        `scored_result` records a `weighted_score` of the given value while criterion 0 is the
        classification match — 1.0 at or above 0.5, else 0.0. So the two readings are visibly
        different tables over the same tree, and an arm left on the wrong one produces a delta
        between two different quantities with nothing raised.
        """
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.5]})
        weighted_arm(tmp_path, "candidate", {"r1": [0.25]})
        call = {
            "run_dirs": run_dirs,
            "incumbent_variant": "incumbent",
            "candidate_variant": "candidate",
            "suite_id": SUITE,
        }

        weighted = replicates_report(**call)
        assert "| r1 | 0.500 | 0.250 | -0.250 |" in weighted

        # The SAME tree read at criterion 0: 0.5 classifies as a match (1.0), 0.25 does not (0.0).
        # An arm left on `weighted_score` would render one column from each quantity — a delta of
        # 1.000-0.250 or 0.500-0.000, neither of which is this row.
        by_criterion = replicates_report(**call, criterion_index=0)
        assert "| r1 | 1.000 | 0.000 | -1.000 |" in by_criterion

    def test_an_arm_that_scored_nothing_is_named_rather_than_rendered_as_holes(self, tmp_path: Path) -> None:
        # A full column of `— (hole)` reads as "present on one arm only", which the block's own prose
        # says — a reading, for a mistyped variant. Every sibling composite makes this loud.
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.5, 0.5]})

        block = replicates_report(
            run_dirs=run_dirs, incumbent_variant="incumbent", candidate_variant="ghost", suite_id=SUITE
        )

        assert "wrong variant id, a wrong suite id or a wrong run directory" in block
        assert "ghost" in block

    def test_an_out_of_range_criterion_index_raises_before_rendering(self, tmp_path: Path) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.5, 0.5]})
        weighted_arm(tmp_path, "candidate", {"r1": [0.9, 0.9]})

        with pytest.raises(ValueError, match="past every row's criteria list"):
            replicates_report(
                run_dirs=run_dirs,
                incumbent_variant="incumbent",
                candidate_variant="candidate",
                suite_id=SUITE,
                criterion_index=7,
            )

    def test_an_arm_compared_against_itself_is_a_caller_error(self, tmp_path: Path) -> None:
        # Every delta 0.000 and every row dead reads as "this candidate changes nothing" — a result,
        # for what is a typo. `execution_gate` refuses the same comparison.
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.5, 0.5]})

        with pytest.raises(ValueError, match="an arm compared against itself"):
            replicates_report(
                run_dirs=run_dirs,
                incumbent_variant="incumbent",
                candidate_variant="incumbent",
                suite_id=SUITE,
            )


_LEAK_GRADED = "minimum-task-score"


def _leak_suite(tmp_path: Path) -> Path:
    """A dataset-backed suite with one TRAIN row and one TEST row, both graded on a string.

    Both splits carry a graded string so the test half is a real trap: a scan that widened past
    `split="train"` would flag content the proposer is blinded to.
    """
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        yaml.safe_dump(
            {
                "task_id": SUITE,
                "description": "leak preflight fixture",
                "initial_prompt": "do the thing for ${row.id}",
                "dataset": {
                    "rows": [
                        {"id": "r1", "split": "train", "graded": _LEAK_GRADED},
                        {"id": "r2", "split": "test", "graded": "test-only-secret-value"},
                    ]
                },
                "success_criteria": [
                    {
                        "type": "file_check",
                        "description": "grades the row",
                        "path": "out.yml",
                        "includes": ["${row.graded}"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return suite


def _leak_arm(root: Path, name: str, skill_name: str, *, body: str = "# skill\n", script: str | None = None) -> Path:
    arm = root / name
    skill = arm / "skills" / skill_name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    if script is not None:
        (skill / "scripts").mkdir()
        (skill / "scripts" / "helper.py").write_text(script, encoding="utf-8")
    return arm


class TestLeakReport:
    SKILL_NAME = "my-skill"

    def _call(self, tmp_path: Path, root: Path, *, round_tag: str = "1") -> str:
        return leak_report(
            suite=_leak_suite(tmp_path),
            skill_name=self.SKILL_NAME,
            root=root,
            round_tag=round_tag,
            baseline_dir=root / "1-incumbent",
        )

    def test_one_arm_reproducing_a_train_row_is_flagged_and_the_other_is_clean(self, tmp_path: Path) -> None:
        root = tmp_path / "snapshots"
        _leak_arm(root, "1-incumbent", self.SKILL_NAME)
        _leak_arm(root, "1-a-leaks", self.SKILL_NAME, body=f"# skill\nAlways write {_LEAK_GRADED}\n")
        _leak_arm(root, "1-b-clean", self.SKILL_NAME, body="# skill\nName the action.\n")

        block = self._call(tmp_path, root)

        assert "`1-a-leaks` — 1 span(s)" in block
        assert _LEAK_GRADED in block
        assert "`1-b-clean` — clean." in block

    def test_a_test_split_string_is_not_scanned(self, tmp_path: Path) -> None:
        # The proposer is blinded to the test split, so reporting on it is reporting on nothing the
        # candidate could have seen — and there is no `split` parameter to widen this by mistake.
        root = tmp_path / "snapshots"
        _leak_arm(root, "1-incumbent", self.SKILL_NAME)
        _leak_arm(root, "1-a", self.SKILL_NAME, body="# skill\nWrite test-only-secret-value\n")

        block = self._call(tmp_path, root)

        assert "`1-a` — clean." in block
        assert "test-only-secret-value" not in block

    def test_a_span_the_baseline_already_has_is_not_flagged(self, tmp_path: Path) -> None:
        # The DIFF, not an absolute scan. Measured on this repo's own `ci` skill, an absolute scan
        # flags five strings that are simply the output contract its suite grades.
        root = tmp_path / "snapshots"
        body = f"# skill\nAlways write {_LEAK_GRADED}\n"
        _leak_arm(root, "1-incumbent", self.SKILL_NAME, body=body)
        _leak_arm(root, "1-a", self.SKILL_NAME, body=body)

        assert "`1-a` — clean." in self._call(tmp_path, root)

    def test_a_graded_string_in_a_script_is_flagged(self, tmp_path: Path) -> None:
        # `skill_text` reads the WHOLE directory. A one-file read comes back clean here, which is
        # byte-identical to a genuinely clean candidate — the worst shape a preflight can have.
        root = tmp_path / "snapshots"
        _leak_arm(root, "1-incumbent", self.SKILL_NAME)
        _leak_arm(root, "1-a", self.SKILL_NAME, script=f"THRESHOLD = {_LEAK_GRADED!r}\n")

        block = self._call(tmp_path, root)

        assert "`1-a` — 1 span(s)" in block

    def test_the_baseline_and_the_control_arm_are_skipped_and_named(self, tmp_path: Path) -> None:
        root = tmp_path / "snapshots"
        _leak_arm(root, "1-incumbent", self.SKILL_NAME, body=f"# skill\n{_LEAK_GRADED}\n")
        _leak_arm(root, "1-control", self.SKILL_NAME, body=f"# skill\n{_LEAK_GRADED}\n")
        _leak_arm(root, "1-a", self.SKILL_NAME)

        block = self._call(tmp_path, root)

        assert "1-incumbent" not in block.split("Not scanned, by design:")[0]
        assert "Not scanned, by design: `1-control`, `1-incumbent`" in block

    def test_a_wrong_round_tag_says_nothing_matched_rather_than_reading_as_clean(self, tmp_path: Path) -> None:
        # An empty block is the worst false negative available here.
        root = tmp_path / "snapshots"
        _leak_arm(root, "1-incumbent", self.SKILL_NAME)
        _leak_arm(root, "1-a", self.SKILL_NAME)

        block = self._call(tmp_path, root, round_tag="7")

        assert "No candidate arms matched" in block
        assert "That is not a clean result" in block

    def test_an_arm_with_no_skill_directory_is_named_and_the_others_still_report(self, tmp_path: Path) -> None:
        root = tmp_path / "snapshots"
        _leak_arm(root, "1-incumbent", self.SKILL_NAME)
        (root / "1-a-broken").mkdir(parents=True)
        _leak_arm(root, "1-b", self.SKILL_NAME, body=f"# skill\n{_LEAK_GRADED}\n")

        block = self._call(tmp_path, root)

        assert "could not be scanned at all — a wiring fault, not a clean result" in block
        assert "1-a-broken" in block
        assert "no skill directory at" in block
        # And it is NOT reported as a leak span: a wiring fault rendered as memorization sends a
        # reader to rewrite a candidate that was never scanned.
        assert "1-a-broken` — 1 span(s)" not in block
        assert "`1-b` — 1 span(s)" in block, "one mis-snapshotted arm must not hide the others"

    def test_a_missing_baseline_directory_raises_rather_than_scanning_absolutely(self, tmp_path: Path) -> None:
        """The asymmetric half, and the more dangerous one.

        A missing CANDIDATE dir is named and skipped; a missing BASELINE makes `skill_text` return
        `""`, so every graded string in every arm reports as newly added — the wolf-crying the diff
        exists to prevent, and silent.
        """
        root = tmp_path / "snapshots"
        _leak_arm(root, "1-a", self.SKILL_NAME, body=f"# skill\n{_LEAK_GRADED}\n")

        with pytest.raises(ValueError, match="no baseline skill directory"):
            self._call(tmp_path, root)

    def test_the_block_states_the_verbatim_only_boundary(self, tmp_path: Path) -> None:
        root = tmp_path / "snapshots"
        _leak_arm(root, "1-incumbent", self.SKILL_NAME)
        _leak_arm(root, "1-a", self.SKILL_NAME)

        assert LEAK_SCAN_BOUNDARY in self._call(tmp_path, root)

    def test_api_exposes_no_split_parameter(self) -> None:
        # Scanning the whole suite flags content a candidate is entitled to be fitted to; scanning
        # test rows reports on a split the proposer cannot see. Neither is a knob worth having.
        assert "split" not in inspect.signature(leak_report).parameters


class TestSearchReport:
    """One arm against the recorded lineage head — a step to revert, never a promotion."""

    def _sidecar_with_head(self, tmp_path: Path, scores: dict[str, float], *, head: str = "head-arm") -> Path:
        sidecar = _sidecar(tmp_path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        record_round_scores(
            sidecar,
            RoundScores(
                round=1,
                arm_row_scores=[arm_row_scores_for(head, scores)],
                lineage_head=head,
            ),
        )
        return sidecar

    def test_a_better_explored_arm_reads_accept(self, tmp_path: Path) -> None:
        sidecar = self._sidecar_with_head(tmp_path, {"r1": 0.5, "r2": 0.5})
        run_dirs = weighted_arm(tmp_path, "explored", {"r1": [0.9], "r2": [0.9]})

        block = search_report(run_dirs=run_dirs, variant_id="explored", suite_id=SUITE, sidecar=sidecar)

        assert "ACCEPT into the lineage" in block

    def test_a_worse_explored_arm_reads_revert(self, tmp_path: Path) -> None:
        sidecar = self._sidecar_with_head(tmp_path, {"r1": 0.9, "r2": 0.9})
        run_dirs = weighted_arm(tmp_path, "explored", {"r1": [0.5], "r2": [0.5]})

        block = search_report(run_dirs=run_dirs, variant_id="explored", suite_id=SUITE, sidecar=sidecar)

        assert "REVERT — the head stands" in block

    def test_no_recorded_lineage_raises_with_the_actionable_sentence(self, tmp_path: Path) -> None:
        # `SystemExit` in the shipped fence. From a library that kills an interpreter with other work.
        run_dirs = weighted_arm(tmp_path, "explored", {"r1": [0.9]})

        with pytest.raises(ValueError, match="run a multi-arm Stage A round first"):
            search_report(run_dirs=run_dirs, variant_id="explored", suite_id=SUITE, sidecar=_sidecar(tmp_path))

    def test_a_wrong_variant_id_names_the_variant_and_the_dirs(self, tmp_path: Path) -> None:
        # `arms[0].row_scores` empty, reachable from a single mistyped slug. The shipped fence did
        # not crash: it printed `search_compare`'s "the two rounds share no rows … a wiring fault"
        # refusal, which points at sampling seeds and snapshot mounts rather than at the typo.
        sidecar = self._sidecar_with_head(tmp_path, {"r1": 0.5})
        run_dirs = weighted_arm(tmp_path, "explored", {"r1": [0.9]})

        with pytest.raises(ValueError, match="wrong variant id, a wrong suite id or a wrong run directory") as exc:
            search_report(run_dirs=run_dirs, variant_id="ghost", suite_id=SUITE, sidecar=sidecar)

        assert "ghost" in str(exc.value)
        assert str(run_dirs[0]) in str(exc.value)

    def test_a_corpus_row_the_explored_arm_re_loses_is_surfaced(self, tmp_path: Path) -> None:
        # A search accept advances the LINEAGE, so accepting a regression carries it forward until a
        # multi-arm round notices. `search_compare` blocks on it; the block has to say so.
        sidecar = self._sidecar_with_head(tmp_path, {"r1": 0.5, "r2": 0.5})
        append_regression_rows(sidecar, [RegressionRow(row_id="r1", promoted_in_round=1, reason="promoted on it")])
        run_dirs = weighted_arm(tmp_path, "explored", {"r1": [0.6], "r2": [1.0]})

        block = search_report(run_dirs=run_dirs, variant_id="explored", suite_id=SUITE, sidecar=sidecar)

        assert "re-loses" in block
        assert "r1" in block
        assert "ACCEPT into the lineage" not in block

    def test_an_empty_corpus_is_passed_through_rather_than_branched_on(self, tmp_path: Path) -> None:
        # Normal early, and `search_compare` takes `corpus=()`. A branch here would be a second way
        # to say the same thing, and a second place to get it wrong.
        sidecar = self._sidecar_with_head(tmp_path, {"r1": 0.5})
        assert load_measurements(sidecar).regression_corpus == []
        run_dirs = weighted_arm(tmp_path, "explored", {"r1": [0.9]})

        assert "ACCEPT into the lineage" in search_report(
            run_dirs=run_dirs, variant_id="explored", suite_id=SUITE, sidecar=sidecar
        )


class TestActivationGateReport:
    """Stage B's ordering, made structural: a sequence in, one correction out."""

    def _arms(self, tmp_path: Path, candidates: dict[str, dict[str, list[tuple[str, str]]]]) -> list[Path]:
        incumbent = {"p1": [("yes", "no")], "p2": [("yes", "no")], "p3": [("yes", "yes")]}
        run_dirs: list[Path] = []
        for i in range(3):
            run_dir = tmp_path / f"run-{i}"
            for row_id, labels in incumbent.items():
                write_row(run_dir, "incumbent", row_id, eval_result(row_id, labels))
            for variant, rows in candidates.items():
                for row_id, labels in rows.items():
                    write_row(run_dir, variant, row_id, eval_result(row_id, labels))
            run_dirs.append(run_dir)
        return run_dirs

    _WINNER: ClassVar[dict[str, list[tuple[str, str]]]] = {
        "p1": [("yes", "yes")],
        "p2": [("yes", "yes")],
        "p3": [("yes", "yes")],
    }

    def test_both_verdicts_match_a_direct_gate_plus_one_holm_call(self, tmp_path: Path) -> None:
        """SSOT against the library, never a literal: the composite's job is the ORDER.

        Every number here comes from calling the same functions in the same order by hand, so the
        test cannot pass while the composite corrects per candidate.
        """
        run_dirs = self._arms(tmp_path, {"cand-a": self._WINNER, "cand-b": self._WINNER})

        block = activation_gate_report(
            gate_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variants=["cand-a", "cand-b"],
            suite_id=SUITE,
            criterion_index=0,
        )

        expected = holm_promote(
            [
                activation_gate(
                    incumbent_run_dirs=run_dirs,
                    candidate_run_dirs=run_dirs,
                    incumbent_variant="incumbent",
                    candidate_variant=slug,
                    suite_id=SUITE,
                    criterion_index=0,
                )
                for slug in ("cand-a", "cand-b")
            ]
        )
        assert block == "\n\n".join(render_markdown(v) for v in expected)
        assert len(expected) == 2

    def test_the_family_is_joined_as_one_block_per_candidate_in_order(self, tmp_path: Path) -> None:
        """The JOIN, asserted independently of the expression that produces it.

        `test_both_verdicts_match_a_direct_gate_plus_one_holm_call` derives its expectation with the
        same `"\n\n".join(...)` the implementation uses, so it cannot see the separator change —
        exactly the "reorders a line without changing a number" class `assert_matches_render_pin`
        exists for. A whole-block pin would work but would run the full bootstrap and be repinned by
        any watched-constant move, so the structure is asserted directly instead.
        """
        run_dirs = self._arms(tmp_path, {"cand-a": self._WINNER, "cand-b": self._WINNER})

        block = activation_gate_report(
            gate_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variants=["cand-a", "cand-b"],
            suite_id=SUITE,
            criterion_index=0,
        )

        headings = [line for line in block.splitlines() if line.startswith("### Activation gate")]
        assert len(headings) == 2, headings
        # Family ORDER, not sorted order: the caller's list is the family, and a ledger entry that
        # reordered it would not line up with the round's own proposal list.
        assert "`cand-a`" in headings[0] and "`cand-b`" in headings[1]
        # Exactly one blank line between the two verdict blocks, and none trailing.
        assert block.count("\n\n### Activation gate") == 1
        assert not block.endswith("\n")

    def test_holm_promote_is_called_exactly_once_over_the_whole_family(self, tmp_path: Path, monkeypatch) -> None:
        # Asserted by counting CALLS, not by reading the code: correcting per candidate leaves every
        # token in place, every test of a single verdict green, and silently reverts to an
        # uncorrected alpha. That is the failure this composite exists to make unrepresentable.
        run_dirs = self._arms(tmp_path, {"cand-a": self._WINNER, "cand-b": self._WINNER})
        calls: list[int] = []
        real = holm_promote

        def counting(verdicts, *args, **kwargs):
            calls.append(len(verdicts))
            return real(verdicts, *args, **kwargs)

        monkeypatch.setattr("coder_eval.optimize.api.holm_promote", counting)
        activation_gate_report(
            gate_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variants=["cand-a", "cand-b"],
            suite_id=SUITE,
            criterion_index=0,
        )

        assert calls == [2], "one call, over both candidates"

    def test_a_family_of_one_renders_and_is_not_special_cased(self, tmp_path: Path) -> None:
        # Holm at m = 1 IS the uncorrected alpha, by construction — the right answer for a round that
        # gated one candidate, and not a case to branch on.
        run_dirs = self._arms(tmp_path, {"cand-a": self._WINNER})

        block = activation_gate_report(
            gate_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variants=["cand-a"],
            suite_id=SUITE,
            criterion_index=0,
        )
        direct = holm_promote(
            [
                activation_gate(
                    incumbent_run_dirs=run_dirs,
                    candidate_run_dirs=run_dirs,
                    incumbent_variant="incumbent",
                    candidate_variant="cand-a",
                    suite_id=SUITE,
                    criterion_index=0,
                )
            ]
        )[0]
        assert block == render_markdown(direct)
        assert direct.holm_alpha == DEFAULT_ALPHA

    def test_an_empty_family_is_a_caller_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="candidate_variants is empty"):
            activation_gate_report(
                gate_dirs=self._arms(tmp_path, {}),
                incumbent_variant="incumbent",
                candidate_variants=[],
                suite_id=SUITE,
                criterion_index=0,
            )

    def test_a_duplicated_candidate_is_a_caller_error(self, tmp_path: Path) -> None:
        # It inflates m, so Holm divides the alpha by a family larger than the one gated — making the
        # test stricter for every candidate in it, including the real ones.
        with pytest.raises(ValueError, match="repeats"):
            activation_gate_report(
                gate_dirs=self._arms(tmp_path, {"cand-a": self._WINNER}),
                incumbent_variant="incumbent",
                candidate_variants=["cand-a", "cand-a"],
                suite_id=SUITE,
                criterion_index=0,
            )

    def test_a_cross_split_refusal_reaches_the_block_and_forces_promoted_false(self, tmp_path: Path) -> None:
        # A refusal keeps `promoted=None` until the correction forces False, so the composite's
        # always-correct shape is what puts it in front of a reader either way.
        incumbent, candidate = split_labelled_arms(tmp_path, "train", "test")

        block = activation_gate_report(
            gate_dirs=incumbent + candidate,
            incumbent_variant="incumbent",
            candidate_variants=["candidate"],
            suite_id=SUITE,
            criterion_index=0,
        )

        assert "NOT A RESULT" in block
        # One literal: `activation.py` emits only the un-backticked form, so a disjunction over both
        # spellings has a branch that can never match and would survive the message changing.
        assert "DIFFERENT --split values" in block

    def test_sibling_indices_reach_the_gate_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        # The sibling checks VETO, and `None` (the default) DERIVES and checks every one. So the
        # parameter's job is to let a caller turn the check off deliberately — which means an
        # explicit sequence has to arrive unchanged, or the veto is over the wrong criteria.
        run_dirs = self._arms(tmp_path, {"cand-a": self._WINNER})
        seen: list[object] = []
        real = activation_gate

        def spy(**kwargs):
            seen.append(kwargs["sibling_indices"])
            return real(**kwargs)

        monkeypatch.setattr("coder_eval.optimize.api.activation_gate", spy)
        activation_gate_report(
            gate_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variants=["cand-a"],
            suite_id=SUITE,
            criterion_index=0,
            sibling_indices=[1, 2],
        )

        assert seen == [[1, 2]]

    def test_a_failing_guardrail_reads_blocked_and_does_not_promote(self, tmp_path: Path) -> None:
        """A candidate that separates on F1 and doubles what a row costs must not read PROMOTED.

        Built as a real tree rather than a hand-made verdict, because the whole point of the
        composite is that the guardrail reaches the block through the SAME single correction the
        p-values go through — `failed_vetoes` is what forces `promoted` False, and a block that said
        PROMOTED here would be shippable.
        """
        run_dirs: list[Path] = []
        for i in range(3):
            run_dir = tmp_path / f"run-{i}"
            for row_id in (f"p{n}" for n in range(12)):
                write_row(run_dir, "incumbent", row_id, costed_result(row_id, [("yes", "no")], cost=1.0, duration=10.0))
                write_row(run_dir, "cand-a", row_id, costed_result(row_id, [("yes", "yes")], cost=4.0, duration=10.0))
            run_dirs.append(run_dir)

        block = activation_gate_report(
            gate_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variants=["cand-a"],
            suite_id=SUITE,
            criterion_index=0,
        )

        # The headline, not a substring search: `BLOCKED BY A GUARDRAIL` is its own rung, and the
        # only thing a reader acts on. `failed_vetoes` is what forced it, over the same single
        # correction the p-value went through.
        assert headline_line(block).startswith("BLOCKED BY A GUARDRAIL")
        assert "cost (USD/row)" in block

    def test_a_sample_too_small_for_a_statistic_reads_not_promoted_not_undecided(self, tmp_path: Path) -> None:
        # The two are different states with different remedies: NOT PROMOTED outright means there was
        # no p for the family to correct, while UNDECIDED means the correction never ran. The
        # composite always corrects, so UNDECIDED must be unreachable through it.
        run_dirs: list[Path] = []
        for i in range(3):
            run_dir = tmp_path / f"run-{i}"
            write_row(run_dir, "incumbent", "p0", eval_result("p0", [("yes", "no")]))
            write_row(run_dir, "cand-a", "p0", eval_result("p0", [("yes", "yes")]))
            run_dirs.append(run_dir)

        block = activation_gate_report(
            gate_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variants=["cand-a"],
            suite_id=SUITE,
            criterion_index=0,
        )

        # Measured: a one-row suite produces no p, so the verdict comes back NOT PROMOTED outright —
        # and, having no p, it is also outside the family, so the shrink note leads the block. Both
        # are true and both are the point; the verdict's own headline is read past the note.
        assert "The Holm correction saw 0 of 1 predeclared candidate(s)" in block
        verdict = "### Activation gate" + block.split("### Activation gate", 1)[1]
        assert headline_line(verdict) == "NOT PROMOTED"
        assert "UNDECIDED" not in block, "the composite always corrects, so this rung is unreachable"

    def test_a_string_family_is_a_caller_error(self) -> None:
        # `"cand-a"` is a `Sequence[str]`: without the guard it gates one candidate per LETTER and
        # Holm corrects over a family of six, making the test stricter for a candidate that does not
        # exist. The same hole `_require_run_dirs` closes on the other argument.
        with pytest.raises(TypeError, match="must be a sequence of variant ids, not a string"):
            activation_gate_report(
                gate_dirs=[Path("/nonexistent")],
                incumbent_variant="incumbent",
                candidate_variants="cand-a",  # type: ignore[arg-type]
                suite_id=SUITE,
                criterion_index=0,
            )

    def test_the_incumbent_in_the_candidate_list_is_a_caller_error(self, tmp_path: Path) -> None:
        """One copy-paste away: Stage A's `variant_ids` legitimately starts with the incumbent.

        Gating an arm against itself adds a candidate to the Holm family, so every REAL candidate is
        decided against a tighter threshold — and the self-comparison's own block reads CANNOT
        SEPARATE, with nothing connecting the two.
        """
        with pytest.raises(ValueError, match="contains the incumbent"):
            activation_gate_report(
                gate_dirs=self._arms(tmp_path, {"cand-a": self._WINNER}),
                incumbent_variant="incumbent",
                candidate_variants=["incumbent", "cand-a"],
                suite_id=SUITE,
                criterion_index=0,
            )

    def test_a_one_shot_iterable_is_materialized_rather_than_exhausted(self, tmp_path: Path) -> None:
        # A generator passes every guard and then yields nothing to the gate loop, so the block comes
        # back EMPTY — the exact outcome the empty-family check exists to prevent.
        run_dirs = self._arms(tmp_path, {"cand-a": self._WINNER})

        block = activation_gate_report(
            gate_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variants=(v for v in ("cand-a",)),  # type: ignore[arg-type]
            suite_id=SUITE,
            criterion_index=0,
        )

        assert block, "a one-shot iterable must not render an empty block"
        assert "cand-a" in block

    def test_no_estimator_knob_is_exposed(self) -> None:
        # Every exposed knob is a way to produce a number that is not comparable with the floor
        # recorded beside it, and no shipped fence varies any of these.
        params = inspect.signature(activation_gate_report).parameters
        assert not {"seed", "n_resamples", "confidence", "materiality"} & set(params)


class TestSeedStabilityReport:
    """Disagreeing seeds are the FINDING, and the block must not collapse them into an answer."""

    def _arms(self, tmp_path: Path) -> list[Path]:
        incumbent, candidate = tiny_suite(3, 1)
        return shared_dirs(tmp_path, incumbent, candidate)

    def test_the_block_matches_a_direct_gate_seed_stability_call(self, tmp_path: Path) -> None:
        run_dirs = self._arms(tmp_path)
        call = {
            "incumbent_variant": "incumbent",
            "candidate_variant": "candidate",
            "suite_id": SUITE,
            "criterion_index": 0,
        }

        block = seed_stability_report(gate_dirs=run_dirs, **call)

        direct = gate_seed_stability(
            incumbent_run_dirs=run_dirs, candidate_run_dirs=run_dirs, sibling_indices=None, **call
        )
        assert block == render_seed_stability(direct)

    def test_no_single_verdict_is_presented(self, tmp_path: Path) -> None:
        # Whatever the seeds did, the block states an agreement COUNT at a family of one — never a
        # `promoted` a reader could quote as the round's decision.
        block = seed_stability_report(
            gate_dirs=self._arms(tmp_path),
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
        )

        assert "would promote at" in block
        assert "family of ONE" in block
        assert "zero** extra agent runs" in block

    def test_an_unstable_split_is_named_a_coin_flip(self) -> None:
        # Rendered directly: a tree that lands 2/3 depends on the estimator, and pinning one here
        # would pin the estimator rather than the composite. The renderer's contract is the claim.
        unstable = SeedStability(seeds=(0, 1, 2), promote_agreement=2, p_values=(0.01, 0.02, 0.06), p_spread=0.05)
        block = render_seed_stability(unstable)

        assert "UNSTABLE" in block
        assert "coin flip, not a result" in block
        assert "Do not report the majority's verdict as the verdict" in block

    def test_an_arm_gated_against_itself_is_a_caller_error(self, tmp_path: Path) -> None:
        # `SeedStability` has no refusal channel, so this renders "STABLE — would promote at none of
        # 3 seeds": a maximally confident negative for a typo.
        with pytest.raises(ValueError, match="an arm gated against itself"):
            seed_stability_report(
                gate_dirs=self._arms(tmp_path),
                incumbent_variant="incumbent",
                candidate_variant="incumbent",
                suite_id=SUITE,
                criterion_index=0,
            )

    def test_duplicate_seeds_are_a_caller_error(self, tmp_path: Path) -> None:
        # Re-running ONE draw three times reports 3/3 at a spread of 0.0000 — the most confident
        # stability claim available, off a single bootstrap. The seeds are the axis being varied.
        with pytest.raises(ValueError, match="seeds repeats"):
            seed_stability_report(
                gate_dirs=self._arms(tmp_path),
                incumbent_variant="incumbent",
                candidate_variant="candidate",
                suite_id=SUITE,
                criterion_index=0,
                seeds=(0, 0, 0),
            )

    def test_no_seeds_at_all_raises_from_the_estimator(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at least one seed"):
            seed_stability_report(
                gate_dirs=self._arms(tmp_path),
                incumbent_variant="incumbent",
                candidate_variant="candidate",
                suite_id=SUITE,
                criterion_index=0,
                seeds=(),
            )

    def test_a_refused_gate_is_not_rendered_as_a_stable_negative(self, tmp_path: Path) -> None:
        """The failure this phase's whole thesis is about: a refusal that reads as a result.

        Every seed's gate refuses on a cross-split pair, so no seed produces a statistic — and
        "STABLE — would promote at none of 3 seeds" is a confident negative about a comparison that
        was never made.
        """
        incumbent, candidate = split_labelled_arms(tmp_path, "train", "test")

        block = seed_stability_report(
            gate_dirs=incumbent + candidate,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
        )

        assert "NOT A STABILITY READING" in block
        assert "STABLE —" not in block
        assert "read its own block for the refusal" in block

    def test_every_keyword_is_declared_rather_than_forwarded_as_a_bag(self) -> None:
        params = inspect.signature(seed_stability_report).parameters
        assert all(p.kind is not p.VAR_KEYWORD for p in params.values()), "no **kwargs on this surface"
        assert {
            "gate_dirs",
            "incumbent_variant",
            "candidate_variant",
            "suite_id",
            "criterion_index",
            "sibling_indices",
            "seeds",
        } == set(params)


class TestExecutionGateReport:
    """The same promotion contract, on a track whose Holm family lives ACROSS run dirs."""

    def _gates(self, tmp_path: Path, candidates: dict[str, dict[str, list[float]]]) -> dict[str, Path]:
        return {
            candidate: exec_run_dir(
                tmp_path / candidate,
                incumbent=WINNER["incumbent"],
                candidate=rows,
                candidate_variant=candidate,
            )
            for candidate, rows in candidates.items()
        }

    # `WINNER`, not a uniform shift. Measured: a candidate that beats the incumbent by exactly the
    # same amount on every row is REFUSED for zero variance ("the two arms differed by exactly 0.500
    # on every one of the 6 paired rows"), so a family built that way would exercise the refusal path
    # in every test here — including the ones about promotion and ordering.
    _WINS: ClassVar[dict[str, list[float]]] = WINNER["candidate"]

    def test_both_verdicts_match_a_direct_gate_plus_one_holm_call(self, tmp_path: Path) -> None:
        gates = self._gates(tmp_path, {"cand-a": self._WINS, "cand-b": self._WINS})

        block = execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

        expected = holm_promote_execution(
            [
                execution_gate(
                    run_dir=gates[candidate],
                    incumbent_variant="incumbent",
                    candidate_variant=candidate,
                    suite_id=EXEC_SUITE,
                )
                for candidate in sorted(gates)
            ]
        )
        assert block == "\n\n".join(render_execution_markdown(v) for v in expected)

    def test_holm_promote_execution_is_called_exactly_once(self, tmp_path: Path, monkeypatch) -> None:
        gates = self._gates(tmp_path, {"cand-a": self._WINS, "cand-b": self._WINS})
        calls: list[int] = []
        real = holm_promote_execution

        def counting(verdicts, *args, **kwargs):
            calls.append(len(verdicts))
            return real(verdicts, *args, **kwargs)

        monkeypatch.setattr("coder_eval.optimize.api.holm_promote_execution", counting)
        execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

        assert calls == [2], "one call, over both candidates"

    def test_block_order_is_by_candidate_id_regardless_of_insertion_order(self, tmp_path: Path) -> None:
        # A dict-order change must not silently reorder a ledger entry a later round is compared
        # against, so the order is a property of the ids rather than of how the dict was built.
        gates = self._gates(tmp_path, {"cand-b": self._WINS, "cand-a": self._WINS})
        assert list(gates) == ["cand-b", "cand-a"], "the fixture must insert out of order"

        block = execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

        headings = [line for line in block.splitlines() if line.startswith("### Execution gate")]
        assert len(headings) == 2
        assert "`cand-a`" in headings[0] and "`cand-b`" in headings[1]

    def test_an_empty_mapping_is_a_caller_error(self) -> None:
        with pytest.raises(ValueError, match="gates is empty"):
            execution_gate_report(gates={}, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

    def test_the_incumbent_as_a_candidate_is_a_caller_error(self, tmp_path: Path) -> None:
        gates = self._gates(tmp_path, {"cand-a": self._WINS})
        gates["incumbent"] = gates["cand-a"]

        with pytest.raises(ValueError, match="contains the incumbent"):
            execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

    def test_a_missing_run_dir_names_the_candidate_it_belongs_to(self, tmp_path: Path) -> None:
        # On this track each candidate has its OWN dir to get wrong, so a bare FileNotFoundError
        # from inside the gate gives no clue which arm is mis-wired.
        gates = self._gates(tmp_path, {"cand-a": self._WINS})
        gates["cand-b"] = tmp_path / "never-ran"

        with pytest.raises(ValueError, match="no gate run directory for") as excinfo:
            execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

        assert "cand-b" in str(excinfo.value)
        assert "cand-a" not in str(excinfo.value), "only the mis-wired arm is named"

    def test_a_three_variant_run_dir_refuses_and_the_refusal_reaches_the_block(self, tmp_path: Path) -> None:
        """`paired_comparison` fires only for exactly two variants, so a third is not a comparison.

        The refusal keeps `promoted=None` until the correction forces False, so the composite's
        always-correct shape is what puts it in front of a reader either way.
        """
        run_dir = exec_run_dir(
            tmp_path / "three",
            incumbent=WINNER["incumbent"],
            candidate=WINNER["candidate"],
            candidate_variant="cand-a",
            variant_ids=["incumbent", "cand-a", "cand-c"],
        )

        block = execution_gate_report(gates={"cand-a": run_dir}, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

        assert "NOT A RESULT" in block

    def test_primary_criterion_index_none_is_passed_through_untouched(self, tmp_path: Path, monkeypatch) -> None:
        # The normal case, and it must stay a READING rather than becoming a decision here.
        gates = self._gates(tmp_path, {"cand-a": self._WINS})
        seen: list[object] = []
        real = execution_gate

        def spy(**kwargs):
            seen.append((kwargs["primary_criterion_index"], kwargs["engagement_criterion_index"]))
            return real(**kwargs)

        monkeypatch.setattr("coder_eval.optimize.api.execution_gate", spy)
        execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

        assert seen == [(None, 0)]

    def test_a_refused_arm_shrinking_the_family_is_named(self, tmp_path: Path) -> None:
        """The one guard here that fails OPEN, so it has to be said rather than inferred.

        A verdict with no p-value is not a family member, so a refused arm drops out and `m` falls —
        and the surviving candidates were predeclared against the larger family but decided against
        the smaller, LOOSER threshold. Measured: two keys onto one run dir promoted the good arm
        "across a family of 1" while the round had predeclared two.
        """
        run_dir = self._gates(tmp_path, {"cand-a": self._WINS})["cand-a"]

        block = execution_gate_report(
            gates={"cand-a": run_dir, "cand-b": run_dir},
            incumbent_variant="incumbent",
            suite_id=EXEC_SUITE,
        )

        assert "The Holm correction saw 1 of 2 predeclared candidate(s)" in block
        assert "LOOSER threshold" in block
        assert "family of 1" in block, "and the misleading line it is warning about is still visible"

    def test_a_whole_family_that_gates_cleanly_carries_no_shrink_notice(self, tmp_path: Path) -> None:
        # It must not fire on every block, or it stops being read.
        gates = self._gates(tmp_path, {"cand-a": self._WINS, "cand-b": self._WINS})

        block = execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

        assert "predeclared candidate(s)" not in block

    def test_the_family_is_joined_as_one_block_per_candidate(self, tmp_path: Path) -> None:
        # The SSOT test derives its expectation with the implementation's own `"\n\n".join(...)`, so
        # it cannot see the separator change. The twin carries the same structural assertion.
        gates = self._gates(tmp_path, {"cand-a": self._WINS, "cand-b": self._WINS})

        block = execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE)

        assert block.count("\n\n### Execution gate") == 1
        assert not block.endswith("\n")
        # The composite always corrects, so no verdict can reach a reader undecided.
        assert "UNDECIDED" not in block

    def test_a_non_default_criterion_index_reaches_the_gate(self, tmp_path: Path, monkeypatch) -> None:
        """Both indices are threaded, and `engagement_criterion_index=None` DISARMS a veto.

        Pinning only the defaults let the composite hardcode them with every test still green —
        measured. The engagement reading feeds `integrity_checks`, and a failed one forces `promoted`
        False, so a dropped argument silently removes the check.
        """
        gates = self._gates(tmp_path, {"cand-a": self._WINS})
        seen: list[tuple[object, object]] = []
        real = execution_gate

        def spy(**kwargs):
            seen.append((kwargs["engagement_criterion_index"], kwargs["primary_criterion_index"]))
            return real(**kwargs)

        monkeypatch.setattr("coder_eval.optimize.api.execution_gate", spy)
        execution_gate_report(
            gates=gates,
            incumbent_variant="incumbent",
            suite_id=EXEC_SUITE,
            engagement_criterion_index=None,
            primary_criterion_index=2,
        )

        assert seen == [(None, 2)]

    def test_no_estimator_knob_is_exposed(self) -> None:
        params = inspect.signature(execution_gate_report).parameters
        assert not {"seed", "n_resamples", "confidence", "materiality"} & set(params)


class TestGatesAreRejectedAtTheBoundary:
    """`api.py` claims every entry point rejects a bad path AT the boundary. This one takes a mapping.

    Two of `_require_run_dirs`' three checks are unrepresentable here — a duplicate key cannot exist
    and there is no one-shot iterable — so this covers what remains: the container and the values.
    """

    ENTRY_POINTS: ClassVar[list] = [
        pytest.param(
            lambda gates: execution_gate_report(gates=gates, incumbent_variant="incumbent", suite_id=EXEC_SUITE),
            id="execution-gate",
        ),
        pytest.param(
            lambda gates: confirm_report_execution(
                gates=gates,
                confirm_run_dir=Path(_MISSING),
                incumbent_variant="incumbent",
                candidate_variant="cand-a",
                suite_id=EXEC_SUITE,
            ),
            id="confirm-execution",
        ),
    ]

    @pytest.mark.parametrize("call", ENTRY_POINTS)
    def test_a_non_mapping_names_the_shape_it_wanted(self, call) -> None:
        with pytest.raises(TypeError, match="gates must be a mapping of candidate id"):
            call(["cand-a"])

    @pytest.mark.parametrize("call", ENTRY_POINTS)
    def test_a_string_value_is_rejected_before_any_read(self, call) -> None:
        # The likeliest mistake from a hand-typed fence, and it used to surface as
        # `AttributeError: 'str' object has no attribute 'is_dir'`.
        with pytest.raises(TypeError, match="gates values must be pathlib"):
            call({"cand-a": "runs/round1-gate-a"})


class TestConfirmReportActivation:
    """Stage C, and the recomputation the whole phase rests on."""

    def _gate_arms(self, tmp_path: Path, candidates: Sequence[str]) -> list[Path]:
        incumbent = {f"r{i}": [("yes", "no")] for i in range(6)}
        winner = {f"r{i}": [("yes", "yes")] for i in range(6)}
        run_dirs: list[Path] = []
        for i in range(3):
            run_dir = tmp_path / f"gate-{i}"
            for row_id, labels in incumbent.items():
                write_row(run_dir, "incumbent", row_id, eval_result(row_id, labels))
            for candidate in candidates:
                for row_id, labels in winner.items():
                    write_row(run_dir, candidate, row_id, eval_result(row_id, labels))
            run_dirs.append(run_dir)
        return run_dirs

    def _confirm_arms(self, tmp_path: Path, candidate: str) -> list[Path]:
        incumbent = {f"r{i}": [("yes", "no")] for i in range(6)}
        winner = {f"r{i}": [("yes", "yes")] for i in range(6)}
        run_dirs: list[Path] = []
        for i in range(3):
            run_dir = tmp_path / f"confirm-{i}"
            for row_id, labels in incumbent.items():
                write_row(run_dir, "incumbent", row_id, eval_result(row_id, labels))
            for row_id, labels in winner.items():
                write_row(run_dir, candidate, row_id, eval_result(row_id, labels))
            set_split(run_dir, "test")
            run_dirs.append(run_dir)
        return run_dirs

    def _call(self, tmp_path: Path, gate_dirs, confirm_dirs, candidates, candidate="cand-a") -> str:
        return confirm_report_activation(
            gate_dirs=gate_dirs,
            confirm_dirs=confirm_dirs,
            incumbent_variant="incumbent",
            candidate_variants=candidates,
            candidate_variant=candidate,
            suite_id=SUITE,
            criterion_index=0,
        )

    def test_the_recomputed_train_verdict_is_bit_identical_to_a_direct_gate_plus_holm(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The phase's entire premise, pinned through the COMPOSITE rather than beside it.

        Calling the library twice and comparing only shows the library is deterministic. What has to
        hold is that the verdict the composite hands `confirm_gate` is the SAME one Stage B rendered
        — so the spy captures `train_verdict` and compares its `model_dump()` against a direct gate
        + Holm + select. A future non-seeded estimator would make Stage C classify against a verdict
        no reader ever saw, and this is what makes that loud.
        """
        gate_dirs = self._gate_arms(tmp_path, ["cand-a", "cand-b"])
        confirm_dirs = self._confirm_arms(tmp_path, "cand-a")
        captured: list[ActivationGateVerdict] = []
        real = confirm_gate

        def spy(**kwargs):
            captured.append(kwargs["train_verdict"])
            return real(**kwargs)

        monkeypatch.setattr("coder_eval.optimize.api.confirm_gate", spy)
        self._call(tmp_path, gate_dirs, confirm_dirs, ["cand-a", "cand-b"])

        direct = holm_promote(
            [
                activation_gate(
                    incumbent_run_dirs=gate_dirs,
                    candidate_run_dirs=gate_dirs,
                    incumbent_variant="incumbent",
                    candidate_variant=slug,
                    suite_id=SUITE,
                    criterion_index=0,
                )
                for slug in ("cand-a", "cand-b")
            ]
        )
        expected = next(v for v in direct if v.candidate_variant == "cand-a")
        assert len(captured) == 1
        assert captured[0].model_dump() == expected.model_dump()
        # And it is the CORRECTED verdict, not a bare gate: `holm_alpha` is only set by the wrapper.
        assert expected.holm_alpha is not None

    def test_the_block_states_the_family_size_it_recomputed_against(self, tmp_path: Path) -> None:
        # `promoted` is a property of the FAMILY, and nothing in the run tree records how many
        # candidates Stage B gated — so a reader comparing this against their own round is the only
        # check there is, and only if the number is in the block.
        gate_dirs = self._gate_arms(tmp_path, ["cand-a", "cand-b"])
        confirm_dirs = self._confirm_arms(tmp_path, "cand-a")

        block = self._call(tmp_path, gate_dirs, confirm_dirs, ["cand-a", "cand-b"])

        assert "recomputed over a family of 2" in block

    def test_both_the_confirm_block_and_the_test_verdict_are_rendered(self, tmp_path: Path) -> None:
        gate_dirs = self._gate_arms(tmp_path, ["cand-a"])
        confirm_dirs = self._confirm_arms(tmp_path, "cand-a")

        block = self._call(tmp_path, gate_dirs, confirm_dirs, ["cand-a"])

        assert "### Stage C confirm" in block
        assert "### Activation gate" in block, "both prints the fence made"

    def test_a_candidate_absent_from_the_family_names_both(self, tmp_path: Path) -> None:
        gate_dirs = self._gate_arms(tmp_path, ["cand-a"])
        confirm_dirs = self._confirm_arms(tmp_path, "cand-a")

        with pytest.raises(ValueError, match="is not in the Stage B family") as excinfo:
            self._call(tmp_path, gate_dirs, confirm_dirs, ["cand-a"], candidate="cand-z")

        assert "cand-z" in str(excinfo.value)
        assert "cand-a" in str(excinfo.value)

    def test_a_gate_that_produced_no_statistic_raises_and_is_not_called_a_loss(self, tmp_path: Path) -> None:
        """A gate that could not MEASURE is not a candidate that lost, and the message must not say so.

        A wrong `criterion_index` reads no comparable rows, so there is no p-value — nothing was
        learned about the candidate. Reporting that as "did not promote" sends a reader to rewrite a
        candidate whose gate never ran.
        """
        gate_dirs = self._gate_arms(tmp_path, ["cand-a"])
        confirm_dirs = self._confirm_arms(tmp_path, "cand-a")

        with pytest.raises(ValueError, match="produced no statistic") as excinfo:
            confirm_report_activation(
                gate_dirs=gate_dirs,
                confirm_dirs=confirm_dirs,
                incumbent_variant="incumbent",
                candidate_variants=["cand-a"],
                candidate_variant="cand-a",
                suite_id=SUITE,
                criterion_index=9,
            )

        assert "NOT a candidate that lost" in str(excinfo.value)

    def test_a_shortlist_is_refused_before_the_family_is_re_gated(self, tmp_path: Path) -> None:
        # Rank 1 owns the sentence, and the guard runs BEFORE the recomputation — which costs one
        # full bootstrap per family member, so a shortlist that died on a dict lookup afterwards
        # would have burned all of it.
        with pytest.raises(TypeError, match="must be ONE variant id"):
            confirm_report_activation(
                gate_dirs=[tmp_path],
                confirm_dirs=[tmp_path],
                incumbent_variant="incumbent",
                candidate_variants=["cand-a"],
                candidate_variant=["cand-a"],  # type: ignore[arg-type]
                suite_id=SUITE,
                criterion_index=0,
            )

    def test_a_family_of_one_is_correct_at_stage_c_and_not_warned_about(self, tmp_path: Path) -> None:
        # Holm at m = 1 is the uncorrected alpha by construction, which is the right answer for a
        # round that gated one candidate.
        gate_dirs = self._gate_arms(tmp_path, ["cand-a"])
        confirm_dirs = self._confirm_arms(tmp_path, "cand-a")

        block = self._call(tmp_path, gate_dirs, confirm_dirs, ["cand-a"])

        assert "recomputed over a family of 1" in block
        # And nothing warns about it: Holm at m = 1 IS the uncorrected alpha, which is the right
        # answer for a round that gated one candidate rather than a case to caveat.
        assert "family of 1 at alpha=0.05" in block


class TestConfirmReportExecution:
    def _gates(self, tmp_path: Path, candidates: Sequence[str]) -> dict[str, Path]:
        return {
            candidate: exec_run_dir(
                tmp_path / f"gate-{candidate}",
                incumbent=WINNER["incumbent"],
                candidate=WINNER["candidate"],
                candidate_variant=candidate,
            )
            for candidate in candidates
        }

    def _confirm(self, tmp_path: Path, candidate: str, *, split: str | None = "test", **arms) -> Path:
        return confirm_dir(
            tmp_path / f"confirm-{candidate}",
            split=split,
            candidate_variant=candidate,
            **(arms or {"incumbent": WINNER["incumbent"], "candidate": WINNER["candidate"]}),
        )

    def _call(self, gates: dict[str, Path], confirm: Path, candidate: str = "cand-a") -> str:
        return confirm_report_execution(
            gates=gates,
            confirm_run_dir=confirm,
            incumbent_variant="incumbent",
            candidate_variant=candidate,
            suite_id=EXEC_SUITE,
        )

    def test_the_recomputed_train_verdict_is_bit_identical(self, tmp_path: Path, monkeypatch) -> None:
        # Spike A's claim on this track, and through the composite: what must hold is that the
        # verdict handed to `confirm_gate_execution` is the one Stage B rendered.
        gates = self._gates(tmp_path, ["cand-a", "cand-b"])
        captured: list[ExecutionGateVerdict] = []
        real = confirm_gate_execution

        def spy(**kwargs):
            captured.append(kwargs["train_verdict"])
            return real(**kwargs)

        monkeypatch.setattr("coder_eval.optimize.api.confirm_gate_execution", spy)
        self._call(gates, self._confirm(tmp_path, "cand-a"))

        direct = holm_promote_execution(
            [
                execution_gate(
                    run_dir=gates[c],
                    incumbent_variant="incumbent",
                    candidate_variant=c,
                    suite_id=EXEC_SUITE,
                )
                for c in sorted(gates)
            ]
        )
        expected = next(v for v in direct if v.candidate_variant == "cand-a")
        assert len(captured) == 1
        assert captured[0].model_dump() == expected.model_dump()

    def test_both_blocks_render_and_the_family_size_is_stated(self, tmp_path: Path) -> None:
        gates = self._gates(tmp_path, ["cand-a", "cand-b"])

        block = self._call(gates, self._confirm(tmp_path, "cand-a"))

        assert "recomputed over a family of 2" in block
        assert "### Stage C confirm" in block
        assert "### Execution gate" in block

    def test_a_confirm_run_with_no_recorded_split_refuses_in_the_block(self, tmp_path: Path) -> None:
        # The confirm gate refuses outright if the run did not record `--split test`, and that
        # refusal has to reach the reader rather than being swallowed.
        gates = self._gates(tmp_path, ["cand-a"])

        block = self._call(gates, self._confirm(tmp_path, "cand-a", split=None))

        assert "split" in block.lower()
        assert "### Stage C confirm" in block

    def test_a_train_verdict_that_is_not_a_result_reaches_the_block_as_a_refusal(self, tmp_path: Path) -> None:
        """Rank 1's decision, not this module's.

        `confirm_train_refusal` and `confirm_train_note` exist precisely so a train verdict that did
        not promote is NAMED rather than rejected — the second's docstring says a reader may
        legitimately want to confirm a candidate that separated and was then vetoed by a guardrail.
        A rank-4 composite that raised here would be overriding that.
        """
        flat = {f"r{i}": [0.5, 0.5] for i in range(4)}
        gates = {
            "cand-a": exec_run_dir(tmp_path / "gate-flat", incumbent=flat, candidate=flat, candidate_variant="cand-a")
        }

        block = self._call(gates, self._confirm(tmp_path, "cand-a"))

        assert "the TRAIN verdict is not a result" in block
        assert "### Stage C confirm" in block, "it is a rendered refusal, not a raise"

    def test_an_empty_family_is_a_caller_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="gates is empty"):
            confirm_report_execution(
                gates={},
                confirm_run_dir=tmp_path,
                incumbent_variant="incumbent",
                candidate_variant="cand-a",
                suite_id=EXEC_SUITE,
            )

    @pytest.mark.parametrize(
        ("test_shift", "swap", "expected"),
        [
            pytest.param(0.30, False, "REPRODUCED", id="reproduced"),
            pytest.param(0.15, False, "SHRANK", id="shrank"),
            pytest.param(0.30, True, "REVERSED", id="reversed"),
        ],
    )
    def test_each_classification_renders_through_the_composite(
        self, tmp_path: Path, test_shift: float, swap: bool, expected: str
    ) -> None:
        """The three outcomes Stage C exists to tell apart, through the composite that prints them.

        The renderer's own suite pins the REVERSED rung whole; what this adds is that the composite
        reaches each rung at all. A confirm split with no priced MDE renders UNDECIDED for every
        input, so `shifted_replicate_arms` (varied per-row spreads) is what makes the ladder
        reachable — a uniform shift is refused for zero variance and pins nothing.

        `engagement_criterion_index=None`: these rows' labels derive from their scores, so the
        incumbent's low rows read `no` and the engagement check would block the train verdict. The
        same reason `test_the_confirm_block_is_unchanged` gives.
        """
        gates = {"candidate": confirm_dir(tmp_path / "gate", split="train", **shifted_replicate_arms(0.30))}
        confirm = confirm_dir(tmp_path / "confirm", split="test", **shifted_replicate_arms(test_shift, swap=swap))

        block = confirm_report_execution(
            gates=gates,
            confirm_run_dir=confirm,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            engagement_criterion_index=None,
        )

        assert headline_line(block).startswith(expected)

    def test_a_missing_gate_dir_names_the_candidate(self, tmp_path: Path) -> None:
        # The Stage B twin's guard, and it matters MORE here: a gate dir that cannot be read makes
        # the recomputation correct over a smaller `m` than the family-size line claims, and the
        # error would otherwise read "did not promote at Stage B" for a wiring fault.
        gates = self._gates(tmp_path, ["cand-a"])
        gates["cand-b"] = tmp_path / "never-ran"

        with pytest.raises(ValueError, match="no gate run directory for") as excinfo:
            self._call(gates, self._confirm(tmp_path, "cand-a"))

        assert "cand-b" in str(excinfo.value)

    def test_a_refused_arm_shrinking_the_recomputed_family_is_named(self, tmp_path: Path) -> None:
        # Worse here than at Stage B: the block STATES the family size it recomputed against, so a
        # silently smaller `m` makes that line a false claim about the threshold the winner cleared.
        run_dir = self._gates(tmp_path, ["cand-a"])["cand-a"]

        block = confirm_report_execution(
            gates={"cand-a": run_dir, "cand-b": run_dir},
            confirm_run_dir=self._confirm(tmp_path, "cand-a"),
            incumbent_variant="incumbent",
            candidate_variant="cand-a",
            suite_id=EXEC_SUITE,
        )

        assert "The Holm correction saw 1 of 2 predeclared candidate(s)" in block
        assert "recomputed over a family of 2" in block, "and the two numbers are both visible"

    def test_a_string_confirm_run_dir_is_rejected_at_the_boundary(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError, match="confirm_run_dir must be a pathlib"):
            confirm_report_execution(
                gates=self._gates(tmp_path, ["cand-a"]),
                confirm_run_dir="runs/round1-confirm",  # type: ignore[arg-type]
                incumbent_variant="incumbent",
                candidate_variant="cand-a",
                suite_id=EXEC_SUITE,
            )

    def test_stage_c_is_two_entry_points_and_neither_takes_a_track_selector(self) -> None:
        """The real invariant, not a grep for a spelling.

        A grep is decoration here: the module legitimately carries `_track_verdict(…, track_name)`,
        so `'track="' not in source` is satisfied by a rename rather than by the design holding. What
        must hold is that Stage C is TWO public entry points, each mirroring its own gate's parameter
        list — a single function would carry mutually exclusive arguments and need a runtime assert
        for a combination the split makes unrepresentable.
        """
        for name, forbidden in (
            (confirm_report_activation, {"gates", "confirm_run_dir", "engagement_criterion_index"}),
            (confirm_report_execution, {"gate_dirs", "confirm_dirs", "candidate_variants"}),
        ):
            params = set(inspect.signature(name).parameters)
            assert not params & {"track", "track_name"}, f"{name.__name__} takes a track selector"
            assert not params & forbidden, f"{name.__name__} carries the other track's parameters"
        # And both are public names on the module, so neither is reachable only through the other.
        assert {"confirm_report_activation", "confirm_report_execution"} <= set(vars(coder_eval.optimize.api))
