"""Unit tests for `coder_eval.reports_optimize` — every rendered block, and only rendering.

The renderer makes no decisions and reads no disk; that boundary is asserted in
`test_optimize_layering.py`, which is where the claim belongs. Here: the headline ladder's rungs,
the tables, and the pinned blocks.
"""

import ast
import shutil
from pathlib import Path
from typing import ClassVar

import pytest

from coder_eval.models import (
    ACTIVATION_FLOOR_METRIC,
    EXECUTION_FLOOR_METRIC,
    ActivationGateVerdict,
    ArmRowScores,
    ConfirmVerdict,
    ExecutionGateVerdict,
    ExperimentResult,
    GuardrailCheck,
    RegressionRow,
)
from coder_eval.optimize.activation import SeedStability, holm_promote
from coder_eval.optimize.execution import confirm_gate_execution, holm_promote_execution
from coder_eval.optimize.fronts import (
    CostQualityPoint,
    RuleCeiling,
    cost_quality_front,
    cost_quality_points,
    headroom_ceiling,
    instance_best_front,
    pareto_front,
)
from coder_eval.optimize.gate import GATE_MAX_FAMILY, MATERIALITY_FLOOR, build_confirm_verdict
from coder_eval.optimize.search import SearchComparison, search_compare
from coder_eval.reports_optimize import (
    CEILING_MARGIN,
    SINGLE_REPLICATE_CAVEAT,
    _front_summary,
    render_confirm_markdown,
    render_cost_quality,
    render_discreteness,
    render_execution_markdown,
    render_headroom_ceilings,
    render_markdown,
    render_noise_floor,
    render_row_matrix,
    render_row_replicates,
    render_search_comparison,
    render_seed_stability,
)
from coder_eval.reports_stats import BOOTSTRAP_RESAMPLES
from tests.optimize_fixtures import (
    EXEC_SUITE,
    FAST_RESAMPLES,
    HEADROOM_FLOOR,
    HEADROOM_ROW_SCORES,
    HEADROOM_RULE_ROWS,
    REFUSAL_RESAMPLES,
    SEARCH_HEAD_SCORES,
    SUITE,
    WINNER,
    activation_verdict,
    activation_verdict_over_arms,
    arm_row_scores_for,
    assert_matches_render_pin,
    confirm_dir,
    cost_quality_arm,
    exec_gate,
    exec_run_dir,
    failing_cost_check,
    full_execution_verdict,
    headline_line,
    module_source,
    parity_activation,
    parity_execution,
    pinned_suite,
    shared_dirs,
    shifted_replicate_arms,
    split_labelled_arms,
    tiny_suite,
    uniform_shift,
)


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


