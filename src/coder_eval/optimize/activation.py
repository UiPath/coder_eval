"""The activation track, end to end: preflight, gate, and the family decision.

Rank 2 of the optimize family, beside :mod:`coder_eval.optimize.execution` and importing nothing
from it. Whether a candidate skill DESCRIPTION beats the incumbent at getting the skill engaged —
measured as ``f1.yes`` over a ``skill_triggered`` criterion, with a paired cluster bootstrap over
rows.

What is here and not on the other track: the discreteness floor (a resample drawing no discordant
row produces a difference of exactly 0.0, which bounds the smallest p this suite can express), the
sibling-annexation checks, and the cross-split refusal — the two arms are separate ``coder-eval
run`` invocations, so they genuinely can have scored different rows.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from coder_eval.models import (
    TARGET_LABEL,
    ActivationGateVerdict,
    ClassificationCriterionResult,
    ConfirmVerdict,
    EvaluationResult,
    GuardrailCheck,
    NoiseFloor,
    OptimizeMeasurements,
)
from coder_eval.optimize.gate import (
    GATE_RESAMPLES,
    MATERIALITY_FLOOR,
    NOTE_CI_CONTAINS_ZERO,
    FamilyFacts,
    FirstCause,
    TrackDecision,
    build_confirm_verdict,
    classification_metric,
    confirm_one_candidate,
    confirm_split_check,
    confirm_train_note,
    confirm_train_refusal,
    cost_latency_guardrails,
    decide_family,
    floor_from_clusters,
    floor_preflight,
    no_floor,
    note_check_failed,
    note_ordinary_negative,
)
from coder_eval.optimize.load import (
    balance_pair,
    format_splits,
    label_pairs,
    load_and_pair,
    pool_replicates,
    read_split_provenance,
    reconcile_arms,
    require_valid_criterion_index,
    split_mismatch_reason,
    stale_locations,
)
from coder_eval.optimize.store import UNRESOLVED_MODEL
from coder_eval.reports_stats import (
    DEFAULT_ALPHA,
    bootstrap_p_floor,
    cluster_bootstrap_diff_ci,
)


logger = logging.getLogger(__name__)

# What "at or near the bootstrap's resolution floor" means: a p within this multiple of the
# estimator's own 2/(m+1) is close enough that the DRAW COUNT, not the data, is plausibly deciding
# it — so the verdict says so and tells the reader to re-run with more draws before believing
# either answer. A suppression threshold on a warning, never a gate: nothing about a promotion
# turns on it. Named because a bare literal silently decides whether that warning fires at all.
NEAR_FLOOR_MULTIPLE = 5.0


def _f1_yes(pairs: list[tuple[str, str]]) -> float:
    return classification_metric(pairs, f"f1.{TARGET_LABEL}")


def _discreteness_floor(n_rows: int, n_discordant: int, n_resamples: int) -> float:
    """The smallest two-sided p this suite at this size can be EXPECTED to produce.

    A resample that draws NONE of the discordant rows gives both arms a byte-identical pooled
    pair multiset, so the difference is exactly 0.0 and the draw counts in BOTH tails. That
    happens with probability ``(1 - R/M)**M``, and the two-sided p doubles it. Floored in turn
    by the estimator's own ``2/(n_resamples+1)``, since a bound below the arithmetic's own
    resolution is not a real bound (via :func:`reports_stats.bootstrap_p_floor`, never a second
    copy of ``2/(m+1)``).

    **It bounds the p's EXPECTATION, not every realization** — and the difference decides how
    the caller must use it. Measured across 30 seeds, a realized p falls below this value about
    half the time (6-row perfect candidate at 20,000 draws: 16/30), exactly as a bound on the
    mean predicts. So a suite whose floor exceeds its Holm threshold must be REFUSED rather
    than allowed to promote on whichever draw happened to dip; a p below what the suite can be
    expected to express is Monte-Carlo noise, not evidence.

    Tight in practice, which is what makes refusing on it safe: against the real machinery it
    is within 0.5% of the measured mean at 6 and 8 rows, for both perfect and imperfect
    candidates (6-row, 3 discordant, perfect candidate: predicts 0.03125, measures 0.03095;
    6-row, 2 discordant: predicts 0.17558, measures 0.17538).
    """
    if n_rows <= 0:
        return 1.0
    concordant_fraction = max(0.0, min(1.0, 1.0 - n_discordant / n_rows))
    return min(1.0, max(bootstrap_p_floor(n_resamples), 2.0 * concordant_fraction**n_rows))


def min_discordant_rows(n_rows: int, threshold: float, n_resamples: int = GATE_RESAMPLES) -> int | None:
    """The smallest discordant-row count whose discreteness floor clears ``threshold``.

    ``None`` when no count ``R <= n_rows`` clears it. There is exactly one way that happens on a
    non-empty suite: at ``R == n_rows`` the analytic term is 0, so the floor collapses to the
    estimator's own ``2/(n_resamples+1)`` — a number that depends on the draw count and on nothing
    about the suite. A caller handed ``None`` should therefore raise ``n_resamples`` or shrink the
    family, never buy rows. A suite of ``n_rows <= 0`` is ``None`` for the separate and more
    mundane reason that there is no count of discordant rows to return.

    **The row count is not the lever, and that is the whole reason this function exists.** Holding
    ``R`` fixed and adding rows makes ``2*(1 - R/M)**M`` RISE toward ``2*e**(-R)``: at ``R = 3`` the
    floor is 0.047 over 8 rows, 0.056 over 10 and 0.078 over 20, so a user told to "add rows" after
    a refusal can buy rows and end up strictly worse off. Only rows the two arms actually DISAGREE
    on lower it. This is what the refusal quotes and what the skill's pre-spend sizing rule prints,
    so the number a reader acts on is computed rather than typed into prose.

    Calls :func:`_discreteness_floor` rather than restating ``2*(1 - R/M)**M``. One declaration of
    the floor is the same rule CE040 enforces for the estimator's — a consumer that spelled it
    inline would keep answering from the old formula after the floor moved.
    """
    for n_discordant in range(1, n_rows + 1):
        if _discreteness_floor(n_rows, n_discordant, n_resamples) <= threshold:
            return n_discordant
    return None


def _holm_threshold(family_p_values: list[float], p: float, alpha: float) -> float:
    """The Holm threshold ``p`` is decided against, given its rank in the family.

    One declaration, called by both the negative-result note and the refusal check, so the two
    can never disagree about the bar.

    Ties take the FIRST occurrence's rank (``sorted(...).index``), which hands every tied verdict
    the strictest threshold ``alpha/S``. Four identical candidates is the measured real case, and
    conservative is the right direction for a refusal — a suite refused on the strictest rank
    would also have been refused on a looser one.
    """
    return alpha / max(1, len(family_p_values) - sorted(family_p_values).index(p))


def noise_floor_mde(
    *,
    run_dirs: Sequence[Path],
    variant_id: str,
    suite_id: str,
    criterion_index: int,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = GATE_RESAMPLES,
    measurements: OptimizeMeasurements | None = None,
    model: str | None = None,
    reasons: list[str] | None = None,
) -> float | None:
    """The smallest F1 difference this suite at this size can resolve — the minimum detectable effect.

    The same machinery run against ONE arm: split its invocations in half, treat the halves as two
    arms, and bootstrap their F1 difference. The true difference there is zero by construction, so
    the interval's half-width is the noise floor. Only the incumbent supplies such a null comparison,
    which is why the gate computes it from the incumbent's run dirs.

    Returns ``None`` — never a fabricated number — with fewer than 2 invocations or fewer than 2
    rows scored in both halves. An odd invocation count splits unevenly (3 → 2/1), which widens the
    interval and therefore reports a CONSERVATIVE floor: the safe direction.

    Pass ``measurements`` and ``model`` together to reuse a stored floor instead of recomputing.
    ``model`` comes from :func:`resolve_model` and from nothing else; a ``None`` model never
    caches and never matches, so a mixed-model suite always recomputes.

    Pass ``reasons`` to find out WHY a ``None`` came back. It is a list this appends to, not a
    changed return type, and that is deliberate: this function is public and imported by the
    skill's inline snippets, so widening ``float | None`` would break a user's terminal. The
    parameter is keyword-only and defaults to ``None``, so every existing caller is untouched.
    FIVE causes are reachable through here — fewer than 2 invocations, a run tree holding results
    no invocation recorded, a wrong variant/suite/run-dir path, run dirs recording different
    ``--split`` values, and fewer than 2 rows scored in both halves — and the hardcoded sentence
    ``activation_gate`` used to print named only the first.

    **At most ONE reason is recorded per call**, because every ``no_floor`` call site is a
    ``return``: the first cause to fire ends the function. So a caller reads ``reasons[0]`` and a
    fresh list per call is the intended use. Reusing one list across arms would silently keep the
    first arm's cause in front of the second arm's, which the five-cause list above might otherwise
    suggest is an accumulation. (:func:`floor_from_clusters` records a
    sixth, the bootstrap declining, which both floors' own ``< 2`` guards make unreachable from
    them; it is defence in depth for a direct caller, and it forwards the sink so a future path
    that does reach it is not silent.)

    To RECORD what this measured, call :func:`measure_noise_floor` instead — it returns the whole
    keyed record, including the row count, which this function does not expose.
    """
    require_valid_criterion_index(criterion_index)
    measured = measure_noise_floor(
        reasons=reasons,
        run_dirs=run_dirs,
        variant_id=variant_id,
        suite_id=suite_id,
        criterion_index=criterion_index,
        confidence=confidence,
        seed=seed,
        n_resamples=n_resamples,
        measurements=measurements,
        model=model or UNRESOLVED_MODEL,
    )
    return measured.mde if measured is not None else None


def measure_noise_floor(
    *,
    run_dirs: Sequence[Path],
    variant_id: str,
    suite_id: str,
    criterion_index: int,
    model: str,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = GATE_RESAMPLES,
    measurements: OptimizeMeasurements | None = None,
    reasons: list[str] | None = None,
) -> NoiseFloor | None:
    """The noise floor as a fully-keyed record, ready to hand to :func:`record_noise_floor`.

    Returns everything the cache keys on — including ``n_rows``, the count of rows scored in BOTH
    halves of the split, which is smaller than the suite whenever a row errored. That number is
    why this function exists: a caller that recorded the suite's row count instead would miss its
    own cache entry forever, silently.

    ``None`` — never a fabricated number — with fewer than 2 invocations or fewer than 2 rows
    scored in both halves. An odd invocation count splits unevenly (3 → 2/1), which widens the
    interval and therefore reports a CONSERVATIVE floor: the safe direction.

    Pass ``measurements`` to reuse a stored floor rather than recomputing; the lookup happens after
    the rows are loaded, because the row count is part of the key. Loading is cheap, the bootstrap
    is not.

    Pass ``reasons`` — a fresh list — to find out WHY a ``None`` came back; at most one reason is
    recorded, since every refusal here is a ``return``. See :func:`noise_floor_mde`, which forwards
    it, for the five causes and for why this is a sink rather than a widened return type.
    """
    require_valid_criterion_index(criterion_index)
    if len(run_dirs) < 2:
        return no_floor(
            f"the null split needs at least 2 invocations of {variant_id!r}, got {len(run_dirs)}", reasons=reasons
        )

    # The three guards both floors open with, in the one order that is correct — `floor_preflight`
    # owns the order and the messages. The two guards ABOVE it are this track's own:
    # `require_valid_criterion_index`, and the invocation count that makes a null split possible.
    preflight = floor_preflight(
        run_dirs=run_dirs,
        variant_id=variant_id,
        suite_id=suite_id,
        split_label="the null split",
        reasons=reasons,
    )
    if preflight is None:
        return None
    per_dir, provenance = preflight

    midpoint = (len(per_dir) + 1) // 2
    first, second = pool_replicates(per_dir[:midpoint]), pool_replicates(per_dir[midpoint:])

    shared = sorted(set(first) & set(second))
    clusters_a = [label_pairs(first[rid], criterion_index) for rid in shared]
    clusters_b = [label_pairs(second[rid], criterion_index) for rid in shared]
    scored = [(a, b) for a, b in zip(clusters_a, clusters_b, strict=True) if a and b]
    if len(scored) < 2:
        return no_floor(
            f"only {len(scored)} row(s) of {suite_id!r} scored a classification result at criterion "
            + f"{criterion_index} in BOTH halves of the invocation split — an interval needs 2",
            reasons=reasons,
        )

    probe = NoiseFloor(
        suite_id=suite_id,
        variant_id=variant_id,
        model=model,
        metric=f"f1.{TARGET_LABEL}",
        criterion_index=criterion_index,
        n_rows=len(scored),
        n_invocations=len(run_dirs),
        confidence=confidence,
        seed=seed,
        n_resamples=n_resamples,
        # Derived from the run dirs, never a caller argument. A defaulted `split=` parameter
        # would reintroduce the bug this closes on the first snippet that forgot to pass it —
        # unlike `model`, whose caller at least resolved it and can see it is unresolved.
        split=provenance.value,
        mde=0.0,
        computed_at=datetime.now(UTC),
    )
    return floor_from_clusters(
        [a for a, _b in scored], [b for _a, b in scored], _f1_yes, probe, measurements, reasons=reasons
    )


def derive_sibling_indices(*rows_maps: dict[str, list[EvaluationResult]], primary_index: int) -> list[int]:
    """Every position holding a ``ClassificationCriterionResult``, except the primary.

    **Varargs, not one merged mapping.** ``activation_gate`` holds the two arms separately, and
    ``{**incumbent_rows, **candidate_rows}`` would silently drop the incumbent's result list for
    every row id present in both — which is every row in the common case, turning the intended
    union into a candidate-only view. Taking both maps and unioning the derived index sets is the
    fix, and it means a criterion present on one arm only is still found (and then reported by
    ``_sibling_checks``' existing one-sided-arm note rather than blamed on the candidate).

    Positions are ABSOLUTE positions in ``success_criteria_results``, so a non-classification
    criterion sitting between two classification ones does not shift the ones after it. Counting
    classification criteria instead is the implementation that gets that case wrong.

    A single-criterion suite — the shipped ``activation.yaml`` — derives ``[]``, which is the
    common case and is silent rather than noted: there is no sibling, so there is nothing to say.

    ``primary_index`` is validated for the same reason every other index on this module's public
    surface is: negative indexing does not fail, and here it fails *quietly the other way* —
    ``found - {-1}`` removes nothing, so the gated criterion stays in its own sibling set and
    ``_sibling_checks`` grades the candidate against the very criterion under test. Unreachable
    through ``activation_gate``, which validates first; reachable by anyone calling this directly,
    which the parameter's different NAME makes easy to overlook.
    """
    require_valid_criterion_index(primary_index)
    found: set[int] = set()
    for rows in rows_maps:
        for results in rows.values():
            for result in results:
                found |= {
                    index
                    for index, criterion_result in enumerate(result.success_criteria_results)
                    if isinstance(criterion_result, ClassificationCriterionResult)
                }
    return sorted(found - {primary_index})


def _balanced_sibling_pairs(
    incumbent_rows: dict[str, list[EvaluationResult]],
    candidate_rows: dict[str, list[EvaluationResult]],
    paired_row_ids: Sequence[str],
    index: int,
) -> list[tuple[list[tuple[str, str]], list[tuple[str, str]]]]:
    """Per row, the two arms' label pairs at ``index``, TRIMMED to a common replicate count.

    The same balancing :func:`activation_gate` applies to the primary criterion, for the same
    reason and now on the same footing: a row's weight in an arm's recall is its observation count,
    so an arm that contributed 3 replicates for a row while the other contributed 2 has silently
    reweighted the comparison. Measured on the sibling check before this existed — two arms with
    byte-identical labels on every row and every replicate read `recall.yes` 0.5 against 0.6 purely
    from one row's extra replicate, and the sibling check is folded into ``promoted``.

    Kept PER ROW rather than flattened, because the annexation rate has to pair the two arms'
    observations positionally: flattening first lets one unbalanced row shift the alignment of
    every row after it.
    """
    per_row: list[tuple[list[tuple[str, str]], list[tuple[str, str]]]] = []
    for row_id in paired_row_ids:
        incumbent = label_pairs(incumbent_rows.get(row_id, []), index)
        candidate = label_pairs(candidate_rows.get(row_id, []), index)
        per_row.append(balance_pair(incumbent, candidate))
    return per_row


def _annexation_rate(per_row: list[tuple[list[tuple[str, str]], list[tuple[str, str]]]]) -> float | None:
    """Of the sibling's true-``yes`` observations, the fraction the candidate alone turned to ``no``.

    A READING, not a gate — the same standing the cost/quality front has (see
    :data:`COST_FRONT_ADVISORY`). ``_sibling_checks`` still passes or fails on the recall drop
    alone; this number says how much of the sibling's territory the candidate took, which a recall
    delta alone does not convey when the incumbent was already missing some of it.

    ``None`` when the sibling has no true instances, since there is then nothing to annex and a
    rate over an empty denominator would read as 0.0 — indistinguishable from "took nothing".

    Takes the pairs **per row and already balanced** (:func:`_balanced_sibling_pairs`) because the
    two arms' observations are matched positionally *within* a row. Over one flattened list an
    unbalanced row shifts every later row's alignment: measured, a candidate that annexed half the
    sibling's true rows rendered a rate of 0.000 — "took nothing" — which is worse than no reading.
    """
    annexed = 0
    total = 0
    for incumbent_pairs, candidate_pairs in per_row:
        for (expected, incumbent_observed), (_e, candidate_observed) in zip(
            incumbent_pairs, candidate_pairs, strict=True
        ):
            if expected != TARGET_LABEL:
                continue
            total += 1
            if incumbent_observed == TARGET_LABEL and candidate_observed != TARGET_LABEL:
                annexed += 1
    return annexed / total if total else None


def _sibling_checks(
    *,
    incumbent_rows: dict[str, list[EvaluationResult]],
    candidate_rows: dict[str, list[EvaluationResult]],
    paired_row_ids: list[str],
    sibling_indices: Sequence[int],
    tolerance: float = 0.0,
) -> list[GuardrailCheck]:
    """`recall.yes` must not drop on any sibling criterion.

    Recall rather than precision, deliberately: a candidate that wins by annexing a sibling's
    requests makes the sibling's criterion ``expected=yes, observed=no`` — a false negative, which
    moves recall. Precision is blind to it right up until the sibling has lost everything.

    ``tolerance`` is how far recall may fall before the check fails; it defaults to 0.0, so any
    measured drop blocks promotion.

    A sibling with no true instances reads 0.0 on both arms; ``0.0 -> 0.0`` is not a regression and
    is reported as a pass with a note rather than a phantom drop. A sibling that produced results
    on ONE arm only is a wiring difference between the snapshots, not a regression of the
    candidate's making, so it passes with a note that says which arm is missing.
    """
    checks: list[GuardrailCheck] = []
    metric_name = f"recall.{TARGET_LABEL}"
    for index in sibling_indices:
        # PRESENCE is read from the untrimmed pools — "this arm produced no results here at all" is
        # an arm-level fact, and balancing would erase it by trimming a one-sided row to nothing.
        # The METRICS are read from the balanced ones, so an extra replicate cannot move recall.
        raw_incumbent = [p for rid in paired_row_ids for p in label_pairs(incumbent_rows.get(rid, []), index)]
        raw_candidate = [p for rid in paired_row_ids for p in label_pairs(candidate_rows.get(rid, []), index)]
        per_row = _balanced_sibling_pairs(incumbent_rows, candidate_rows, paired_row_ids, index)
        incumbent_pairs = [p for inc, _c in per_row for p in inc]
        candidate_pairs = [p for _i, cand in per_row for p in cand]
        incumbent_recall = classification_metric(incumbent_pairs, metric_name)
        candidate_recall = classification_metric(candidate_pairs, metric_name)

        note: str | None = None
        one_sided = bool(raw_incumbent) != bool(raw_candidate)
        if not raw_incumbent and not raw_candidate:
            note = f"criterion {index} produced no classification results on either arm — not evaluated"
        elif one_sided:
            missing = "candidate" if raw_incumbent else "incumbent"
            note = (
                f"criterion {index} produced results on one arm only (the {missing} arm has none) — that is a "
                + "difference between the snapshots, not a regression, so it is reported rather than gated on"
            )
        elif incumbent_recall == 0.0 and candidate_recall == 0.0:
            note = f"{metric_name} is 0.0 on both arms — nothing to regress"

        checks.append(
            GuardrailCheck(
                name=f"sibling {metric_name} [criterion {index}]",
                incumbent=incumbent_recall if raw_incumbent else None,
                candidate=candidate_recall if raw_candidate else None,
                relative_change=(
                    (candidate_recall - incumbent_recall) / incumbent_recall
                    if incumbent_recall and not one_sided
                    else None
                ),
                tolerance=tolerance,
                # A READING beside the recall, never a second gate: `passed` below is unchanged.
                rate=None if one_sided else _annexation_rate(per_row),
                passed=one_sided or not raw_incumbent or candidate_recall >= incumbent_recall - tolerance,
                note=note,
            )
        )
    return checks


def _activation_preflight(
    *,
    incumbent_run_dirs: Sequence[Path],
    candidate_run_dirs: Sequence[Path],
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
) -> tuple[str | None, list[str]]:
    """Both row-selection preflights: a refusal message, or notes to carry forward.

    Returns a refusal STRING rather than a verdict, because building one needs
    ``scored_row_ids``, ``rows_excluded`` and the caller's ``notes`` list — none of which this
    function has, and all of which ``_refuse_activation`` closes over. The caller extends its notes
    and then refuses.

    **Refusal PRECEDENCE is program order and is load-bearing.** The cross-split check returns
    before tree reconciliation runs, so a pair that is both cross-split AND contaminated reports the
    cross-split cause — the more specific one, and the one whose remedy ("re-run both arms under one
    --split") a reader can act on without first understanding the other. The missing-provenance note
    is likewise appended BEFORE the reconciliation loop, so a block carries its caveats in the order
    the checks ran.
    """
    notes: list[str] = []

    # --- Row-selection preflight, BEFORE any bootstrap ---------------------------------
    #
    # The two arms are separate `coder-eval run` invocations, so they can genuinely have scored
    # different row sets. Pairing a train run against a test run is not a weak comparison, it is
    # not a comparison at all — the arms never saw the same rows — so this refuses outright
    # rather than reporting a number with a caveat.
    #
    # This asymmetry with `execution_gate` (which only NOTES the split) is a consequence of the
    # data sources, not drift: that track takes ONE run_dir holding both variants, so both arms
    # share one run.json and one split by construction and a cross-split pair is unrepresentable
    # there.
    # SCOPE: `--split` only, and the message says so rather than claiming "row selections".
    # `run.json` also records `max_rows` / `sample_per_stratum`, and those are NOT compared here.
    # The gap is real but narrower: a `--sample` draw is fixed-seed, so two arms at DIFFERENT
    # counts do score largely disjoint rows — however that shows up downstream as a small
    # `rows_paired` beside a large `rows_excluded`, which the block already reports, whereas a
    # split mismatch can leave both arms fully paired on rows that merely happen to share ids.
    # Widening the comparison is a behaviour change beyond what this preflight was scoped to and
    # is recorded in `.claude/harness-candidates.md` rather than smuggled in here.
    incumbent_provenance = read_split_provenance(incumbent_run_dirs)
    candidate_provenance = read_split_provenance(candidate_run_dirs)
    union = incumbent_provenance.recorded | candidate_provenance.recorded
    if len(union) > 1:
        splits = format_splits(union)
        return (
            f"the two arms recorded DIFFERENT --split values ({splits}) — "
            f"{incumbent_variant!r} over {', '.join(str(d) for d in incumbent_run_dirs)} and "
            f"{candidate_variant!r} over {', '.join(str(d) for d in candidate_run_dirs)}. "
            "They did not score the same rows, so their difference is not an effect. Re-run both "
            "arms under one --split before gating."
        ), notes
    # A missing provenance cannot RULE OUT a cross-split pair, so it is said out loud rather than
    # passed over in silence — the one state where the fault is undetectable must not also be the
    # one state that says nothing. Not a refusal: old run dirs stay gatable.
    missing = incumbent_provenance.unrecorded + candidate_provenance.unrecorded
    total_dirs = len(incumbent_run_dirs) + len(candidate_run_dirs)
    if missing:
        # "directories" unconditionally: `load_and_pair` does NOT return early on an empty arm
        # (it notes zero rows and continues), so `total_dirs == 1` is reachable via an empty arm.
        # A plural on a count of one is a cosmetic wart; a comment claiming an invariant that does
        # not hold is worse, so this says which it is.
        notes.append(
            f"row-selection provenance is missing from {missing} of {total_dirs} run directories "
            + "(they predate the run.json `row_selection` field, or it could not be read), so a "
            + "cross-split pair cannot be ruled out for this comparison."
        )

    # --- Tree reconciliation, the OTHER half of the same preflight ---------------------
    #
    # The refusal above compares what the two run.jsons SAY. This one asks whether either run.json
    # describes the tree it sits on. A reused `--run-dir` defeats the first check completely: the
    # second invocation rewrites `row_selection` to a single split while the first split's results
    # stay on disk, so provenance reads clean and the gate pools both splits into one arm. See
    # `reconcile_tree_against_run_json` for why this matches (row, replicate) rather than counting.
    stale, unknown_dirs = reconcile_arms(
        ((incumbent_variant, incumbent_run_dirs), (candidate_variant, candidate_run_dirs)), suite_id
    )
    if stale:
        locations = stale_locations(stale)
        refusal = (
            "the run directory tree holds results that no recorded invocation wrote — "
            f"{locations}. run.json is written per INVOCATION while the tree is APPEND-ONLY, so a "
            "re-used --run-dir leaves an earlier call's results behind while `row_selection` is "
            "rewritten to describe only the latest one. Both arms live under the same run dir, so "
            "those results pair on BOTH sides and nothing else flags them. Re-run both arms into "
            "a fresh --run-dir before gating."
        )
        if unknown_dirs:
            # Otherwise the totals above silently exclude a directory that could not be checked at
            # all, and the note that would have said so is unreachable past this return.
            refusal += (
                f" ({unknown_dirs} further run directory/directories record no `task_results` and "
                "could not be reconciled either way.)"
            )
        return refusal, notes
    if unknown_dirs:
        # Same stance as the missing-split note above, and for the same reason: the one state
        # where contamination is undetectable must not also be the one state that refuses
        # everything. (`reconcile_tree_against_run_json` records the second, quieter version of
        # the same limit: an `aggregate`-rebuilt run.json launders contamination.)
        notes.append(
            f"{unknown_dirs} of {total_dirs} run directories record no `task_results`, so their "
            + "run.json cannot be reconciled against the results on disk — a re-used --run-dir "
            + "pooling an earlier invocation's results into this comparison cannot be ruled out."
        )
    return None, notes


def activation_gate(
    *,
    incumbent_run_dirs: Sequence[Path],
    candidate_run_dirs: Sequence[Path],
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    criterion_index: int,
    sibling_indices: Sequence[int] | None = None,
    materiality: float = MATERIALITY_FLOOR,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = GATE_RESAMPLES,
) -> ActivationGateVerdict:
    """Gate ONE candidate against the incumbent with a paired cluster bootstrap over rows.

    Each row is a cluster carrying all of its per-invocation label pairs. One draw samples rows
    with replacement, pools each drawn row's replicates, and recomputes ``f1.yes`` per arm through
    the criterion layer's routine; the CI is over ``candidate - incumbent``.

    ``criterion_index`` is the criterion's **position** in the suite's ``success_criteria`` list
    (0-based, counting from the top of the YAML).

    ``sibling_indices`` has three states, and ``None`` and ``()`` are no longer the same thing:
    ``None`` (the default) **derives** every other classification position from the run itself via
    :func:`derive_sibling_indices`, ``()`` checks nothing, and an explicit sequence checks exactly
    those. The default flipped because a guardrail nobody remembers to arm is a guardrail the tool
    does not have — the shipped snippet passed ``()`` and told the reader to leave it that way, so
    a suite that stacked sibling criteria was silently ungated against a candidate winning by
    annexing them. Passing ``()`` is now how you turn the check off deliberately.

    Leaves ``promoted=None``. One gate knows nothing about the family it belongs to, and the Holm
    correction is a property of that family — pass every survivor's verdict through
    :func:`holm_promote` in one call. (The one exception is a sample too small to support any
    statistic at all: that returns ``promoted=False`` outright, because there is no p-value for a
    family decision to correct.)
    """
    require_valid_criterion_index(criterion_index)
    for index in sibling_indices or ():
        # Same guard, different parameter NAME — which is exactly why it was missed. `label_pairs`
        # bounds only above, so a negative sibling index grades the LAST criterion under a label
        # reading `criterion -1`, and `holm_promote` folds `sibling_checks` into `promoted`: the
        # wrong criterion then vetoes, or fails to veto, a promotion.
        require_valid_criterion_index(index)
    paired = load_and_pair(
        incumbent_run_dirs=incumbent_run_dirs,
        candidate_run_dirs=candidate_run_dirs,
        incumbent_variant=incumbent_variant,
        candidate_variant=candidate_variant,
        suite_id=suite_id,
        criterion_index=criterion_index,
    )
    incumbent_rows, candidate_rows = paired.incumbent_rows, paired.candidate_rows
    scored_row_ids, n_discordant = paired.scored_row_ids, paired.n_discordant
    # THE SAME list object, not a copy: pydantic copies it at construction, so every note this
    # function still adds has to land in the list `load_and_pair` returned, before either return.
    notes = paired.notes

    def _refuse_activation(refusal: str) -> ActivationGateVerdict:
        """The row-selection preflight's early exit: every statistic ``None``, the refusal set.

        One helper for BOTH preflight refusals (and any third), because they differ only in the
        message: twenty identical keywords copied per cause is how two blocks drift, and how
        adding a field to ``ActivationGateVerdict`` becomes three coordinated edits. Literal
        keywords, never a splat, so CE041/CE048 still see the construction — the same shape
        ``execution_gate``'s :class:`~coder_eval.optimize.gate.FirstCause` already has on the
        other track.

        The loaded counts ARE echoed: the block still says what it read, even though it refuses
        to compare it. Everything derived from the comparison is ``None``, and there is no
        ``p_value`` — this is a WIRING refusal, so it renders under ``NOT A RESULT`` and stays
        distinguishable from the discreteness refusal, the only one that ever carries a p.
        """
        return ActivationGateVerdict(
            incumbent_variant=incumbent_variant,
            candidate_variant=candidate_variant,
            suite_id=suite_id,
            criterion_index=criterion_index,
            confidence=confidence,
            n_resamples=n_resamples,
            rows_paired=len(scored_row_ids),
            rows_excluded=paired.rows_excluded,
            incumbent_f1=None,
            candidate_f1=None,
            mean_diff=None,
            ci_low=None,
            ci_high=None,
            p_value=None,
            gate_refusal=refusal,
            notes=notes,
        )

    refusal, preflight_notes = _activation_preflight(
        incumbent_run_dirs=incumbent_run_dirs,
        candidate_run_dirs=candidate_run_dirs,
        incumbent_variant=incumbent_variant,
        candidate_variant=candidate_variant,
        suite_id=suite_id,
    )
    # EXTEND, never re-bind: `notes` is the SAME list object `load_and_pair` returned, and pydantic
    # copies it at construction — so `notes = notes + preflight_notes` would leave every later
    # append landing in a list no verdict ever sees.
    notes.extend(preflight_notes)
    if refusal is not None:
        return _refuse_activation(refusal)

    # The four shared values, hoisted into named locals rather than a keyword dict the two returns
    # splat. Three are expensive (two bootstraps and a sibling scan); `p_floor` is pure arithmetic
    # and is computed last here rather than first as the dict had it — safe, and stated because
    # this comment is the only record of the reorder: `_discreteness_floor` is pure, logs nothing,
    # raises nothing, and every RNG on this path is a seed-local `random.Random`.
    #
    # A splat means a mistyped or renamed field silently becomes None — exactly what
    # `extra="forbid"` on the model and CE041 in the linter now forbid — and the two return paths
    # differ in ten values anyway, so the dict was only ever holding these four plus the scalars.
    sibling_checks = _sibling_checks(
        incumbent_rows=incumbent_rows,
        candidate_rows=candidate_rows,
        paired_row_ids=scored_row_ids,
        # Derived over the UNION of both arms' rows, so a criterion present on one arm only is
        # still found and reported by the one-sided note rather than going unchecked.
        sibling_indices=(
            derive_sibling_indices(incumbent_rows, candidate_rows, primary_index=criterion_index)
            if sibling_indices is None
            else sibling_indices
        ),
    )
    # Over the rows the F1 comparison actually used, so a guardrail is never computed on a
    # different sample than the number it guards.
    guardrails = cost_latency_guardrails(
        incumbent_rows=incumbent_rows,
        candidate_rows=candidate_rows,
        row_ids=scored_row_ids,
        materiality=materiality,
        seed=seed,
        confidence=confidence,
        n_resamples=n_resamples,
    )
    # The MDE is a NULL comparison, and only the incumbent supplies one — splitting the
    # candidate's invocations would measure a different arm's noise.
    #
    # The sink is what lets the note below name the ACTUAL cause. Five are reachable, and the
    # sentence this replaces named one of them unconditionally.
    mde_reasons: list[str] = []
    mde = noise_floor_mde(
        run_dirs=incumbent_run_dirs,
        variant_id=incumbent_variant,
        suite_id=suite_id,
        criterion_index=criterion_index,
        confidence=confidence,
        seed=seed,
        n_resamples=n_resamples,
        reasons=mde_reasons,
    )
    # THE ALL-NEGATIVE SUBSET. When the target label appears in neither arm's pairs, `f1.yes` is
    # undefined on both arms and reads 0.0 by the ClassificationMetrics convention — so the block
    # reports a real-looking zero-effect verdict over a metric that was never computed.
    #
    # A NOTE, not a refusal, and that is a measured decision rather than a preference: the case
    # always carries n_discordant == 0 (`expected` is a property of the ROW, so both arms share
    # it; an absent label forces every pair to ("no","no") on both arms), so it is ALREADY refused
    # by the zero-discordant path with promoted=False. A second refusal would add an ordering
    # question against the first and change no outcome. What it does fix is the message: without
    # this note, "the label is absent from the suite" and "rows expect the label and neither arm
    # engaged" render byte-identically, and the shared remedy is wrong for the first.
    #
    # `--split` is how this now arises in practice: a test split can select an all-negative subset
    # of a suite that has positive rows in train.
    # Guarded on there BEING pairs. `any()` over an empty iterable is False, so without this the
    # note also fires when the arms scored NOTHING — a wiring fault that already has its own note
    # naming a mistyped `criterion_index`. The block then carried two contradictory remedies
    # ("check expected_skill / your --split" against "fix the criterion index"), on the commonest
    # wiring error this gate has a dedicated message for. It also falsified the comment below:
    # with no pairs, `n_discordant` is None rather than 0, so the zero-discordant path does NOT
    # refuse and the "already refused" argument does not hold.
    scored_pairs = (*paired.incumbent_pairs, *paired.candidate_pairs)
    if scored_pairs and not any(TARGET_LABEL in pair for pair in scored_pairs):
        notes.append(
            f"no row scored here expects or observes {TARGET_LABEL!r}, so f1.{TARGET_LABEL} is "
            + "undefined on BOTH arms and reads 0.000 by the criterion layer's convention — the "
            + "comparison above is over a metric that was never really computed. The remedy is rows "
            + "that EXPECT the label: check `expected_skill`, and check whether --split selected an "
            + "all-negative subset of a suite whose positive rows live in another split."
        )

    p_floor = _discreteness_floor(len(scored_row_ids), n_discordant, n_resamples)

    bootstrap = cluster_bootstrap_diff_ci(
        paired.candidate_clusters,
        paired.incumbent_clusters,
        _f1_yes,
        n_resamples=n_resamples,
        confidence=confidence,
        seed=seed,
    )
    if bootstrap is None:
        notes.append(
            f"only {len(scored_row_ids)} row(s) scored on both arms — fewer than the 2 an interval needs, "
            + "so every statistic is reported as unavailable rather than fabricated."
        )
        return ActivationGateVerdict(
            incumbent_variant=incumbent_variant,
            candidate_variant=candidate_variant,
            suite_id=suite_id,
            criterion_index=criterion_index,
            confidence=confidence,
            n_resamples=n_resamples,
            rows_paired=len(scored_row_ids),
            rows_excluded=paired.rows_excluded,
            incumbent_f1=None,
            candidate_f1=None,
            mean_diff=None,
            ci_low=None,
            ci_high=None,
            p_value=None,
            # No interval, so there is nothing for a floor to be a floor ON. And `n_discordant` is
            # `None` rather than the 0 or 1 a single row would compute: 0 is the meaningful "the
            # arms agreed on every row", which is a finding. "There was no comparison" is not.
            p_floor=None,
            n_discordant=None,
            promoted=False,
            mde=mde,
            sibling_checks=sibling_checks,
            guardrails=guardrails,
            notes=notes,
        )

    mean_diff, ci_low, ci_high, p_value = bootstrap

    if mde is not None and abs(mean_diff) < mde:
        notes.append(
            f"the observed difference ({mean_diff:.3f}) is smaller than this suite's minimum detectable "
            + f"effect ({mde:.3f}). An interval excluding zero is still reportable, but do not present it "
            + "as a comfortable win — the suite cannot resolve a difference this size reliably."
        )
    elif mde is None:
        # The REAL cause, threaded out of `measure_noise_floor` rather than guessed at. This used
        # to say "(a null comparison needs at least two invocations of the incumbent)"
        # unconditionally, which is false for five of the six causes — reproduced on an incumbent
        # with TWO invocations where one row scored in both halves, which rendered "needs at least
        # two invocations" beside `len(incumbent_run_dirs) == 2`. The generic tail is the fallback
        # for a `None` that arrived with no reason recorded, which no path produces today.
        cause = mde_reasons[0] if mde_reasons else "no reason was recorded"
        notes.append(
            f"the minimum detectable effect could not be computed ({cause}), so nothing here says "
            + "how small a difference this suite can resolve."
        )

    # Retained as a DIAGNOSTIC, never as the gate: the per-invocation ranges are what the old
    # rule compared, and reporting them keeps a reader's intuition calibrated against the CI.
    incumbent_per_invocation = [
        _f1_yes([p for rid in scored_row_ids if rid in rows for p in label_pairs(rows[rid], criterion_index)])
        for rows in paired.incumbent_by_dir
    ]
    candidate_per_invocation = [
        _f1_yes([p for rid in scored_row_ids if rid in rows for p in label_pairs(rows[rid], criterion_index)])
        for rows in paired.candidate_by_dir
    ]
    range_non_overlap = bool(
        incumbent_per_invocation
        and candidate_per_invocation
        and min(candidate_per_invocation) > max(incumbent_per_invocation)
    )

    return ActivationGateVerdict(
        incumbent_variant=incumbent_variant,
        candidate_variant=candidate_variant,
        suite_id=suite_id,
        criterion_index=criterion_index,
        confidence=confidence,
        n_resamples=n_resamples,
        rows_paired=len(scored_row_ids),
        rows_excluded=paired.rows_excluded,
        incumbent_f1=_f1_yes(paired.incumbent_pairs),
        candidate_f1=_f1_yes(paired.candidate_pairs),
        mean_diff=mean_diff,
        ci_low=ci_low,
        ci_high=ci_high,
        p_value=p_value,
        p_floor=p_floor,
        n_discordant=n_discordant,
        range_non_overlap=range_non_overlap,
        mde=mde,
        sibling_checks=sibling_checks,
        guardrails=guardrails,
        notes=notes,
    )


def _refusal_message(verdict: ActivationGateVerdict, *, threshold: float, family_size: int, alpha: float) -> str | None:
    """The CANNOT SEPARATE refusal for one verdict, or ``None`` when the suite can express its bar.

    A ~60-line message builder sitting inside a boolean decision: it computes ``max_family``, the
    family lever, the row lever and the remedy, and the decision resumes 60 lines later. It takes
    everything it needs from the verdict plus the family's rank-dependent threshold — which is
    exactly why it could not live on the verdict model and does live here.

    Rank-scoped, deliberately: ``p_floor`` is a property of the SUITE and identical across the
    family, but ``threshold`` depends on rank, so where the floor sits between alpha/S and
    alpha/(S-1) a worse-ranked sibling is NOT refused and may still promote.

    Returns ``None``, never ``""``: ``gate_refusal`` is ``str | None`` on the model and
    :func:`render_markdown` branches on ``is not None``.
    """
    # A floor above the bar the p is decided against means the gate structurally cannot
    # separate here — a statement about the SUITE, not about this candidate.
    #
    # `not p_floor > threshold` rather than `p_floor <= threshold`, which is the one place this
    # early return is not a plain De Morgan of the `if p_floor is not None and p_floor > threshold`
    # it replaced. Every comparison against NaN is False, so a NaN floor took the no-refusal path
    # before; under `<=` it would fall THROUGH to `math.floor(alpha / nan)` and raise out of the
    # skill's inline snippet. Defence in depth rather than a live path — `_discreteness_floor`
    # clamps through `min`/`max` and pydantic's validator rejects a non-finite float, so no
    # validated verdict can carry one — but preserving the original semantics costs one `not`.
    if verdict.p_floor is None or not verdict.p_floor > threshold:
        return None

    # A floor of exactly 1.0 is the ZERO-DISCORDANT case and nothing else: `2*(1-R/M)**M`
    # reaches 1.0 only at R = 0 (for R >= 1 it peaks at 0.5, at M = 2). It is a different
    # finding with a different remedy — the arms behaved identically, so the discordance
    # RATE is zero and `(1-R/M)**M` does not shrink with M however many rows are added.
    # Telling that operator to buy more rows is the misdiagnosis this branch exists to stop.
    if verdict.p_floor >= 1.0:
        return (
            f"the two arms produced identical labels on every one of the {verdict.rows_paired} "
            "scored rows, so there is nothing for any test to separate — at any family size, "
            "at any alpha, and at any number of rows. That is a result about this candidate "
            "rather than about the suite: adding more rows LIKE THESE cannot change it. Check "
            "the candidate actually differs from the incumbent, and that both arms were wired "
            "to the snapshots you think they were."
        )

    # TWO levers, and the second one is not "more rows". `2*(1-R/M)**M` RISES with M at
    # a fixed R, so a reader told to add rows can buy concordant ones and land strictly
    # worse off (measured: R=3 floors at 0.047 over 8 rows, 0.056 over 10, 0.078 over
    # 20). The lever is the DISCORDANT count, and `min_discordant_rows` owns it — the
    # number is computed here, never quoted from prose.
    max_family = math.floor(alpha / verdict.p_floor)
    family_lever = (
        f"Gate at most {max_family} survivor(s) at alpha={alpha}"
        if max_family >= 1
        else f"No family size works at alpha={alpha}, not even a family of one"
    )
    if verdict.n_discordant is None:
        # Never a sentence about a count the verdict does not carry.
        remedy = f"{family_lever}."
    else:
        required = min_discordant_rows(verdict.rows_paired, threshold, verdict.n_resamples)
        row_lever = (
            (
                f"raise the rows the two arms DISAGREE on from {verdict.n_discordant} to "
                + f"{required} at the current {verdict.rows_paired} paired rows — adding "
                + "rows they agree on makes this floor worse, not better"
            )
            if required is not None
            # `min_discordant_rows` returns None only when even EVERY row discordant
            # leaves the estimator's own 2/(m+1) above the bar — and that value
            # depends on the draw count alone, not on the suite. So "more rows" is
            # not merely insufficient here, it is irrelevant, and saying otherwise
            # would send a user to buy rows that provably cannot work.
            else (
                f"no discordant count clears this bar at {verdict.rows_paired} rows or at "
                + f"any other: the bar sits below what {verdict.n_resamples} bootstrap "
                + "draws can resolve at all, so the levers are a larger n_resamples or a "
                + "smaller family — not rows"
            )
        )
        remedy = f"{family_lever}, or {row_lever}."
    return (
        f"this suite cannot express a p below {verdict.p_floor:.4f} at {verdict.rows_paired} "
        f"paired rows, and the Holm threshold for this rank in a family of {family_size} is "
        f"{threshold:.4f}. This candidate could not have promoted however good it is — the "
        f"bar sits below what the suite can measure — so this is NOT a negative result "
        f"about it. {remedy}"
    )


def _activation_notes(
    verdict: ActivationGateVerdict,
    *,
    p_value: float,
    rejected: bool,
    refusal: str | None,
    siblings_hold: bool,
    threshold: float,
    family_size: int,
    alpha: float,
) -> list[str]:
    """Every note :func:`holm_promote` adds to a MEASURED verdict, in rendered order.

    A pure function of one verdict plus the family facts, extracted because the ladder is what
    made ``holm_promote`` an E-grade function once the guardrail veto and the refusal guard landed
    on top of it. Nothing here decides anything: ``promoted`` is computed by the caller and is not
    read.

    ``p_value`` is passed rather than read off the verdict, making the contract explicit: this
    ladder runs only on the MEASURED branch, after ``holm_promote`` has already returned for
    every ``p_value is None`` verdict. Reading ``verdict.p_value`` here would be ``float | None``
    and would need a coercion that quietly renders ``p = 0.0000`` for a verdict that never had
    a p at all.

    **Two tiers, and the split is the contract.** The first four rungs are NEGATIVE-RESULT claims —
    sentences about a candidate that lost — and every one is suppressed under a refusal, because a
    refusal says the comparison decided nothing and a claim beneath it is a second, contradictory
    one. Three of the four used to fire regardless. The resolution-floor warning is NOT such a
    claim — it is a statement about the draw count, which stays true under a refusal — so it sits
    outside the guard. The execution track draws the line in exactly the same place.

    The two TRAILING notes are deliberately absent: :func:`~coder_eval.optimize.gate.decide_family`
    appends :func:`~coder_eval.optimize.gate.note_holm_family` and
    :func:`~coder_eval.optimize.gate.note_resolution_degraded` for both tracks. Appending either
    here prints it twice.
    """
    notes: list[str] = []
    favours_candidate = verdict.mean_diff is not None and verdict.mean_diff > 0.0
    # The interval must exclude zero as well as the corrected test rejecting. Holm is the stricter
    # of the two almost always, so this changes nothing on a typical family — but it keeps "promote
    # when the interval excludes zero" literally true, which is how the method file states the rule
    # and how anyone reading the rendered block will check it. Kept apart from `verdict.separated`
    # so the rungs can say WHICH of the two failed, which is their whole content.
    excludes_zero = verdict.ci_low is not None and verdict.ci_low > 0.0

    if refusal is None:
        if rejected and favours_candidate and siblings_hold and not excludes_zero:
            notes.append(NOTE_CI_CONTAINS_ZERO)
        if rejected and not siblings_hold:
            notes.append(
                "not promoted: the interval separates but a sibling's recall.yes dropped — this candidate "
                + "moved the failure rather than fixing it."
            )
        if rejected and not favours_candidate:
            notes.append("not promoted: the interval separates in the incumbent's favour.")
        if not rejected:
            notes.append(note_ordinary_negative(p_value, family_size, alpha))
        # Names WHICH guardrail vetoed, mirroring the execution track's loop. `sibling_checks` is
        # deliberately NOT iterated: the rung above is already the single declaration for a sibling
        # failure. Guarded further on `rejected and separated`, the same conjuncts the BLOCKED
        # headline is keyed on — on a candidate that merely lost the guardrail forced nothing, so
        # the note would claim a veto that did not happen.
        if rejected and verdict.separated:
            notes += [note_check_failed(check.name) for check in verdict.guardrails if not check.passed]

    # A p at the resample floor is a resolution statement, not a measurement: the corrected
    # threshold can sit BELOW what the bootstrap can express, and then no candidate can ever
    # promote however good it is. Measured: 4 perfect candidates at 8 rows flip from all-rejected
    # to all-promoted between 2,000 and 20,000 resamples on identical data. OUTSIDE the guard —
    # it is true whatever the refusal says.
    estimator_floor = bootstrap_p_floor(verdict.n_resamples)
    if p_value <= NEAR_FLOOR_MULTIPLE * estimator_floor:
        notes.append(
            f"p = {p_value:.4f} is at or near this bootstrap's resolution floor "
            + f"({estimator_floor:.4f} at {verdict.n_resamples} draws), and the Holm threshold for "
            + f"this rank is {threshold:.4f}. Where the threshold approaches the floor the decision is "
            + "being made by the resample count rather than by the data — re-run the gate with a larger "
            + "n_resamples before believing either answer. A small suite has its own coarser floor: with "
            + "few positive rows the smallest achievable p is bounded well above the estimator's."
        )
    return notes


class SeedStability(NamedTuple):
    """Whether a gate's decision survives a change of bootstrap seed. A READING, never a verdict.

    A `NamedTuple` beside the function that produces it rather than a model in ``models/optimize.py``,
    following :class:`~coder_eval.optimize.fronts.RuleCeiling` — whose docstring states the rule this
    family goes by outright, "computed and rendered, never persisted". The verdict models are the
    other category: decision records with ``extra="forbid"``, dumped to pinned fixtures. This is
    neither, so it is not exported from ``coder_eval.models`` either.

    **It deliberately carries NO single ``promoted`` field.** Collapsing three disagreeing seeds into
    one verdict is the exact thing it exists to prevent: a decision that flips with the seed is a coin
    flip, and reporting the majority's answer as *the* answer hides that.

    **``promote_agreement`` counts promotions at a FAMILY OF ONE, which is not the round's decision
    when the round gated more than one candidate.** Each seed's verdict goes through ``holm_promote``
    alone, so the threshold is ``alpha`` rather than the rank-dependent ``alpha/m`` a real family
    applies — measured on a 10-row suite at p = 0.0299: a family of three rejects nothing while this
    reads 3/3. A faithful reproduction would need every sibling's p at every seed, which is a
    different and much more expensive question. So the count answers "does THIS candidate's own
    statistic survive the seed", the renderer says so in those words, and ``p_spread`` is the part to
    compare against the real family's threshold.
    """

    seeds: tuple[int, ...]
    promote_agreement: int
    p_values: tuple[float | None, ...]
    p_spread: float | None

    @property
    def unanimous(self) -> bool:
        """True when every seed agreed — either all promoted or none did.

        A property rather than a field, for :attr:`SearchComparison.accepted`'s reason: nothing new is
        stored, so no construction site can set it inconsistently with the counts it derives from.
        """
        return self.promote_agreement in (0, len(self.seeds))


def gate_seed_stability(*, seeds: Sequence[int] = (0, 1, 2), **gate_kwargs) -> SeedStability:
    """Run :func:`activation_gate` once per seed and report whether the decision held.

    The bootstrap is seeded, so a p near the Holm threshold can land on either side of it depending on
    the draw — and nothing in a single verdict says whether that happened. This asks.

    **A separate function rather than a ``seeds=`` parameter on the gate**, and that is what keeps it
    cheap to have: adding the parameter would change the cost and the rendered output of every
    existing call site and every pinned fixture. The gate is untouched, so nothing moves.

    **It costs no agent runs at all** — three bootstraps over rows already on disk, CPU only. Say so
    wherever it is offered, or a reader assumes it triples the round.

    Disagreeing seeds are the FINDING, not an error. Never pick the majority's verdict and present it
    as the answer; :attr:`SeedStability.unanimous` is there so a caller cannot accidentally read one.

    **It decides at a FAMILY OF ONE**, per seed, which is not the round's decision when the round
    gated a shortlist — see :class:`SeedStability`. The reading is about whether one candidate's own
    statistic is seed-stable, and it is legitimate to ask that of a candidate the family correction
    rejected; what is not legitimate is quoting its promote count as the round's answer.

    **The execution track has no useful twin, and the reason is worth stating rather than leaving as
    an omission.** Its primary statistic is an analytic paired *t*, deterministic given the rows, so a
    seed moves only the MDE and the cost/latency guardrails — the function would report a spread of
    zero on the number that decides, which is a true answer to a question nobody asked.

    ``gate_kwargs`` are forwarded verbatim; passing ``seed`` there raises, since the seeds are the
    axis being varied.
    """
    if "seed" in gate_kwargs:
        raise TypeError("gate_seed_stability varies the seed itself — pass `seeds=`, not `seed=`.")
    if not seeds:
        raise ValueError("gate_seed_stability needs at least one seed to vary.")
    # Each seed's verdict goes through `holm_promote` on its own, because `promoted` is a FAMILY
    # decision and a family of one is what a single-candidate stability check is asking about. Pooling
    # the three into one family would correct across three runs of the same hypothesis.
    decided = [holm_promote([activation_gate(seed=seed, **gate_kwargs)])[0] for seed in seeds]
    p_values = tuple(verdict.p_value for verdict in decided)
    measured = [p for p in p_values if p is not None]
    return SeedStability(
        seeds=tuple(seeds),
        promote_agreement=sum(1 for verdict in decided if verdict.promoted),
        p_values=p_values,
        # `None` below two measured values: a spread over one p is 0.0, which reads as "the seeds
        # agreed" when what happened is that only one of them produced a number.
        p_spread=max(measured) - min(measured) if len(measured) >= 2 else None,
    )


def confirm_gate(
    *,
    train_verdict: ActivationGateVerdict,
    incumbent_run_dirs: Sequence[Path],
    candidate_run_dirs: Sequence[Path],
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    criterion_index: int,
    sibling_indices: Sequence[int] | None = None,
    materiality: float = MATERIALITY_FLOOR,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = GATE_RESAMPLES,
) -> ConfirmVerdict:
    """Stage C on the activation track: did the Stage B ``f1.yes`` effect REPRODUCE on the test split?

    The twin of :func:`~coder_eval.optimize.execution.confirm_gate_execution`, and it shares the
    classification and the record's assembly with it — :func:`~coder_eval.optimize.gate.classify_confirm`
    and :func:`~coder_eval.optimize.gate.build_confirm_verdict` both live at rank 1 precisely because
    the two rank-2 track modules may not import each other, so writing the rule here and mirroring it
    there would be two copies of promotion-relevant arithmetic.

    What differs is only what each track measures: the effect here is ``candidate_f1 - incumbent_f1``
    and the margin is THIS gate's own floor on ``f1.yes``. The margin rule is the same sentence on
    both tracks — the confirm split's own resolution on the metric that track gates — so it is
    per-track by construction rather than by a second constant.

    **Exactly ONE candidate**, a family of ONE, and a REFUSAL unless the confirm runs recorded
    ``--split test``: all three for the reasons the execution twin's docstring gives. This track pools
    several run directories per arm, so the provenance check reads BOTH arms' dirs and a cross-split
    pair is caught by ``activation_gate``'s own preflight underneath.
    """
    confirm_one_candidate(candidate_variant)

    notes: list[str] = []
    # The FIRST cause that makes this not a comparison — see `FirstCause`.
    cause = FirstCause()

    if (train_note := confirm_train_note(train_verdict.promoted)) is not None:
        notes.append(train_note)
    if (train_refusal := confirm_train_refusal(train_verdict.gate_refusal)) is not None:
        cause.record(train_refusal)

    # De-duplicated, preserving order: this track's normal shape — and the shipped snippet — passes
    # the SAME run dirs for both arms, since one invocation writes both variants. Concatenating them
    # made the unrecorded-provenance note say "missing from 2 of 6" for three real directories.
    confirm_dirs = list(dict.fromkeys([*incumbent_run_dirs, *candidate_run_dirs]))
    provenance = read_split_provenance(confirm_dirs)
    split_refusal, split_note = confirm_split_check(provenance, confirm_dirs)
    if split_note is not None:
        notes.append(split_note)
    if split_refusal is not None:
        cause.record(split_refusal)
    elif provenance.mismatched:
        # This track pools several dirs per arm, so its confirm can be internally inconsistent in a
        # way the execution twin cannot. `activation_gate` refuses it underneath too, but a Stage C
        # block reporting UNDECIDED with no reason would send the reader to the gate's notes to find
        # out why. Below the split check: a mismatch that INCLUDES a non-test split is the more
        # specific fault, and `confirm_split_check` names every off-split value it saw.
        cause.record(split_mismatch_reason("the confirm run's", provenance, confirm_dirs))

    test_verdict = holm_promote(
        [
            activation_gate(
                incumbent_run_dirs=incumbent_run_dirs,
                candidate_run_dirs=candidate_run_dirs,
                incumbent_variant=incumbent_variant,
                candidate_variant=candidate_variant,
                suite_id=suite_id,
                criterion_index=criterion_index,
                sibling_indices=sibling_indices,
                materiality=materiality,
                confidence=confidence,
                seed=seed,
                n_resamples=n_resamples,
            )
        ]
    )[0]
    if test_verdict.gate_refusal is not None:
        cause.record(f"the confirm gate is not a result: {test_verdict.gate_refusal}")

    return build_confirm_verdict(
        incumbent_variant=incumbent_variant,
        candidate_variant=candidate_variant,
        suite_id=suite_id,
        train_effect=train_verdict.mean_diff,
        test_effect=test_verdict.mean_diff,
        test_mde=test_verdict.mde,
        test_verdict=test_verdict,
        confirm_refusal=cause.reason,
        notes=notes,
    )


def holm_promote(verdicts: list[ActivationGateVerdict], alpha: float = DEFAULT_ALPHA) -> list[ActivationGateVerdict]:
    """Decide the whole survivor family at once, and record the decision on each verdict.

    A thin wrapper over :func:`~coder_eval.optimize.gate.decide_family`, which owns the Holm loop,
    the ``promoted`` conjunction and the two trailing notes for BOTH tracks. What is left here is
    what only this track knows: the rank-dependent threshold, the discreteness refusal computed
    from it, and the note ladder.

    With ``S`` survivors gated against the same incumbent on the same rows, the family-wise error
    rate inflates. Holm's step-down corrects it — and it is a property of the FAMILY, so
    ``decide_family`` calls :func:`coder_eval.reports_stats.holm_rejections` **once** across the
    whole p-value vector. Calling it per candidate would degenerate to an uncorrected ``p <= alpha``;
    dividing alpha by the survivor count at each gate would be plain Bonferroni, which is not Holm.

    A verdict whose ``p_value`` is ``None`` (too few paired rows) is not part of the family: it is
    excluded from the vector so it cannot tighten the correction for the others, and comes back
    ``promoted=False``.

    **``promoted`` is Holm rejecting AND ``verdict.separated`` AND no refusal AND no failed sibling
    check or guardrail** — and it is now literally the same expression
    :func:`holm_promote_execution` applies, because both go through ``decide_family``. The
    cost/latency guardrails used to gate in the skill's prose rather than in this field, which meant
    a candidate that materially raised what a row costs read ``promoted=True``; the veto lives in
    the DECISION. What still differs between the tracks is only which lists each HAS: there are no
    ``integrity_checks`` on this one.

    Folding the veto in is only safe because the STATISTICAL half has its own name —
    :attr:`~coder_eval.models.GateVerdictBase.separated`, the property ``render_markdown`` keys its
    BLOCKED headline on together with ``holm_rejected``. Read ``promoted`` there instead and that
    headline becomes unreachable the moment this fold lands, silently degrading a blocked winner to
    the ordinary NOT PROMOTED rung.

    **A suite whose discreteness floor exceeds its Holm threshold is REFUSED, not rejected.** The
    corrected threshold can sit below what the suite's own row count can express, and then no
    candidate can promote however good it is — reporting that as an ordinary negative result is a
    claim about the candidates that the data cannot support. Such a verdict comes back with
    ``gate_refusal`` set and ``promoted=False``, and renders as its own headline.

    **There are TWO `gate_refusal` setters on this track and THREE causes**, and they differ in
    where they run and in what they carry. The discreteness refusal is computed by the hook below,
    because it needs the family's rank-dependent threshold. The other two are both set in
    :func:`activation_gate`'s row-selection preflight — the arms recorded different ``--split``
    values, or a run directory holds results its own ``run.json`` never wrote (a re-used
    ``--run-dir``). Neither needs anything outside a single verdict, and both always arrive with
    ``p_value is None`` — so ``decide_family`` returns before the hook runs, which is what stops the
    two refusals overwriting each other. The membership rule for the family is
    ``p_value is not None`` and nothing else: a refused verdict is outside it, so ``m`` (and
    therefore every sibling's ``alpha/m``) is unchanged by its presence.
    """

    def decide(verdict: ActivationGateVerdict, facts: FamilyFacts) -> TrackDecision:
        # `p_value` is not None on this branch — `decide_family` returns before calling the hook
        # otherwise — and the ladder below depends on that, so it is asserted by the narrowing
        # rather than by a coercion that would render `p = 0.0000` for a verdict that never had one.
        assert verdict.p_value is not None, "decide_family calls the hook on the measured branch only"
        threshold = _holm_threshold(facts.family_p_values, verdict.p_value, facts.alpha)
        refusal = _refusal_message(verdict, threshold=threshold, family_size=facts.family_size, alpha=facts.alpha)
        # `siblings_hold` is NOT merely a `promoted` conjunct — `failed_vetoes` covers that half.
        # The note ladder needs to tell a sibling regression apart from a guardrail failure, and it
        # gets that from this binding alone. Delete it as redundant and both causes print the
        # generic guardrail note instead.
        siblings_hold = all(check.passed for check in verdict.sibling_checks)
        return TrackDecision(
            refusal,
            _activation_notes(
                verdict,
                p_value=verdict.p_value,
                rejected=facts.rejected,
                refusal=refusal,
                siblings_hold=siblings_hold,
                threshold=threshold,
                family_size=facts.family_size,
                alpha=facts.alpha,
            ),
        )

    return decide_family(verdicts, alpha, decide=decide)
