"""The composites in `coder_eval.optimize.api` — the surface `SKILL.md` imports.

Every assertion on a NUMBER compares against a direct call to the library function the composite
composes, never a hardcoded float: the composite's job is the guards, the fallbacks and the order,
and a literal here would pin the estimator instead — which is what `tests/lint/estimator_ledger.py`
exists to govern.

A `NoiseFloor` is deliberately never compared by `model_dump()`: `computed_at` is
`datetime.now(UTC)`, so the dump moves between two calls over the same tree.
"""

from pathlib import Path

import pytest

from coder_eval.models import (
    ACTIVATION_FLOOR_METRIC,
    EXECUTION_FLOOR_METRIC,
    NoiseFloor,
    RegressionRow,
)
from coder_eval.optimize.activation import min_discordant_rows, noise_floor_mde
from coder_eval.optimize.api import (
    activation_floor_report,
    corpus_report,
    cost_quality_report,
    discreteness_report,
    execution_floor_report,
    headroom_report,
    replicates_report,
    row_matrix_report,
)
from coder_eval.optimize.execution import measure_execution_noise_floor
from coder_eval.optimize.store import UNRESOLVED_MODEL, append_regression_rows
from coder_eval.reports_optimize import SINGLE_REPLICATE_CAVEAT
from coder_eval.reports_stats import DEFAULT_ALPHA
from tests.optimize_fixtures import (
    SUITE,
    assert_matches_render_pin,
    cost_quality_arm,
    eval_result,
    grader_result,
    scored_result,
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
_REPORTERS = ["row-matrix", "cost-quality", "corpus", "headroom", "replicates"]


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