class TestRowMatrixReplicateLabelling:
    def _arm(self, name: str, **rows: float) -> ArmRowScores:
        return ArmRowScores(variant_id=name, row_scores=rows)

    def _arms(self) -> list[ArmRowScores]:
        return [self._arm("incumbent", r1=0.5, r2=0.5), self._arm("cand-a", r1=1.0, r2=0.4)]

    def test_row_matrix_unchanged_without_n_replicates(self) -> None:
        # Byte-identical to today's output when the new keyword is omitted, so the shipped call
        # site and its pinned renders are untouched. The same contract `instance_best` has.
        arms = self._arms()
        assert render_row_matrix(arms, pareto_front(arms)) == render_row_matrix(
            arms, pareto_front(arms), n_replicates=None
        )
        assert "replicate" not in render_row_matrix(arms, pareto_front(arms))

    def test_row_matrix_warns_at_single_replicate(self) -> None:
        # A Stage A matrix over one draw per cell reads exactly like a measurement. Measured: it
        # reported +0.0392 against a 0.0255 floor, and the replicated gate returned p = 0.9977.
        arms = self._arms()
        text = render_row_matrix(arms, pareto_front(arms), n_replicates=1)
        assert SINGLE_REPLICATE_CAVEAT in text
        assert "RANKS, it does not MEASURE" in text

    def test_row_matrix_no_warning_at_three(self) -> None:
        arms = self._arms()
        text = render_row_matrix(arms, pareto_front(arms), n_replicates=3)
        assert SINGLE_REPLICATE_CAVEAT not in text
        assert "mean of 3 replicate(s)" in text

    def test_the_caveat_is_declared_once_and_read_by_the_skill(self) -> None:
        # The MATERIALITY_FLOOR / COST_FRONT_ADVISORY shape: the sensor IMPORTS the constant rather
        # than retyping it, so the claim cannot exist in two files at two vintages.
        skill = (
            Path(__file__).parent.parent / "plugins" / "coder-eval" / "skills" / "optimize-skill" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "ranking device" in skill, "the skill no longer says Stage A ranks rather than measures"
        # BINDING, not two presence checks: the anchor must be in the constant AND in the prose, so
        # the skill's own retelling cannot drift to a different pair of numbers than the block
        # prints. This is the shape the MATERIALITY_FLOOR and COST_FRONT_ADVISORY sensors have.
        for anchor in ("0.9977", "0.0392", "0.0255"):
            assert anchor in SINGLE_REPLICATE_CAVEAT, f"the constant no longer carries {anchor!r}"
            assert anchor in skill, (
                f"optimize-skill retells the single-replicate evidence without {anchor!r}, so the "
                "prose and the rendered block can state different numbers for the same round"
            )


class TestRowReplicates:
    def test_row_replicates_flags_zero_variance_rows(self) -> None:
        """The most informative rows in a run, and the ones a suite mean destroys.

        The real pair: +0.238 on one row and -0.272 on another, both perfectly reproducible,
        cancelling to a suite delta of +0.0001. "The difference is noise" is the opposite of what
        happened there, and no verdict block can express it.
        """
        text = render_row_replicates(
            {"up": [0.76, 0.76, 0.76], "down": [0.86, 0.86, 0.86]},
            {"up": [1.00, 1.00, 1.00], "down": [0.59, 0.59, 0.59]},
        )
        assert "Zero variance on BOTH arms" in text
        assert "down, up" in text
        assert "+0.240" in text and "-0.270" in text

    def test_row_replicates_single_replicate_shows_undefined_not_zero(self) -> None:
        # A spread over one draw is UNDEFINED, not zero. Printing 0.0 would fire the
        # zero-variance flag on every row of a single-replicate run, where it means nothing.
        text = render_row_replicates({"r1": [0.5]}, {"r1": [1.0]})
        assert "spread" not in text
        assert "Zero variance" not in text

    def test_row_replicates_counts_dead_rows(self) -> None:
        text = render_row_replicates({"a": [0.5, 0.5], "b": [1.0, 0.0]}, {"a": [0.5, 0.5], "b": [0.0, 1.0]})
        assert "2 row(s) dead for this comparison" in text
        assert "a, b" in text

    def test_row_replicates_treats_one_armed_row_as_a_hole(self) -> None:
        # A hole is never a zero — the convention ArmRowScores and the row matrix already use. A
        # row counted at 0.0 would fabricate the largest delta in the table.
        text = render_row_replicates({"only-incumbent": [0.5]}, {})
        assert "(hole)" in text
        assert "dead for this comparison" not in text

    def test_row_replicates_reports_unequal_replicate_counts(self) -> None:
        text = render_row_replicates({"r1": [1.0, 1.0, 1.0]}, {"r1": [0.5, 0.5]})
        assert "r1 (3 v 2)" in text
        assert "shifts the comparison on its own" in text

    def test_row_replicates_with_no_rows(self) -> None:
        assert render_row_replicates({}, {}) == "_No rows to compare._"

    def test_a_zero_delta_row_is_dead_but_not_reproducible(self) -> None:
        # Both flags key on the same row and would be contradictory together: a row that did not
        # move is not a reproducible CHANGE, whatever its variance.
        text = render_row_replicates({"a": [0.5, 0.5]}, {"a": [0.5, 0.5]})
        assert "dead for this comparison" in text
        assert "Zero variance on BOTH arms" not in text


class TestRenderHeadroomCeilings:
    def _ceilings(self) -> list[RuleCeiling]:
        return [
            headroom_ceiling(HEADROOM_ROW_SCORES, rule=rule, rows=rows)
            for rule, rows in sorted(HEADROOM_RULE_ROWS.items())
        ]

    def test_render_headroom_ceilings_names_the_gap(self) -> None:
        rendered = render_headroom_ceilings(self._ceilings(), HEADROOM_FLOOR)
        # Three of the four are below the floor, and the block must say the remedy is rows.
        assert rendered.count("GAP") == 3, rendered
        assert "the remedy is ROWS, not candidates" in rendered
        assert f"{CEILING_MARGIN:g} x floor x n_rows" in rendered

    def test_unattributed_rows_are_named_as_an_under_estimate(self) -> None:
        """The one line that stops a truncated log from reading as a suite gap.

        `run_command` caps each stream at 4000 characters, so a verbose grader loses its `RULES`
        line. That row is then in no rule's failing set, its headroom is counted nowhere, and every
        ceiling is an UNDER-estimate — while `_ceiling_verdict` prints a confident GAP, the verdict
        that tells a reader to stop working on the rule. `n_dropped` cannot see it: the row IS in
        `row_scores`, it just went unattributed.
        """
        ceilings = self._ceilings()
        assert "UNDER-estimate" not in render_headroom_ceilings(ceilings, HEADROOM_FLOOR)
        rendered = render_headroom_ceilings(ceilings, HEADROOM_FLOOR, unattributed=2)
        assert "2 row(s) carried no rule attribution" in rendered
        assert "UNDER-estimate" in rendered and "truncated grader log" in rendered

    def test_render_headroom_ceilings_omits_verdict_without_a_floor(self) -> None:
        # A ceiling with no floor still ranks the rules; a fabricated floor says nothing true.
        rendered = render_headroom_ceilings(self._ceilings(), None)
        assert "GAP" not in rendered and "x floor" not in rendered
        assert "No noise floor was measured" in rendered
        assert "0.0300" in rendered, "the ceilings themselves must still print"

    def test_render_headroom_ceilings_names_dropped_rows(self) -> None:
        stale = headroom_ceiling(HEADROOM_ROW_SCORES, rule="R1", rows={"sku-labels", "a-row-that-moved"})
        assert "R1 (1)" in render_headroom_ceilings([stale], HEADROOM_FLOOR)

    def test_the_suite_level_entry_is_not_rendered_as_a_rule(self) -> None:
        rendered = render_headroom_ceilings([headroom_ceiling(HEADROOM_ROW_SCORES)], HEADROOM_FLOOR)
        assert "whole suite" in rendered and "``" not in rendered

    def test_render_headroom_ceilings_with_nothing_to_size(self) -> None:
        assert render_headroom_ceilings([], HEADROOM_FLOOR) == "_No rules to size._"

    def test_the_block_says_it_is_advisory(self) -> None:
        # The one sentence that keeps an AUTHORED attribution from reading as a gate.
        assert "never blocks a promotion" in render_headroom_ceilings(self._ceilings(), HEADROOM_FLOOR)


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


class TestEveryHeadlineRungIsReachableOnBothTracks:
    """All five rungs on BOTH tracks, one verdict each, asserted on the headline LINE.

    Folding the guardrail veto into `promoted` retires the BLOCKED rung the moment the renderer
    reads `promoted` for it — silently, because the block still renders and still says something
    plausible ("NOT PROMOTED"). The two rungs it collapses are the two a reader must never
    confuse: "it lost" and "it won and was vetoed" call for opposite next actions. The rungs are
    parametrized from one table so a future reorder cannot make one unreachable without failing
    here, and `test_blocked_and_not_promoted_differ_only_by_separated` pins the collapse itself.

    Widened from the execution track alone to a generated cross-product once both ladders became one
    `_headline` chain: the tracks differ by three strings, so a rung reachable on one and not the
    other is now a defect in the arguments rather than in a hand-written chain, and only a
    cross-product can see it. The execution-specific tests below still exercise this track's own
    verdict, because what they pin (family size, `separated` vs `promoted`) is not track-shaped.
    """

    def _activation_verdict(self, **overrides) -> ActivationGateVerdict:
        base: dict[str, object] = {
            "incumbent_variant": "incumbent",
            "candidate_variant": "cand",
            "suite_id": SUITE,
            "criterion_index": 0,
            "confidence": 0.95,
            "n_resamples": FAST_RESAMPLES,
            "rows_paired": 8,
            "rows_excluded": 0,
            "incumbent_f1": 0.4,
            "candidate_f1": 0.8,
            "mean_diff": 0.2,
            "ci_low": 0.1,
            "ci_high": 0.3,
            "p_value": 0.001,
        }
        # CE041 scans `src/` only; a test building a verdict from a base dict is the documented
        # legitimate splat.
        return ActivationGateVerdict(**{**base, **overrides})

    def _verdict(self, **overrides) -> ExecutionGateVerdict:
        base: dict[str, object] = {
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
            "p_value": 0.001,
        }
        # CE041 scans `src/` only; a test building a verdict from a base dict is the documented
        # legitimate splat.
        return ExecutionGateVerdict(**{**base, **overrides})

    # One row per rung, top of the ladder down: (id, overrides, expected per track). The overrides
    # are the fields both verdicts share, which is what makes one table serve both tracks.
    #
    # `refusal-with-a-p` is the ONE rung whose text differs, and the difference is the whole reason
    # `refusal_label` is an argument: on activation a refusal that DID compute a p is a statement
    # about the suite's RESOLUTION, and demoting it to `NOT A RESULT` would tell the user the run was
    # mis-wired. The execution track has no discreteness refusal, so it passes `NOT A RESULT` and its
    # ladder reads as four rungs.
    # `via_holm` says whether the wrapper produces this rung's state or the fixture declares it.
    # Exactly ONE rung declares it, and the reason is precise rather than blanket: `holm_promote`
    # recomputes `gate_refusal` only in its `p_value is not None` branch, so a fixture refusal
    # survives the wrapper when there is no p (the `not-a-result` rung) and cannot survive it when
    # there is (`refusal-with-a-p`). The subject here is the RENDERER's ladder, so that rung is fed
    # the post-Holm field state a real gate emits; that the state is reachable is pinned separately,
    # by the wrapper-driven tests below and by the activation gate's own discreteness-refusal suite.
    _RUNGS: ClassVar[list[tuple[str, dict, dict[str, str], bool]]] = [
        ("undecided", {}, {"activation": "UNDECIDED", "execution": "UNDECIDED"}, False),
        (
            "not-a-result",
            {"gate_refusal": "there is no experiment file", "p_value": None},
            {"activation": "NOT A RESULT", "execution": "NOT A RESULT"},
            True,
        ),
        (
            "refusal-with-a-p",
            {"gate_refusal": "the suite cannot separate at this size", "promoted": False, "holm_rejected": True},
            {"activation": "CANNOT SEPARATE AT THIS SIZE", "execution": "NOT A RESULT"},
            False,
        ),
        (
            "blocked",
            {"guardrails": [_FAILING_GUARDRAIL]},
            {"activation": "BLOCKED BY A GUARDRAIL", "execution": "BLOCKED BY A GUARDRAIL"},
            True,
        ),
        # `failed_vetoes` alone cannot tell this rung from the one above it — the primary reason here
        # is that the candidate LOST, and `separated` is the only thing that distinguishes them.
        (
            "lost-and-blocked",
            {"mean_diff": -0.2, "ci_low": -0.3, "ci_high": -0.1, "guardrails": [_FAILING_GUARDRAIL]},
            {"activation": "NOT PROMOTED", "execution": "NOT PROMOTED"},
            True,
        ),
        ("promoted", {}, {"activation": "PROMOTED", "execution": "PROMOTED"}, True),
    ]

    @pytest.mark.parametrize("track", ["activation", "execution"])
    @pytest.mark.parametrize(("rung", "overrides", "expected", "via_holm"), _RUNGS, ids=[r[0] for r in _RUNGS])
    def test_the_rung_is_reachable(
        self, track: str, rung: str, overrides: dict, expected: dict[str, str], via_holm: bool
    ) -> None:
        if track == "activation":
            verdict = self._activation_verdict(**overrides)
            rendered = render_markdown(holm_promote([verdict])[0] if via_holm else verdict)
        else:
            execution_verdict = self._verdict(**overrides)
            rendered = render_execution_markdown(
                holm_promote_execution([execution_verdict])[0] if via_holm else execution_verdict
            )
        assert headline_line(rendered).startswith(expected[track]), rung

    @staticmethod
    def _headline_assignments_inside_a_branch(function: ast.FunctionDef) -> list[int]:
        """Lines where ``function`` assigns a headline-shaped name INSIDE an `if`."""
        return [
            node.lineno
            for branch in ast.walk(function)
            if isinstance(branch, ast.If)
            for node in ast.walk(branch)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id.endswith("headline") for target in node.targets)
        ]

    @pytest.mark.parametrize("renderer", ["render_markdown", "render_execution_markdown"])
    def test_the_stage_b_headline_is_built_in_exactly_one_place(self, renderer: str) -> None:
        """The cheap sensor for the drift that already happened twice.

        Two hand-written ladders were kept in step by hand and diverged anyway: the activation
        `BLOCKED` rung once read `guardrails` alone while its twin unioned both lists, and before
        that it keyed on `promoted`, which the veto had made unsatisfiable. Neither drift deleted a
        token, so no presence sensor could see either — but both looked exactly like an `if/elif`
        chain assigning a headline at a call site, and THAT is detectable.

        An assignment from the shared helper is fine and is what both renderers do; a headline built
        inside a BRANCH is the rung being rebuilt locally. A test rather than a CE rule: two call
        sites in one file, and a rule would need a scope nothing else in the tree shares. On the AST
        rather than on text, because the chains were heavily commented and a substring scan over
        them is the fragile shape CE039 discourages.

        **The boundary, so a green run is not mistaken for a proof.** It matches an `ast.Assign` to a
        `Name` whose id ends in `headline`, inside an `ast.If`, in the two functions the parametrize
        names. Invisible to it: a rung that `return`s instead of assigning, a top-level ternary
        (which also keeps `"_headline" in calls` true), a differently-named local, and a THIRD Stage B
        renderer nobody added it to. The shape that actually drifted twice is exactly the shape it
        catches, which is the whole claim — not that no bespoke rung is expressible.
        """
        tree = ast.parse(module_source("reports_optimize"))
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == renderer)
        branched = self._headline_assignments_inside_a_branch(function)
        assert branched == [], (
            f"{renderer} builds a headline inside a branch at line(s) {branched} — the Stage B ladder "
            "is `_headline` alone, and a second chain is how the two tracks' BLOCKED rungs drifted "
            "apart once already"
        )
        calls = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_headline" in calls, (
            f"{renderer} no longer calls the shared ladder — no chain AND no headline would satisfy "
            "the assertion above while printing nothing"
        )

    def test_the_branch_scan_can_see_a_real_ladder(self) -> None:
        """Anti-vacuity, and the standing positive control is in the tree rather than fabricated.

        `render_confirm_markdown` keeps its OWN short ladder on purpose — its REVERSED rung is
        Stage-C-specific and there is exactly one confirm renderer, so folding it in would be the
        generality YAGNI forbids. That makes it the subject that proves this scan is looking at what
        it claims: a renamed local or a changed AST shape would otherwise make the two assertions
        above silently green.
        """
        tree = ast.parse(module_source("reports_optimize"))
        confirm = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "render_confirm_markdown"
        )
        assert self._headline_assignments_inside_a_branch(confirm), (
            "no headline is built inside a branch in `render_confirm_markdown` — either its ladder "
            "moved, or this scan no longer detects one and the Stage B assertions prove nothing"
        )

    def test_an_execution_refusal_with_a_p_value_still_says_not_a_result(self) -> None:
        """The collapsed-rung guard, and the one place the shared chain could change a byte.

        The execution ladder read as four rungs because it had no `CANNOT SEPARATE AT THIS SIZE`
        text — not because it skipped a rung. A refusal carrying a p must still reach rung 3 and
        print `NOT A RESULT`, exactly as it did when the chain was hand-written per track.
        """
        decided = holm_promote_execution(
            [self._verdict(gate_refusal="zero variance in the paired differences", p_value=0.001)]
        )[0]
        assert decided.p_value is not None, "the premise: this refusal DID compute a p"
        assert (
            headline_line(render_execution_markdown(decided))
            == "NOT A RESULT — zero variance in the paired differences"
        )

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
        headlines = [headline_line(render_execution_markdown(v)) for v in (blocked_arm, clean_arm)]
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
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT")

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
        [(ActivationGateVerdict, parity_activation), (ExecutionGateVerdict, parity_execution)],
        ids=["activation", "execution"],
    )
    def test_separated_is_not_a_serialized_field(self, model, build) -> None:
        # A property, never a stored field: nothing new is written, so no construction site can set
        # it inconsistently with the numbers it derives from. Asserted on BOTH verdicts — the
        # activation twin was added later, and a symmetry claim needs both halves witnessed.
        assert "separated" not in model.model_fields
        assert "separated" not in build().model_dump()


class TestRenderConfirmMarkdown:
    """The Stage C block. Only the REVERSED rung is new; the rest takes its shape from the gate ladder."""

    @staticmethod
    def _verdict(outcome: str, **overrides) -> ConfirmVerdict:
        base: dict[str, object] = {
            "incumbent_variant": "incumbent",
            "candidate_variant": "cand",
            "suite_id": EXEC_SUITE,
            "train_effect": 0.08,
            "test_effect": 0.075,
            "test_mde": 0.02,
            "delta": -0.005,
            "outcome": outcome,
            "test_verdict": full_execution_verdict(),
        }
        return ConfirmVerdict(**{**base, **overrides})

    _RUNGS: ClassVar[list[tuple[str, dict, str]]] = [
        ("refusal", {"confirm_refusal": "the confirm run recorded --split 'train'"}, "NOT A COMPARISON"),
        ("reversed", {}, "REVERSED"),
        ("shrank", {}, "SHRANK"),
        ("reproduced", {}, "REPRODUCED"),
        ("undecided", {}, "UNDECIDED"),
    ]

    @pytest.mark.parametrize(("rung", "overrides", "expected"), _RUNGS, ids=[r[0] for r in _RUNGS])
    def test_the_rung_is_reachable(self, rung: str, overrides: dict, expected: str) -> None:
        outcome = "undecided" if rung == "refusal" else rung
        block = render_confirm_markdown(self._verdict(outcome, **overrides))
        assert headline_line(block).startswith(expected)

    def test_a_refusal_is_printed_once_not_twice(self, tmp_path: Path) -> None:
        # `holm_promote`'s rule for `gate_refusal`, applied here: notes is the distrust-the-numbers
        # channel and a refusal is a headline, so a refused block must not carry both.
        reason = "the confirm run recorded --split 'train'"
        block = render_confirm_markdown(self._verdict("undecided", confirm_refusal=reason, notes=[]))
        assert block.count(reason) == 1

    def test_a_refused_verdict_carries_no_classification_note(self, tmp_path: Path) -> None:

        verdict = build_confirm_verdict(
            incumbent_variant="incumbent",
            candidate_variant="cand",
            suite_id=EXEC_SUITE,
            train_effect=0.08,
            test_effect=0.075,
            test_mde=0.02,
            test_verdict=full_execution_verdict(),
            confirm_refusal="the confirm run recorded --split 'train'",
            notes=["a qualification worth keeping"],
        )
        assert verdict.outcome == "undecided"
        assert verdict.notes == ["a qualification worth keeping"], "the refusal must not become a note"

    def test_a_refusal_outranks_every_outcome(self, tmp_path: Path) -> None:
        # Including REVERSED: a refused block is not a comparison, so no classification beneath it
        # means anything — the same precedence both gate ladders apply.
        block = render_confirm_markdown(
            self._verdict("reversed", confirm_refusal="the confirm run recorded --split 'train'")
        )
        assert headline_line(block).startswith("NOT A COMPARISON")
        assert "REVERSED" not in headline_line(block)

    def test_reversed_says_do_not_promote_in_the_headline(self) -> None:
        # A reversal is a headline, not a footnote: a reader who skims past it promotes on a number
        # that does not hold on held-out rows.
        assert "Do not promote" in headline_line(render_confirm_markdown(self._verdict("reversed")))

    def test_the_block_carries_both_effects_the_delta_and_the_margin(self) -> None:
        block = render_confirm_markdown(self._verdict("reproduced"))
        assert "Train effect (candidate - incumbent): 0.080" in block
        assert "Test effect on the held-out split: 0.075" in block
        assert "Delta: -0.005" in block and "MDE: 0.020" in block

    def test_it_names_the_family_of_one(self) -> None:
        # Or a reader looks for a Holm correction over hypotheses that were never tested.
        assert "family of ONE" in render_confirm_markdown(self._verdict("reproduced"))

    def test_it_does_not_re_render_the_carried_gate_block(self) -> None:
        # A third copy of either gate ladder is the drift this renderer's docstring is about.
        block = render_confirm_markdown(self._verdict("reproduced"))
        assert "Execution gate —" not in block
        assert "render_execution_markdown" in block, "it must say which renderer to print beside it"

    def test_every_note_is_printed(self) -> None:
        verdict = self._verdict("reproduced", notes=["something worth distrusting"])
        assert "something worth distrusting" in render_confirm_markdown(verdict)


class TestRenderExecutionMarkdown:
    def _decided(self, tmp_path: Path, **kwargs) -> ExecutionGateVerdict:
        return holm_promote_execution([exec_gate(exec_run_dir(tmp_path, **WINNER), **kwargs)])[0]

    def test_says_undecided_before_holm_has_run(self, tmp_path: Path) -> None:
        text = render_execution_markdown(exec_gate(exec_run_dir(tmp_path, **WINNER)))
        assert "UNDECIDED" in text
        assert "NOT PROMOTED" not in text

    def test_prints_the_interval_the_mde_and_every_check(self, tmp_path: Path) -> None:
        text = render_execution_markdown(self._decided(tmp_path))
        assert "candidate - incumbent, sign resolved by the tool" in text
        assert "Minimum detectable effect" in text
        assert "Integrity checks" in text and "completion_rate" in text
        assert "Guardrails" in text

    def test_a_failing_integrity_check_blocks_the_headline(self, tmp_path: Path) -> None:
        candidate = {**WINNER["candidate"], "r3": [0.6, 0.2]}
        run_dir = exec_run_dir(tmp_path, incumbent=WINNER["incumbent"], candidate=candidate)
        decided = holm_promote_execution([exec_gate(run_dir, n_resamples=FAST_RESAMPLES)])[0]
        # Unconditional on BOTH halves, so neither assertion can become a silent no-op: the check
        # vetoes the promotion, and `separated` records that the statistic itself came out — which
        # is what makes the BLOCKED headline reachable rather than an ordinary NOT PROMOTED.
        assert decided.promoted is False
        assert decided.separated is True
        text = render_execution_markdown(decided)
        # The headline, not the page — the failed-check note quotes this phrase too.
        assert headline_line(text).startswith("BLOCKED BY A GUARDRAIL")
        assert "engagement" in text

    def test_renders_a_missing_effect_size_as_a_dash(self, tmp_path: Path) -> None:
        verdict = exec_gate(exec_run_dir(tmp_path, **WINNER)).model_copy(update={"effect_size": None})
        assert "Cohen's d: —" in render_execution_markdown(verdict)

    def test_a_refused_verdict_leads_with_not_a_result(self, tmp_path: Path) -> None:
        # SEPARATE tmp dirs: `exec_run_dir` always writes `<tmp>/round1-gate` and never clears it,
        # so building both fixtures under one `tmp_path` leaves the refused arm's rows on disk for
        # the control — measured, it moved the control's `mde` from 2.8e-17 to 0.030.
        decided = holm_promote_execution([exec_gate(exec_run_dir(tmp_path / "refused", **uniform_shift(4)))])[0]
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT — ")
        # And the assertion is not a no-op: a clean fixture WITH spread headlines PROMOTED, so the
        # headline above is discriminating rather than whatever this renderer happens to print.
        assert headline_line(render_execution_markdown(self._decided(tmp_path / "winner"))) == "PROMOTED"

    def test_a_refusal_outranks_a_failing_guardrail(self, tmp_path: Path) -> None:
        # Reading a guardrail presupposes a statistic that separated, so the refusal is above it —
        # matching `render_markdown`'s precedence. Guaranteed only indirectly today (a refusal
        # forces `promoted=False`, which makes the BLOCKED rung unreachable), which is exactly why
        # it is pinned: a change that stopped forcing it would reorder the ladder silently.
        verdict = exec_gate(exec_run_dir(tmp_path, **uniform_shift(4)))
        failing = GuardrailCheck(
            name="cost (USD/row)", incumbent=1.0, candidate=3.0, relative_change=2.0, tolerance=0.25, passed=False
        )
        decided = holm_promote_execution([verdict.model_copy(update={"guardrails": [failing]})])[0]
        assert headline_line(render_execution_markdown(decided)).startswith("NOT A RESULT — ")

    def test_undecided_still_outranks_the_refusal(self, tmp_path: Path) -> None:
        # A verdict Holm never saw has no decision to refuse, so `promoted is None` wins the ladder.
        verdict = exec_gate(exec_run_dir(tmp_path, **uniform_shift(4)))
        assert verdict.gate_refusal is not None
        assert headline_line(render_execution_markdown(verdict)).startswith("UNDECIDED")

    def test_the_refusal_text_survives_the_undecided_headline(self, tmp_path: Path) -> None:
        """The message must reach the reader on EVERY render path, not only when it wins.

        The refusal replaced notes that `render_execution_markdown` printed unconditionally. Moving
        it to a headline-only channel meant a pre-Holm block over a mis-wired arm rendered a
        confident interval and four green checks with nothing anywhere saying the rows are missing
        — measured, and the exact silent-zero this module's docstring promises never happens.
        """
        run_dir = exec_run_dir(tmp_path, **WINNER)
        shutil.rmtree(run_dir / "incumbent")
        verdict = exec_gate(run_dir)
        assert verdict.promoted is None and verdict.gate_refusal is not None
        block = render_execution_markdown(verdict)
        assert headline_line(block).startswith("UNDECIDED"), "the ladder is unchanged — this is about the TEXT"
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
        run_dir = exec_run_dir(tmp_path, **WINNER)
        raw = ExperimentResult.model_validate_json((run_dir / "experiment.json").read_text(encoding="utf-8"))
        scores = {v: dict(per) for v, per in raw.per_replicate_scores.items()}
        scores["candidate"][f"{EXEC_SUITE}/r1"] = [float("nan"), 0.8]
        (run_dir / "experiment.json").write_text(
            raw.model_copy(update={"per_replicate_scores": scores}).model_dump_json(), encoding="utf-8"
        )
        verdict = exec_gate(run_dir)
        assert (verdict.mean_diff, verdict.p_value) == (None, None)
        assert verdict.gate_refusal is not None and "could not be read or parsed" in verdict.gate_refusal
        assert holm_promote_execution([verdict])[0].promoted is False


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
        cost_quality_arm(tmp_path, variant, per_row)
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
        verdict = holm_promote([activation_verdict(shared_dirs(tmp_path, *pinned_suite()))])[0]
        assert_matches_render_pin(render_markdown(verdict), "activation_gate")

    def test_the_refused_activation_block_is_unchanged(self, tmp_path: Path) -> None:
        # The discreteness refusal, which is the one refused verdict carrying no filesystem path.
        incumbent, candidate = tiny_suite(3, 3)
        run_dirs = shared_dirs(tmp_path, incumbent, candidate)
        verdicts = [activation_verdict(run_dirs, n_resamples=REFUSAL_RESAMPLES) for _ in range(2)]
        assert_matches_render_pin(render_markdown(holm_promote(verdicts)[0]), "activation_gate_refused")

    def test_the_execution_block_is_unchanged(self, tmp_path: Path) -> None:
        verdict = holm_promote_execution([exec_gate(exec_run_dir(tmp_path, **WINNER))])[0]
        assert_matches_render_pin(render_execution_markdown(verdict), "execution_gate")

    def test_the_family_of_eight_block_is_unchanged(self, tmp_path: Path) -> None:
        """The resolution note, pinned whole — and a NEW fixture, which is why it owes no ledger row.

        Every other pinned render carries a family of 1 or 2, at or below `GATE_MAX_FAMILY`, so the
        note appears in none of them and this phase modified nothing. A new fixture has no "before",
        so there is no step for `docs/REPORT_SCHEMA.md`'s `## Estimator changes` table to attribute.
        If this ever starts MODIFYING one of its siblings, the change reached further than intended.
        """
        verdicts = [exec_gate(exec_run_dir(tmp_path / f"g{i}", **WINNER)) for i in range(GATE_MAX_FAMILY + 3)]
        decided = holm_promote_execution(verdicts)[0]
        assert_matches_render_pin(render_execution_markdown(decided), "execution_gate_family8")

    def test_the_seed_stability_block_is_unchanged(self, tmp_path: Path) -> None:
        """A NEW fixture, so it owes no ledger row, and pinned on the UNSTABLE rung.

        That is the rung whose wording is load-bearing: a split decision reported as "2/3" reads like
        a result to anyone skimming, and the block exists to stop that. Built from a constructed
        `SeedStability` rather than a run, because a fixture that happens to straddle the Holm
        threshold at three particular seeds is exactly what drifts.
        """
        split = SeedStability(seeds=(0, 1, 2), promote_agreement=2, p_values=(0.02, 0.03, 0.06), p_spread=0.04)
        assert_matches_render_pin(render_seed_stability(split), "seed_stability_unstable")

    def test_the_confirm_block_is_unchanged(self, tmp_path: Path) -> None:
        """Stage C's block, pinned whole. A NEW fixture, so it owes no ledger row.

        Pinned on the REVERSED rung specifically: it is the only rung this renderer adds, and it is
        the one whose precedence matters most — a reversal that renders as a footnote is a promotion
        made on a number that does not hold.
        """
        # `engagement_criterion_index=None` on the TRAIN gate: these rows' labels derive from their
        # scores, so the incumbent's low rows read `no` and the engagement check fails — which blocks
        # the train verdict and would add a "not the Stage B winner" note to a pin whose whole subject
        # is the REVERSED rung. `TestConfirmGateExecution` covers that note on its own.
        train = holm_promote_execution(
            [
                exec_gate(
                    confirm_dir(tmp_path / "train", split="train", **shifted_replicate_arms(0.30)),
                    engagement_criterion_index=None,
                )
            ]
        )[0]
        assert train.promoted is True, "fixture drifted — the pin's train verdict must be a WINNER"
        confirm = confirm_gate_execution(
            train_verdict=train,
            confirm_run_dir=confirm_dir(tmp_path / "test", split="test", **shifted_replicate_arms(0.30, swap=True)),
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=EXEC_SUITE,
            engagement_criterion_index=None,
            n_resamples=FAST_RESAMPLES,
        )
        assert confirm.outcome == "reversed", "fixture drifted — this pin exists for the REVERSED rung"
        assert_matches_render_pin(render_confirm_markdown(confirm), "confirm_gate_reversed")

    def test_the_cross_split_refusal_block_is_unchanged(self, tmp_path: Path) -> None:
        """The fifth headline, pinned whole like its siblings rather than sampled by substring.

        The other refused pin next door is the DISCRETENESS refusal, which carries a p and keeps
        `CANNOT SEPARATE AT THIS SIZE`. This is the other refusal on the same track — no p, no
        comparison made — and the two must not converge on one block.
        """
        inc, cand = split_labelled_arms(tmp_path, "train", "test")
        verdict = holm_promote([activation_verdict_over_arms(inc, cand)])[0]
        assert_matches_render_pin(render_markdown(verdict), "activation_gate_cross_split", tmp_path=tmp_path)

    def test_the_row_matrix_is_unchanged(self) -> None:
        arms = _matrix_arms()
        block = render_row_matrix(arms, pareto_front(arms), instance_best=instance_best_front(arms))
        assert_matches_render_pin(block, "row_matrix")

    def test_the_cost_quality_table_is_unchanged(self, tmp_path: Path) -> None:
        points = _cost_quality_pin_points(tmp_path)
        assert_matches_render_pin(render_cost_quality(points, cost_quality_front(points)), "cost_quality")

    def test_the_search_comparison_is_unchanged(self) -> None:
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        # The head vector comes from `TestSearchCompare` rather than being respelled here: this pin
        # exists to witness the block that class's corpus-regression case renders, and an inlined
        # copy would silently stop mirroring it the day that vector is edited.
        comparison = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES),
            arm_row_scores_for("cand", {"r1": 0.0, "r2": 1.0, "r3": 1.0, "r4": 1.0}),
            corpus=corpus,
        )
        assert_matches_render_pin(render_search_comparison(comparison), "search_comparison")


class TestRenderSearchComparison:
    def test_an_accepted_comparison_names_both_numbers_and_the_row_count(self) -> None:
        block = render_search_comparison(
            search_compare(
                arm_row_scores_for("head", {"r1": 0.0, "r2": 1.0}), arm_row_scores_for("cand", {"r1": 1.0, "r2": 1.0})
            )
        )
        assert "ACCEPT" in block and "0.500" in block and "1.000" in block and "2" in block

    def test_a_blocked_comparison_leads_with_the_blocker(self) -> None:
        block = render_search_comparison(
            search_compare(arm_row_scores_for("head", {"r1": 1.0}), arm_row_scores_for("cand", {"other": 1.0}))
        )
        assert "sample_seed" in block
        # Read the discriminating LINE. The old form here asserted that "ACCEPT" was absent from
        # the block once the negative headline had been stripped out of it — vacuous on this
        # fixture, whose headline is CANNOT COMPARE: the strip removed nothing, so the absence
        # assertion could not fail while reading as a strong guard.
        #
        # Not `headline_line`: that helper returns the first line starting with `**`, and
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
            arm_row_scores_for("head", {"r1": 1.0, "r2": 0.0, "r3": 0.0, "r4": 0.0}),
            arm_row_scores_for("cand", {"r1": 0.0, "r2": 1.0, "r3": 1.0, "r4": 1.0}),
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
        block = render_search_comparison(
            search_compare(arm_row_scores_for("head", {"r1": 0.0}), arm_row_scores_for("cand", {"r1": 1.0}))
        )
        assert "not a promotion" in block.lower()


class TestCrossSplitRendering:
    """The refusal must reach the reader whatever state the verdict is in."""

    @staticmethod
    def _refused(tmp_path: Path):
        return activation_verdict_over_arms(*split_labelled_arms(tmp_path, "train", "test"))

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
        incumbent, candidate = tiny_suite(positives=3, distractors=3)
        dirs = shared_dirs(tmp_path, incumbent, candidate)
        decided = holm_promote([activation_verdict(dirs, n_resamples=2_000) for _ in range(2)])
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
        dirs = shared_dirs(tmp_path, rows, rows)
        (decided,) = holm_promote([activation_verdict(dirs, n_resamples=2_000)])
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
        dirs = shared_dirs(tmp_path, rows, rows)
        verdict = activation_verdict(dirs, criterion_index=9, n_resamples=FAST_RESAMPLES)

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


class TestPromotionIsNotOverstated:
    """Two ways the rendered block could claim more than the tool decided."""

    # The same separating verdict the cross-track parity class builds. It was a byte-identical
    # second copy of that base dict, which is the duplication `_TRACKS` exists to remove.
    _verdict = staticmethod(parity_activation)

    def test_an_interval_containing_zero_never_promotes(self) -> None:
        # Holm can reject at a corrected alpha while the reported interval still contains zero.
        # The method file states the rule as "the interval excludes zero", so the code must make
        # that literally true rather than approximately true.
        decided = holm_promote([self._verdict(ci_low=-0.05, ci_high=0.6)])[0]
        assert decided.promoted is False
        assert any("still contains zero" in note for note in decided.notes)

    def test_a_failed_guardrail_never_renders_as_promoted(self) -> None:
        decided = holm_promote([self._verdict(guardrails=[failing_cost_check()])])[0]
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
        assert headline_line(text).startswith("BLOCKED BY A GUARDRAIL —")
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
        assert headline_line(render_markdown(decided)) == "PROMOTED"

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
            assert headline_line(render_markdown(verdict)) == "NOT PROMOTED"

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
        assert headline_line(text) == "PROMOTED"


class TestRenderNoiseFloor:
    """The preflight both tracks open with, and the one block whose ABSENCE is the message.

    A bare `None` beside a floor of `0.000` is indistinguishable to anyone not reading the source,
    and the two say opposite things — a deterministic suite that measures no noise, against a
    sample that could not support the measurement. Both are pinned whole.
    """

    def test_a_measured_floor_is_pinned(self) -> None:
        assert_matches_render_pin(render_noise_floor(0.0255, metric=ACTIVATION_FLOOR_METRIC), "noise_floor")

    def test_an_unavailable_floor_names_the_precondition_and_is_pinned(self) -> None:
        block = render_noise_floor(
            None,
            metric=EXECUTION_FLOOR_METRIC,
            reason="only 1 of 4 row(s) carry 2+ replicates with a weighted_score",
        )
        assert_matches_render_pin(block, "noise_floor_unavailable")

    def test_a_sampled_floor_states_the_shape_it_was_measured_over_and_is_pinned(self) -> None:
        block = render_noise_floor(0.0500, metric=EXECUTION_FLOOR_METRIC, n_rows=4, n_replicates=2)
        assert_matches_render_pin(block, "noise_floor_sampled")

    def test_a_zero_floor_is_a_reading_rather_than_an_absence(self) -> None:
        # A deterministic instrument whose replicates agree measures no noise. It must NOT read like
        # the absence block — but "0.0000" alone reads as "this suite can resolve anything", and one
        # of the three ways to produce it (a criterion index pointing at something already perfect)
        # has actually happened on the bundled outcome template. So the zero gets its own sentence.
        block = render_noise_floor(0.0, metric=EXECUTION_FLOOR_METRIC)
        assert "0.0000" in block
        assert "No noise floor" not in block
        assert "A floor of exactly zero is a real answer" in block
        assert "already perfect on every row" in block

    def test_a_non_zero_floor_carries_no_zero_caveat(self) -> None:
        # The caveat must not fire on every block, or it stops being read.
        assert "exactly zero" not in render_noise_floor(0.0255, metric=EXECUTION_FLOOR_METRIC)

    def test_an_unavailable_floor_with_no_recorded_cause_says_so(self) -> None:
        # The execution estimator threads no `reasons` sink, so this path is reachable in the tree.
        # "No cause was recorded" is honest; an empty line where the cause goes is not.
        block = render_noise_floor(None, metric=EXECUTION_FLOOR_METRIC)
        assert "No cause was recorded." in block


class TestRenderDiscreteness:
    """The requirement a reader is most likely to answer with the wrong lever."""

    def test_an_achievable_count_is_pinned(self) -> None:
        block = render_discreteness(4, rows=12, survivors=3, threshold=0.01667)
        assert_matches_render_pin(block, "discreteness")

    def test_an_unachievable_threshold_is_pinned(self) -> None:
        block = render_discreteness(None, rows=8, survivors=1_000, threshold=0.00005)
        assert_matches_render_pin(block, "discreteness_unachievable")

    def test_both_blocks_refuse_the_row_lever(self) -> None:
        # The one sentence that must survive any rewording: adding rows the arms AGREE on makes the
        # floor worse, so "buy rows" is advice that can leave a reader further from a promotion.
        achievable = render_discreteness(4, rows=12, survivors=3, threshold=0.01667)
        unachievable = render_discreteness(None, rows=8, survivors=5, threshold=0.01)
        assert "not a row count" in achievable
        assert "AGREE on makes the floor worse" in achievable
        assert "buying rows cannot fix this one" in unachievable
