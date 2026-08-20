"""The execution track, end to end: gate, diagnostics, and the family decision.

Rank 2 of the optimize family, beside :mod:`coder_eval.optimize.activation` and importing nothing
from it. Whether a candidate skill BODY produces better outcomes than the incumbent — measured as
per-row ``weighted_score`` through :func:`coder_eval.reports_stats.paired_comparison`, the
reporter's own paired *t*, REUSED rather than re-derived so the gate cannot disagree with the
``## Paired Comparison`` block a user reads beside it.

What is here and not on the other track: the sign resolution (``paired_comparison`` subtracts in
variant DECLARATION order, so this module resolves ``candidate - incumbent`` once, in code), the
integrity checks (engagement recall, completion rate), and a replicate-split noise floor measured
on ``weighted_score`` rather than F1 — because this track's gate never reads F1.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from coder_eval.models import (
    TARGET_LABEL,
    ConfirmVerdict,
    EvaluationResult,
    ExecutionGateVerdict,
    ExperimentResult,
    GuardrailCheck,
    NoiseFloor,
    OptimizeMeasurements,
    copy_with,
)
from coder_eval.optimize.gate import (
    FLOOR_RESOLUTION,
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
    criterion_weights,
    label_pairs,
    load_arm_rows,
    observed_result_types,
    pool_replicates,
    read_split_provenance,
    reconcile_tree_against_run_json,
    require_valid_criterion_index,
    row_score,
    task_json_pattern,
)
from coder_eval.optimize.store import UNRESOLVED_MODEL
from coder_eval.reports_stats import (
    DEFAULT_ALPHA,
    PairedComparison,
    mean,
    paired_comparison,
)


logger = logging.getLogger(__name__)


def resolve_model(rows: dict[str, list[EvaluationResult]]) -> str | None:
    """The model id these rows ran under, or ``None`` when it is not a single agreed value.

    ``None`` for unset, and ``None`` when the rows disagree — a mixed-model suite must never be
    cached under one model key, so an unresolvable model recomputes rather than borrowing another
    model's measurement. ``NoiseFloor.model`` (Phase 6) has no other source.
    """
    models = {result.model_used for results in rows.values() for result in results if result.model_used}
    return models.pop() if len(models) == 1 else None


def measure_execution_noise_floor(
    *,
    run_dirs: Sequence[Path],
    variant_id: str,
    suite_id: str,
    model: str,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = GATE_RESAMPLES,
    measurements: OptimizeMeasurements | None = None,
) -> NoiseFloor | None:
    """The execution track's minimum detectable effect: a null half-split over REPLICATES.

    The activation floor splits *invocations*, because Stage B there is three separate
    ``coder-eval run`` commands. The execution track has no such axis — it runs one invocation at
    ``--repeats 3`` — so the null comparison splits each row's **replicates** instead, the larger
    half first: at three replicates per row, two against one. The true difference is zero by
    construction either way, and the interval's half-width is the floor.

    Replicates are pooled across ``run_dirs`` before splitting, so the halves are ordered by run
    directory first and replicate number second. Which is which does not matter — replicates of one
    arm are exchangeable, so any fixed split is a valid null — but the ordering is deterministic,
    which is what makes the floor reproducible for a seed.

    The statistic is the mean per-row ``weighted_score``, which is what the execution gate's
    ``## Paired Comparison`` block actually compares. It is deliberately NOT ``f1.yes``: computing
    an F1 floor for a gate that never reads F1 is the bug this function exists to replace, and on
    the bundled outcome template it returns a confidently meaningless 0.000.

    **+0 runs.** It reads the control-arm run directory, which the method already requires once
    per suite at ``--repeats 3`` — so the preflight costs arithmetic, not money.

    Returns ``None`` — never a fabricated number — when nothing loaded (a mistyped path) or when
    fewer than 2 rows carry at least 2 replicates; the two are distinguished in the logged reason.
    An odd replicate count splits unevenly (3 -> 2/1), which widens the interval and therefore
    reports a CONSERVATIVE floor: the safe direction, exactly as on the activation side.

    A floor of exactly **0.000** is a real answer, not a missing one: it means every row's
    replicates agreed exactly, so the suite showed no run-to-run noise at all. On a real agent run
    that is worth checking rather than treating as a green light — it is what a suite whose rows
    are deterministic looks like, and also what one whose rows all failed identically looks like.
    """
    # The same three guards, on this track's split axis and with a sharper reason for the first:
    # the replicate half of the reconciliation keys on `<NN>`, so a re-used `--run-dir` with a
    # smaller `--repeats` leaves exactly the stale replicates this splits into halves. The split
    # refusal is reachable even though the execution GATE takes one run_dir — this takes a sequence.
    preflight = floor_preflight(
        run_dirs=run_dirs, variant_id=variant_id, suite_id=suite_id, split_label="the replicate split"
    )
    if preflight is None:
        return None
    per_dir, provenance = preflight
    # Pooled HERE rather than in the preflight: this track splits each row's replicates, so it wants
    # one map, while the activation twin keeps the per-invocation maps in order to halve them.
    rows = pool_replicates(per_dir)

    # `criterion_index=None` is already the "read the row's weighted_score" mode, so this reuses
    # the existing extractor rather than adding a second definition of what a row scored.
    replicated: list[list[float]] = []
    for _row_id, results in sorted(rows.items()):
        values = [v for r in results if (v := row_score(r, None)) is not None]
        if len(values) >= 2:
            replicated.append(values)
    if len(replicated) < 2:
        return no_floor(
            f"only {len(replicated)} of {len(rows)} row(s) of {suite_id!r} carry 2+ replicates with a "
            + "weighted_score — the replicate split needs 2. Was this run made with --repeats 2 or more?"
        )

    # BALANCE to a common replicate count before splitting, mirroring the per-row trim
    # `activation_gate` applies for the same reason. `cluster_bootstrap_diff_ci` pools the drawn
    # clusters' OBSERVATIONS before applying the statistic, so an unbalanced row weighs 2:1 across
    # the halves while a balanced one weighs 1:1 — and between-row spread then leaks into a
    # difference that is supposed to be zero by construction. Measured: 8 rows with NO within-row
    # variance report 0.000 at uniform counts and 0.056 when half of them carry 2 replicates
    # instead of 3 — a floor invented out of nothing but the imbalance. The trigger is mundane: one
    # crashed replicate at `--repeats 3` writes an empty result list and drops that row to 2.
    per_row = min(len(values) for values in replicated)
    # Split point is (per_row+1)//2, so 3 replicates go 2/1 rather than 1/2. Deliberate: the larger
    # half first keeps the bias conservative, exactly as the invocation split does.
    midpoint = (per_row + 1) // 2
    halves = [(values[:midpoint], values[midpoint:per_row]) for values in replicated]
    probe = NoiseFloor(
        suite_id=suite_id,
        variant_id=variant_id,
        model=model,
        metric="weighted_score",
        criterion_index=None,
        n_rows=len(replicated),
        n_invocations=len(run_dirs),
        n_replicates=per_row,
        confidence=confidence,
        seed=seed,
        n_resamples=n_resamples,
        split=provenance.value,
        mde=0.0,
        computed_at=datetime.now(UTC),
    )
    return floor_from_clusters([a for a, _b in halves], [b for _a, b in halves], mean, probe, measurements)


# ---------------------------------------------------------------------------
# The execution track's gate — the same decision, on the reporter's own statistic
# ---------------------------------------------------------------------------


def _completion_rates(
    incumbent_rows: dict[str, list[EvaluationResult]], candidate_rows: dict[str, list[EvaluationResult]]
) -> tuple[tuple[int, int], tuple[int, int]]:
    """``((incumbent scored, attempted), (candidate scored, attempted))`` over the UNION of rows.

    **The union, and a shared per-row denominator, are the whole point.** Computed over the paired
    intersection instead, this check is blind to exactly the erosion it exists to catch: a row that
    vanished from one arm leaves both the numerator and the denominator, and two arms measured on
    different row sets both report 100%. Measured before this was fixed — an arm missing two of
    eight rows reported ``8/8`` against ``8/8`` and passed.

    So each row contributes ``max(len(incumbent), len(candidate))`` slots to BOTH arms' denominators
    — what the row was worth if the run had completed — while only replicates that actually produced
    a criterion result count toward an arm's numerator. A row an arm never ran, and a row it ran and
    crashed, then read the same way, which is what the method's rule means by an eroded sample.
    """
    totals = {"incumbent": [0, 0], "candidate": [0, 0]}
    for row_id in sorted(set(incumbent_rows) | set(candidate_rows)):
        per_arm = {"incumbent": incumbent_rows.get(row_id, []), "candidate": candidate_rows.get(row_id, [])}
        attempted = max(len(results) for results in per_arm.values())
        for arm, results in per_arm.items():
            totals[arm][0] += sum(1 for result in results if result.success_criteria_results)
            totals[arm][1] += attempted
    logger.debug("completion: incumbent %s, candidate %s", totals["incumbent"], totals["candidate"])
    return (totals["incumbent"][0], totals["incumbent"][1]), (totals["candidate"][0], totals["candidate"][1])


def _paired_criterion_diffs(
    *,
    incumbent_rows: dict[str, list[EvaluationResult]],
    candidate_rows: dict[str, list[EvaluationResult]],
    row_ids: Sequence[str],
    criterion_index: int,
) -> list[float]:
    """One criterion's per-row paired differences, candidate - incumbent, replicates meaned first.

    The ONE spelling of that reduction on this track: :func:`_dead_weight` classifies these vectors
    and :func:`execution_gate` averages one of them for ``primary_mean_diff``, and a second
    implementation would let the reported primary effect disagree with the dead-weight reading
    computed from the same rows.

    A row where either arm produced no score for this criterion is EXCLUDED, exactly as the primary
    statistic excludes it — never folded in as a zero.
    """
    diffs: list[float] = []
    for row_id in row_ids:
        incumbent = [s for r in incumbent_rows.get(row_id, []) if (s := row_score(r, criterion_index)) is not None]
        candidate = [s for r in candidate_rows.get(row_id, []) if (s := row_score(r, criterion_index)) is not None]
        if incumbent and candidate:
            diffs.append(mean(candidate) - mean(incumbent))
    return diffs


def _dead_weight(
    *,
    incumbent_rows: dict[str, list[EvaluationResult]],
    candidate_rows: dict[str, list[EvaluationResult]],
    row_ids: Sequence[str],
) -> tuple[float | None, list[str]]:
    """The share of total criterion weight held by criteria whose paired difference is always zero.

    ``weighted_score`` — this gate's primary statistic — is a weighted mean over every criterion,
    so a criterion that scores identically on both arms on every row contributes its whole weight to
    the DENOMINATOR of that mean and nothing to the difference. The shipped outcome template is the
    worked case, and it is worth being precise about how much of it is BY DESIGN. Its engagement
    criterion saturates by design — that is what its ``recall.yes: 1.0`` threshold demands — and it
    carries a deliberately small weight for exactly this reason, so the by-design dead weight is that
    one criterion's share of the suite's 2.05 total: about 2.4%. The template's ``file_check`` is a
    GRADED outcome check with its own ``mean`` threshold and does NOT saturate by design; when it
    happens to saturate anyway — every arm produced the artifact — the share reaches 1.05 of 2.05 and
    an effect confined to the grader then arrives at roughly half its size. That second case is the
    one worth reporting, and it is a property of the RUN rather than of the template, which is why
    this is measured per verdict rather than assumed. (The engagement weight is deliberately not
    quoted as a literal: the sensor keeping ``DEFAULT_ALPHA`` the single spelling of its own value
    matches that number everywhere in this family.)

    **A READING, never a veto**, and the measurement behind that is in the field's own description:
    a constant criterion scales the paired difference vector without changing its shape, so it
    scales the mean and the standard deviation by the same factor and the paired *t* is identical to
    1e-12. Every conjunct of ``promoted`` is invariant to it. The one degenerate case that does
    invalidate a comparison — every criterion constant — is already the zero-variance refusal.

    ``None`` — never ``0.0`` — whenever the share cannot be computed, with a note naming why. "No
    dead weight" and "we cannot tell" are the two states this exists to separate.

    **Replicates collapse by MEAN, per row, per arm, before pairing**, which is the same reduction
    :func:`~coder_eval.reports_stats.paired_comparison` applies to ``weighted_score`` and
    :func:`~coder_eval.optimize.fronts.arm_row_scores` applies to its vectors — so the three
    surfaces agree about what a row scored. A mean over identical floats is still that float
    exactly, so the ``== 0.0`` test below survives the reduction: the comparison is on the raw
    subtraction and deliberately carries no tolerance, since a criterion that is genuinely constant
    produces exact zeros while a tolerance would classify a small real effect as dead.
    """
    if len(row_ids) < 2:
        return None, [
            "dead weight was not computed: fewer than two rows paired, so no per-criterion paired "
            + "difference vector exists to call constant."
        ]

    incumbent_results = [result for rid in row_ids for result in incumbent_rows.get(rid, [])]
    candidate_results = [result for rid in row_ids for result in candidate_rows.get(rid, [])]
    weights = criterion_weights(incumbent_results)
    candidate_weights = criterion_weights(candidate_results)
    if not weights or not candidate_weights:
        return None, [
            "dead weight was not computed: at least one arm produced no criterion results over the "
            + "paired rows, so there is no criteria list to weigh."
        ]
    if len(weights) != len(candidate_weights):
        # The suite is shared by construction on this track — one run_dir, one experiment — so this
        # is a contaminated tree. The reconciliation refusal above owns that diagnosis; adding a
        # second one here would report the same fault twice under two different names.
        return None, [
            f"dead weight was not computed: the arms carry {len(weights)} and "
            + f"{len(candidate_weights)} criterion result(s) per row, so their criteria lists "
            + "disagree. Both arms ran one suite, so this is a contaminated tree — re-run both into "
            + "a fresh --run-dir."
        ]
    if any(weight is None for weight in (*weights, *candidate_weights)):
        return None, [
            "dead weight is UNKNOWN: this run predates `CriterionResult.weight`, so the blend "
            + "behind `weighted_score` is not recorded in the artifact. Re-run to record it — the "
            + "share is deliberately not reported as 0.0, which would claim no dilution."
        ]

    # Every weight is a float from here; the reference result is the one `criterion_weights` read
    # them off, so index i of both lists describes one criterion.
    known = [weight for weight in weights if weight is not None]
    reference = next((result for result in incumbent_results if result.success_criteria_results), None)
    descriptions = [c.description for c in reference.success_criteria_results] if reference is not None else []

    dead: list[int] = []
    unusable: list[int] = []
    usable_rows: dict[int, int] = {}
    for index in range(len(known)):
        diffs = _paired_criterion_diffs(
            incumbent_rows=incumbent_rows,
            candidate_rows=candidate_rows,
            row_ids=row_ids,
            criterion_index=index,
        )
        usable_rows[index] = len(diffs)
        if not diffs:
            # Neither dead nor alive: it left no evidence either way, so it is excluded from BOTH
            # the numerator and the denominator rather than counted as varying.
            unusable.append(index)
        elif all(diff == 0.0 for diff in diffs):
            dead.append(index)

    total = sum(known[i] for i in range(len(known)) if i not in unusable)
    if total == 0.0:
        return None, [
            "dead weight was not computed: the compared criteria carry zero total weight, so the "
            + "share has no denominator."
        ]

    share = sum(known[i] for i in dead) / total
    notes: list[str] = []
    if dead:
        named = " and ".join(
            f"[{i}] {descriptions[i]!r} (w={known[i]:.2f}, {usable_rows[i]} row(s))"
            if i < len(descriptions)
            else f"[{i}] (w={known[i]:.2f}, {usable_rows[i]} row(s))"
            for i in dead
        )
        notes.append(
            f"{share:.1%} of the compared weight is dead: {named} have identically zero paired "
            + "differences on every row they scored, so they contribute to `weighted_score`'s "
            + "denominator and nothing to its difference. An effect confined to the remaining "
            + f"criteria reaches this block multiplied by {1.0 - share:.3f}. This is a READING and "
            + "gates nothing — the paired t is invariant to a constant criterion."
        )
    if unusable:
        notes.append(
            f"criterion index(es) {unusable} scored no row on both arms, so they are excluded from "
            + "the dead-weight share entirely rather than counted as varying."
        )
    return share, notes


def _sample_divergence_note(*, experiment_rows: int, primary_rows: int | None, on_disk_rows: int) -> str | None:
    """Why the three magnitudes on one block may not convert into each other. ``None`` when they do.

    `mean_diff` is computed from ``experiment.json``, `primary_mean_diff` over the rows the paired
    statistic used, and `dead_weight` over the on-disk intersection. A reader converting the blended
    difference back into the grader's unit is relying on those being one sample — true in the ordinary
    case, and false whenever a row scored in ``experiment.json`` has no readable ``task.json``, or one
    on disk was never scored.

    Reported rather than refused: each number is individually correct over the rows it was measured
    on, so this qualifies a conversion rather than invalidating a comparison. Which is exactly what
    ``notes`` is for.
    """
    counts = {experiment_rows, on_disk_rows} | ({primary_rows} if primary_rows is not None else set())
    if len(counts) == 1:
        return None
    primary = "not requested" if primary_rows is None else f"{primary_rows} row(s)"
    return (
        "the magnitudes on this block were measured over DIFFERENT numbers of rows: the paired "
        + f"statistic over {experiment_rows} row(s) from experiment.json, the primary reading over "
        + f"{primary}, and the dead weight over {on_disk_rows} row(s) read from disk. Each is correct "
        + "over its own sample, but `mean_diff` does NOT convert exactly into the primary via "
        + "(1 - dead_weight) here. A row scored in experiment.json whose task.json is missing or "
        + "unparseable is the usual cause — check the loader warnings above."
    )


def _integrity_checks(
    *,
    incumbent_rows: dict[str, list[EvaluationResult]],
    candidate_rows: dict[str, list[EvaluationResult]],
    row_ids: Sequence[str],
    engagement_criterion_index: int | None,
) -> list[GuardrailCheck]:
    """The two readings the method's promote-only-when list requires, derived from the ROWS.

    Deliberately not from ``suite.json``'s ``criterion_aggregates``: that list is *filtered*
    (``reports._compute_suite_rollup`` skips a criterion whose ``aggregate()`` returns ``None``
    with no ``suite_thresholds``, and an unregistered checker entirely), so position *i* there is
    not criterion *i*. Reading a positional engagement index out of it silently reports a
    DIFFERENT criterion's ``recall.yes`` the moment any earlier criterion produces no aggregate.

    ``engagement_criterion_index`` is a position in ``EvaluationResult.success_criteria_results``
    — the same index space :func:`activation_gate` uses, and the one that IS aligned with the
    suite's ``success_criteria``. ``None`` skips the engagement check.
    """
    checks: list[GuardrailCheck] = []
    metric_name = f"recall.{TARGET_LABEL}"

    if engagement_criterion_index is not None:
        index = engagement_criterion_index
        incumbent_pairs = [p for rid in row_ids for p in label_pairs(incumbent_rows.get(rid, []), index)]
        candidate_pairs = [p for rid in row_ids for p in label_pairs(candidate_rows.get(rid, []), index)]
        if not incumbent_pairs and not candidate_pairs:
            found = observed_result_types(incumbent_rows, engagement_criterion_index) | observed_result_types(
                candidate_rows, engagement_criterion_index
            )
            checks.append(
                GuardrailCheck(
                    name=f"engagement {metric_name} [criterion {engagement_criterion_index}]",
                    incumbent=None,
                    candidate=None,
                    relative_change=None,
                    tolerance=0.0,
                    passed=True,
                    note=(
                        f"criterion {engagement_criterion_index} holds no classification result on either arm "
                        + f"(types found: {sorted(found) or 'none — the index is past the end'}), so engagement "
                        + "was NOT evaluated. This is a wrong index, not a pass on the merits — the index is the "
                        + "criterion's POSITION in success_criteria, and it is not a position in suite.json's "
                        + "criterion_aggregates, which is a filtered list."
                    ),
                )
            )
        else:
            incumbent_recall = classification_metric(incumbent_pairs, metric_name)
            candidate_recall = classification_metric(candidate_pairs, metric_name)
            # The bar is 1.0 flat, not "no worse than the incumbent": a row the skill never engaged
            # on measured the ABSENCE of the thing under test, so it is not evidence about the
            # candidate's body however the incumbent did on it. (Recall is bounded above by 1.0, so
            # `>= 1.0` already implies `>= incumbent` — spelling both would be a dead conjunct.)
            checks.append(
                GuardrailCheck(
                    name=f"engagement {metric_name} [criterion {engagement_criterion_index}]",
                    incumbent=incumbent_recall,
                    candidate=candidate_recall,
                    relative_change=(
                        (candidate_recall - incumbent_recall) / incumbent_recall if incumbent_recall else None
                    ),
                    # An absolute FLOOR, not a permitted drop — see GuardrailCheck.tolerance.
                    tolerance=1.0,
                    passed=candidate_recall >= 1.0,
                    note=(
                        None
                        if candidate_recall >= 1.0
                        else "the skill did not engage on every scored row, so part of the sample measured the "
                        + "absence of the thing under test rather than a worse version of it"
                    ),
                )
            )

    # Over the union of rows, NOT `row_ids`: the paired set cannot see a row that vanished from one
    # arm, which is the erosion this check exists for.
    (incumbent_scored, incumbent_present), (candidate_scored, candidate_present) = _completion_rates(
        incumbent_rows, candidate_rows
    )
    incumbent_rate = incumbent_scored / incumbent_present if incumbent_present else None
    candidate_rate = candidate_scored / candidate_present if candidate_present else None
    checks.append(
        GuardrailCheck(
            name="completion_rate",
            incumbent=incumbent_rate,
            candidate=candidate_rate,
            relative_change=(
                (candidate_rate - incumbent_rate) / incumbent_rate
                if incumbent_rate and candidate_rate is not None
                else None
            ),
            tolerance=0.0,
            # An eroded, asymmetric sample produces confident nonsense: a p computed over rows that
            # vanished from one arm is not evidence. Equal, or favouring the incumbent, is the bar.
            passed=incumbent_rate is None or candidate_rate is None or candidate_rate >= incumbent_rate,
            note=(
                f"{candidate_scored}/{candidate_present} candidate replicate(s) scored against "
                + f"{incumbent_scored}/{incumbent_present} incumbent"
                if incumbent_present or candidate_present
                else "no replicates loaded on either arm — completion was not evaluated"
            ),
        )
    )
    return checks


def execution_gate(
    *,
    run_dir: Path,
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    engagement_criterion_index: int | None = 0,
    primary_criterion_index: int | None = None,
    materiality: float = MATERIALITY_FLOOR,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = GATE_RESAMPLES,
) -> ExecutionGateVerdict:
    """Gate ONE candidate against the incumbent on the execution track, from a Stage B gate run.

    The primary statistic is :func:`coder_eval.reports_stats.paired_comparison` — the reporter's
    own paired *t* over per-row ``weighted_score``, which the method file already calls this
    track's primary instrument. It is REUSED, never re-derived: a second assembly of a statistic
    this repo owns is the CE037-class defect, and it would let the gate disagree with the
    ``## Paired Comparison`` block a user reads beside it.

    **One ``run_dir`` per candidate.** ``paired_comparison`` fires only for exactly two variants,
    so the execution track gates one candidate at a time in its own ``round<N>-gate.yaml``. A round
    gating three candidates therefore has three gate run dirs — and the Holm family is assembled
    ACROSS them by :func:`holm_promote_execution`, exactly as the activation track assembles its
    family across candidates in shared ones.

    **The gate resolves the sign.** ``paired_comparison`` subtracts in variant *declaration* order,
    so with the incumbent declared first a candidate win comes back negative. This function knows
    which arm is the incumbent, so ``mean_diff`` here is ALWAYS ``candidate - incumbent`` and the
    interval bounds are swapped along with it. The method file warns twice that a reversed reading
    promotes the arm that lost; this is that warning, implemented.

    **Every state that is not a decision sets** ``gate_refusal``, which forces ``promoted=False``
    and takes the headline. Never an exception, and never a silent zero. It does NOT by itself
    drop the verdict out of the Holm family: membership is ``p_value is not None`` and nothing
    else, so a refusal that still MEASURED a p stays in and keeps ``m`` honest for its siblings
    (see :func:`holm_promote_execution`). The causes meaning there was no comparison at all
    already carry no p, so those leave by the ordinary rule rather than by a second one. Four
    kinds of cause, recorded MOST-SPECIFIC-FIRST because a later one is often the earlier one's
    consequence:

    - **No comparison to make** — both arms named the same variant; a missing, unreadable or
      malformed ``experiment.json``; an experiment declaring other than exactly two variants;
      either variant id absent from it. Each returns statistics that are all ``None``.
    - **An arm loaded ZERO rows** — the statistic comes from ``experiment.json``, so it computes
      perfectly well over rows that are not on disk while every check reads green over nothing.
    - **Fewer than two rows paired** — no interval exists, so there is nothing to weigh.
    - **Zero-variance paired differences** — the statistic computes, and every promotion conjunct
      then holds at once on a sample that separated nothing.
    - **A difference below this suite's own MDE *while the interval excludes zero*** — a confident
      claim about an effect the instrument cannot see. The second half of that condition is what
      keeps the commonest honest outcome intact: a candidate that simply does not help also has a
      difference below the floor, but its interval CONTAINS zero, and that is an ordinary negative
      result which stays one.

    **Two criterion indices, and they do different jobs.** ``engagement_criterion_index`` is an
    integrity CHECK's subject — which criterion says the skill engaged, a reading that can VETO a
    promotion. ``primary_criterion_index`` is the PREDECLARED primary, and the VALUE it reports is a
    reading: it adds ``primary_mean_diff`` so the effect is legible in the grader's own unit beside the
    blended one, and that number never touches ``promoted``. Setting the parameter can still refuse —
    an index selecting no usable row is a wiring fault, and a refusal forces ``promoted=False`` —
    stated here because a reader would otherwise take "a reading" to mean "safe to pass".
    (A third exists on the other track —
    ``ActivationGateVerdict.criterion_index``, that gate's metric SOURCE — which is why each of
    these says which kind it is.)

    Leaves ``promoted=None``: one gate knows nothing about its family.
    """
    require_valid_criterion_index(engagement_criterion_index)
    require_valid_criterion_index(primary_criterion_index)
    notes: list[str] = []
    # The run's row-selection provenance, as a NOTE and never a refusal — deliberately unlike
    # `activation_gate`, and the asymmetry is a consequence of the data sources rather than drift:
    # this track takes ONE `run_dir` holding BOTH variants, so the two arms share one run.json and
    # one split by construction and a cross-split pair is unrepresentable here. There is nothing to
    # refuse; there is still something worth stating, because "which rows did this gate run score?"
    # is the question a reader of a promotion ledger asks weeks later.
    run_provenance = read_split_provenance([run_dir])
    if run_provenance.unrecorded:
        notes.append(
            f"row-selection provenance is missing from {run_dir} (it predates the run.json "
            + "`row_selection` field, or it could not be read), so which rows this gate scored is "
            + "not recorded. Both arms still share one run directory, so they cannot disagree."
        )
    elif run_provenance.value is not None:
        notes.append(
            f"both arms ran under --split {run_provenance.value!r} (one run directory, so " + "they cannot disagree)."
        )

    # Read by `_verdict`'s construction at CALL time, so EVERY return path reports it. An attribute
    # rather than a `nonlocal` for exactly that reason — see `FirstCause`, which owns the
    # precedence rule and the four sites' shared rationale. Here the order reads: if there is no
    # comparison to make, the rows are moot; if the rows never loaded, whether their differences
    # vary is moot.
    cause = FirstCause()

    if incumbent_variant == candidate_variant:
        # Otherwise the sign resolves off the candidate, matches vid_a, and the block reports
        # `vid_a - vid_b` labelled `candidate - incumbent` with both labels reading the same name
        # — a confident, significant, sign-flipped verdict comparing an arm to the other arm while
        # claiming to compare it to itself. Measured before this guard existed.
        cause.record(
            f"incumbent_variant and candidate_variant are both {incumbent_variant!r}, so there is no "
            + "comparison to make and no sign to resolve. Name the two arms you meant to compare."
        )
    # Tree reconciliation, the same preflight `activation_gate` runs and for a sharper reason.
    # This track has NO cross-split refusal — one run_dir holds both arms, so they share one split
    # by construction — but a re-used `--run-dir` is fully representable here, and since the
    # integrity checks and guardrails are folded into `promoted` a stale replicate does not merely
    # get reported: it FLIPS the answer. Measured on an identical winning candidate, four
    # unrecorded incumbent replicates moved `completion_rate` from 1.0 to 0.667 and `promoted`
    # from True to False, with no refusal and no note. Contaminate the candidate arm instead and
    # the error runs the other way.
    for variant in (incumbent_variant, candidate_variant):
        reconciliation = reconcile_tree_against_run_json(run_dir, variant, suite_id)
        if reconciliation.unrecorded:
            examples = ", ".join(f"{row}/{rep}" for row, rep in sorted(reconciliation.unrecorded)[:3])
            cause.record(
                f"{run_dir}/{variant} holds {len(reconciliation.unrecorded)} result(s) that its "
                + f"run.json never wrote (e.g. {examples}). run.json is written per INVOCATION "
                + "while the tree is APPEND-ONLY, so a re-used --run-dir leaves an earlier call's "
                + "rows — or, with a smaller --repeats, its replicates — on disk. They are pooled "
                + "into this comparison and into the integrity checks that gate it. Re-run both "
                + "arms into a fresh --run-dir before gating."
            )
            break

    incumbent_rows = load_arm_rows([run_dir], incumbent_variant, suite_id)
    candidate_rows = load_arm_rows([run_dir], candidate_variant, suite_id)
    row_ids = sorted(set(incumbent_rows) & set(candidate_rows))

    # Measured ONCE, before `_verdict` exists: it costs a bootstrap, every return path reports it,
    # and the below-MDE note has to be written before the model is constructed (pydantic COPIES the
    # notes list, so an append after construction is silently discarded — measured, and it is why
    # `activation_gate` appends before its own return too).
    measured = measure_execution_noise_floor(
        run_dirs=[run_dir],
        variant_id=incumbent_variant,
        suite_id=suite_id,
        model=resolve_model(incumbent_rows) or UNRESOLVED_MODEL,
        confidence=confidence,
        seed=seed,
        n_resamples=n_resamples,
    )
    # `measured.mde if ... is not None`, never `measured.mde or None`: a floor of exactly 0.000 is a
    # real answer (every replicate agreed), and truthiness would erase it.
    mde = measured.mde if measured is not None else None

    # Computed HERE, beside the floor and before `_verdict` exists, for the same two reasons: every
    # return path reports it, and its explanatory sentence has to be in `notes` before the model is
    # constructed, since pydantic COPIES the list and a later append is silently discarded.
    dead_weight, dead_weight_notes = _dead_weight(
        incumbent_rows=incumbent_rows, candidate_rows=candidate_rows, row_ids=row_ids
    )
    notes.extend(dead_weight_notes)

    # Assigned near the end, over the rows the STATISTIC used — see the comment at that point. It is
    # declared here because `_verdict` closes over it and reads it at CALL time.
    primary_mean_diff: float | None = None
    # The primary's USABLE row count, for the divergence note below — `None` when no primary was
    # requested. Deliberately not `len(check_row_ids)`: that list comes from experiment.json, so a row
    # it names but disk does not carry is IN it and contributes no difference, which is exactly the
    # divergence being reported.
    primary_usable: int | None = None

    # The rows the CHECKS are computed over. Starts as the on-disk intersection and is narrowed to
    # the rows `paired_comparison` actually paired once that is known: `cost_latency_guardrails`'
    # own docstring states the contract — a guardrail must never be computed over a different
    # sample than the number it guards — and the two sets genuinely differ when a row exists on
    # disk for both arms but carries an empty score list on one.
    check_row_ids = list(row_ids)

    def _verdict(
        *,
        rows_paired: int = 0,
        rows_excluded: int = 0,
        mean_diff: float | None = None,
        ci_low: float | None = None,
        ci_high: float | None = None,
        effect_size: float | None = None,
        p_value: float | None = None,
    ) -> ExecutionGateVerdict:
        """The verdict every return path builds, differing only in the counts and the statistics.

        Explicit keyword-only parameters rather than ``**overrides`` splatted over a base dict: a
        splat means a mistyped or renamed field silently becomes ``None`` on a model whose whole
        job is to say what a promotion decision rests on. Kept as a closure — unlike
        ``activation_gate``'s dict, it has seven call sites and closes over values every path needs.
        """
        return ExecutionGateVerdict(
            incumbent_variant=incumbent_variant,
            candidate_variant=candidate_variant,
            suite_id=suite_id,
            confidence=confidence,
            n_resamples=n_resamples,
            rows_paired=rows_paired,
            rows_excluded=rows_excluded,
            mean_diff=mean_diff,
            ci_low=ci_low,
            ci_high=ci_high,
            effect_size=effect_size,
            p_value=p_value,
            # Read from the enclosing scope at CALL time, so a return path that runs after a
            # refusal was set carries it without every call site repeating the keyword.
            gate_refusal=cause.reason,
            mde=mde,
            dead_weight=dead_weight,
            primary_criterion_index=primary_criterion_index,
            primary_mean_diff=primary_mean_diff,
            integrity_checks=_integrity_checks(
                incumbent_rows=incumbent_rows,
                candidate_rows=candidate_rows,
                row_ids=check_row_ids,
                engagement_criterion_index=engagement_criterion_index,
            ),
            guardrails=cost_latency_guardrails(
                incumbent_rows=incumbent_rows,
                candidate_rows=candidate_rows,
                row_ids=check_row_ids,
                materiality=materiality,
                seed=seed,
                confidence=confidence,
                n_resamples=n_resamples,
            ),
            # Pydantic copies this list, so every note must already be in it. `_verdict` is
            # therefore always the LAST thing a return path does.
            notes=notes,
        )

    if incumbent_variant == candidate_variant:
        return _verdict()

    experiment_json = run_dir / "experiment.json"
    if not experiment_json.is_file():
        cause.record(
            f"there is no experiment file at {experiment_json}, so the paired statistic could not be "
            + "computed at all. A plain `coder-eval run` without `-e <experiment>` writes none, and "
            + "this track's gate is a two-variant experiment. Re-run the gate with its "
            + "round<N>-gate.yaml."
        )
        return _verdict()
    try:
        result = ExperimentResult.model_validate_json(experiment_json.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # OSError as well as ValueError: the docstring promises "Never an exception", and an
        # unreadable file (permissions, or one that vanished between the is_file() and the read) is
        # exactly as much a wiring fault as a malformed one — with the same right answer.
        logger.warning("Failed to load %s for the execution gate", experiment_json, exc_info=True)
        cause.record(
            f"the experiment file at {experiment_json} could not be read or parsed, so no statistic "
            + "was computed. Check the file is present, readable and complete — a run killed while "
            + "writing it leaves a truncated one."
        )
        return _verdict()

    # Only this suite's rows. `expand_dataset` writes row task ids as `<suite_id>/<row_id>`, so
    # both forms are kept: the fanned rows and an unfanned single-task suite.
    scoped_scores = {
        variant_id: {
            task_id: scores
            for task_id, scores in per_task.items()
            if task_id == suite_id or task_id.startswith(f"{suite_id}/")
        }
        for variant_id, per_task in result.per_replicate_scores.items()
    }
    scoped = copy_with(result, per_replicate_scores=scoped_scores)
    comparison = paired_comparison(scoped, confidence)
    if comparison is None:
        # `variant_ids` is deliberately NOT narrowed to force a comparison out of an N-variant
        # file: that would compute a Stage B verdict from Stage A data — one replicate per row,
        # arms chosen on those same rows — which is precisely what the method forbids.
        cause.record(
            f"no paired comparison: {experiment_json} declares {len(result.variant_ids)} variant(s) "
            + f"({', '.join(result.variant_ids) or 'none'}) and no row of {suite_id!r} scored on both, "
            + "or the file predates per-replicate scores. Check the suite id first — the one above is "
            + "what was searched for. If the variant count is not EXACTLY two, that is the other cause: "
            + "the statistic fires only for two, so gate one candidate at a time in its own "
            + "round<N>-gate.yaml, since re-passing the triage experiment produces no paired block."
        )
        return _verdict()

    if candidate_variant == comparison.vid_a:
        sign = 1.0
    elif candidate_variant == comparison.vid_b:
        sign = -1.0
    else:
        cause.record(
            f"candidate_variant={candidate_variant!r} is not one of the two variants the experiment "
            + f"compared ({comparison.vid_a!r}, {comparison.vid_b!r}), so no sign could be resolved and "
            + "no statistic is reported. Check the variant id against the experiment file."
        )
        return _verdict(rows_paired=comparison.task_count, rows_excluded=comparison.excluded_count)
    if incumbent_variant not in (comparison.vid_a, comparison.vid_b):
        # Fails CLOSED, like its candidate-side sibling above. It used to annotate and fall
        # through, which reported a real, significant difference against whichever arm the file
        # happened to carry — under a header naming the arm the caller asked for.
        cause.record(
            f"incumbent_variant={incumbent_variant!r} is not one of the two variants the experiment "
            + f"compared ({comparison.vid_a!r}, {comparison.vid_b!r}), so the difference could not be "
            + "resolved against the arm you named and no statistic is reported. Check the variant id "
            + "against the experiment file."
        )
        return _verdict(rows_paired=comparison.task_count, rows_excluded=comparison.excluded_count)

    # The rows the statistic was actually computed over — `paired_comparison`'s own rule, applied
    # to the same scoped copy it was handed, so the checks below guard the number above rather than
    # a neighbouring sample.
    per_a, per_b = scoped_scores.get(comparison.vid_a, {}), scoped_scores.get(comparison.vid_b, {})
    prefix = f"{suite_id}/"
    check_row_ids = sorted(
        task_id.removeprefix(prefix) for task_id in set(per_a) & set(per_b) if per_a[task_id] and per_b[task_id]
    )
    if comparison.excluded_count:
        notes.append(
            f"{comparison.excluded_count} row(s) scored for one arm only and were excluded from the "
            + "pairing. An asymmetric sample produces confident nonsense — find out why before "
            + "reading the interval, and note that the guardrails and integrity checks below are "
            + "computed over the PAIRED rows, so they cannot see what is missing either."
        )

    def _signed(value: float | None) -> float | None:
        return None if value is None else sign * value

    # HOISTED above the diagnostics below, which used to assign these three and which the final
    # `return _verdict(...)` reads. All three are pure — `_signed` has no side effects — so
    # computing them earlier changes nothing; what must NOT move is the order the causes are
    # detected in, and that order lives inside `_execution_diagnostics`, which still evaluates
    # `empty_arms` first.
    mean_diff = _signed(comparison.mean_diff)
    # Cohen's d carries the direction too, so it is signed with the rest.
    effect_size = _signed(comparison.effect_size)
    # Negating an interval reverses it, so re-order rather than reporting a "low" above its "high".
    bounds = sorted(b for b in (_signed(comparison.ci_low), _signed(comparison.ci_high)) if b is not None)

    # The predeclared primary, as a READING, computed over `check_row_ids` — the rows
    # `paired_comparison` actually paired.
    #
    # It sits here rather than beside the noise floor for the ORDERING reason below, and the sample
    # question is worth being exact about rather than overclaiming, which an earlier version of this
    # comment did. THREE magnitudes reach this block from three sources: `mean_diff` from
    # `experiment.json`, `primary_mean_diff` from the on-disk criterion results over `check_row_ids`,
    # and `dead_weight` from the on-disk results over `row_ids`. The conversion a reader performs —
    # `mean_diff ~= primary x (1 - dead_weight)` — is therefore exact only while all three samples
    # coincide, which is the ordinary case and NOT guaranteed: a row `experiment.json` scored but whose
    # `task.json` is absent or unparseable is in one sample and not the others. `_sample_divergence`
    # below says so on the block rather than leaving the reader to trust an identity that has quietly
    # stopped holding.
    #
    # The consequence, stated rather than hidden: a return path ABOVE this one carries no primary. A
    # verdict with no statistic has no primary either, so there is nothing to report there — unlike
    # `dead_weight`, which is a property of the suite rather than of the comparison.
    #
    # BEFORE `_execution_diagnostics`, and that ordering is load-bearing rather than tidy: that
    # function takes `refused_already` and uses it to SUPPRESS every note that would contradict a
    # refusal headline. Setting this refusal after it produced exactly the contradiction those
    # guards exist to prevent — measured: a `NOT A RESULT — primary_criterion_index=7 selected no
    # usable row` headline above notes reading "this is an ordinary negative result and not a
    # measurement problem" and "the paired interval is tighter than this suite's own noise floor".
    if primary_criterion_index is not None:
        primary_diffs = _paired_criterion_diffs(
            incumbent_rows=incumbent_rows,
            candidate_rows=candidate_rows,
            row_ids=check_row_ids,
            criterion_index=primary_criterion_index,
        )
        primary_usable = len(primary_diffs)
        if primary_diffs:
            primary_mean_diff = mean(primary_diffs)
        elif check_row_ids:
            # `require_valid_criterion_index` bounds only BELOW — deliberately, since rows may
            # legitimately differ in criteria count and an over-long index should skip a row rather
            # than raise. That is the wrong answer HERE: an over-long primary index makes `row_score`
            # return None on every row, so the vector is EMPTY and indistinguishable from a suite of
            # rows that all errored on that criterion.
            cause.record(
                f"primary_criterion_index={primary_criterion_index} selected no usable row on either "
                + f"arm across {len(check_row_ids)} paired row(s), so no primary effect could be "
                + "reported. The index is the criterion's POSITION in the suite's success_criteria "
                + "list — check it against the suite rather than reading the blended difference as "
                + "the primary one."
            )

    if (
        divergence := _sample_divergence_note(
            experiment_rows=comparison.task_count, primary_rows=primary_usable, on_disk_rows=len(row_ids)
        )
    ) is not None:
        notes.append(divergence)

    refusal, diagnostics = _execution_diagnostics(
        incumbent_rows=incumbent_rows,
        candidate_rows=candidate_rows,
        incumbent_variant=incumbent_variant,
        candidate_variant=candidate_variant,
        suite_id=suite_id,
        run_dir=run_dir,
        comparison=comparison,
        mean_diff=mean_diff,
        effect_size=effect_size,
        mde=mde,
        bounds=bounds,
        refused_already=cause.reason is not None,
    )
    if refusal is not None:
        cause.record(refusal)
    notes.extend(diagnostics)

    return _verdict(
        rows_paired=comparison.task_count,
        rows_excluded=comparison.excluded_count,
        mean_diff=mean_diff,
        ci_low=bounds[0] if len(bounds) == 2 else None,
        ci_high=bounds[1] if len(bounds) == 2 else None,
        effect_size=effect_size,
        p_value=comparison.p_value,
    )


def _execution_diagnostics(
    *,
    incumbent_rows: dict[str, list[EvaluationResult]],
    candidate_rows: dict[str, list[EvaluationResult]],
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    run_dir: Path,
    comparison: PairedComparison,
    mean_diff: float | None,
    effect_size: float | None,
    mde: float | None,
    bounds: list[float],
    refused_already: bool,
) -> tuple[str | None, list[str]]:
    """Every check that runs AFTER the paired statistic is signed: (refusal cause, notes).

    Five independent findings in a linear ladder — an arm with no rows on disk, zero-variance
    paired differences, a difference under the suite's own noise floor, a floor that could not be
    priced, and an interval tighter than that floor. Each is a pure function of values the gate has
    already computed, none of them can return early, and together they were most of what took
    :func:`execution_gate` to F(50).

    Returns the FIRST refusal cause rather than writing the caller's own :class:`FirstCause`, so
    the gate's first-cause-wins ordering stays in one place: every cause here ranks below every
    cause the gate found before the statistic, which is the precedence the zero-row comment states.
    Two setters for one field is exactly the state ``FirstCause`` collapsed, and a helper writing
    into the caller's sink could not be tested without building a gate around it.

    ``refused_already`` is what the two advisory notes suppress on — a note explaining a number
    printed under a refusal headline contradicts it. It is OR-ed with a cause found here, because
    "nothing has refused yet" has to include what this function itself decided three lines up.

    **It is reachable as ``True`` today, and the way it used to claim otherwise was a trap.** This
    docstring said "False at the only call site" — but the TREE-RECONCILIATION cause does not
    return: it records a cause and then ``break``s out of its variant loop, so control reaches the
    call site with the gate's ``FirstCause`` already set and ``refused_already=True``. A reader who
    believed the old sentence would delete the two ``not refused_already`` guards below as dead
    code, and a contaminated run would immediately print the MDE and tighter-than-floor advisories
    under a ``NOT A RESULT`` headline — the exact contradiction those guards exist to prevent.

    Every OTHER cause the gate finds before the statistic does return early, so the reconciliation
    one is currently the only path that arrives here already refused. The parameter keeps that a
    fact about the caller rather than an assumption baked in here.
    """
    notes: list[str] = []
    # First cause wins — see `FirstCause`, which owns the rule.
    cause = FirstCause()

    # The statistic comes from `experiment.json` while every check comes from the on-disk row tree,
    # so the two can disagree — and a valid experiment file beside a mistyped variant, suite or run
    # directory renders as PROMOTED with every check a green `— -> —`. `activation_gate` carries
    # this note for the same reason; without it the silent-zero failure mode is loud on one track
    # and silent on the other.
    #
    # Refused HERE, after the experiment-file and variant-id causes above, even though the rows
    # were loaded at the top. `FirstCause` keeps the first cause recorded, so the call order IS the
    # precedence — and a mistyped variant id makes that arm load zero rows as a CONSEQUENCE, so
    # refusing on the consequence first would replace a message naming the two ids the experiment
    # actually carries with one that can only say "a wrong variant id, suite id or run directory".
    # What is left for this cause is the case the others cannot see: every id correct, and the rows
    # still not on disk.
    empty_arms = [
        (arm, variant_id)
        for arm, variant_id, rows in (
            ("incumbent", incumbent_variant, incumbent_rows),
            ("candidate", candidate_variant, candidate_rows),
        )
        if not rows
    ]
    if empty_arms:
        # ONE refusal naming every empty arm, not one message per arm: the loop this replaces
        # appended twice when both arms were empty, saying the same thing about a single fault.
        named = " and ".join(f"the {arm} arm ({variant_id!r})" for arm, variant_id in empty_arms)
        cause.record(
            f"{named} loaded ZERO rows: nothing matched "
            # `<variant>` literally: this one message names BOTH arms, so it cannot spell either id.
            + f"{task_json_pattern('<variant>', suite_id)} under {run_dir}. That is a wrong variant "
            + "id, a wrong suite id or a wrong run directory. Every guardrail and integrity check "
            + "below is computed over the rows that DID load, so they all pass — over nothing when "
            + "both arms are empty, and as a large candidate improvement when only one is — and the "
            + "paired statistic comes from experiment.json, which can still be perfectly valid. "
            + "Fix the path before reading anything below."
        )

    if comparison.task_count < 2:
        # A refusal, not a note: no interval can be computed at all, so there is nothing here for a
        # reader to weigh — and rendering it as NOT PROMOTED says the candidate lost a comparison
        # that never happened. The row count is still carried on the verdict, so an eroded sample
        # (say 3 incumbent rows against 1 candidate row) is visible as `paired 1 · excluded 2`
        # rather than being flattened into the message.
        cause.record(
            f"only {comparison.task_count} row(s) of {suite_id!r} scored on both arms — fewer than the 2 a "
            + "paired interval needs, so every statistic is unavailable rather than fabricated. Check "
            + "why the rows did not pair before reading anything below; an asymmetric sample is the "
            + "usual cause, and `rows_excluded` above says how many were dropped."
        )
    # `paired_comparison` has one other way to return an all-`None` statistic on a sample big enough
    # for one: `paired_t_ci` declines on a NON-FINITE score. That cannot arrive through this
    # function, and the reason is worth recording rather than guarded against twice — pydantic's
    # JSON validator REJECTS `NaN` / `Infinity`, so such a file never parses and is already reported
    # by the read's own note above. A guard here would be an unreachable branch claiming otherwise.

    # No `refused_already` guard: the sink keeps the first cause, and every cause above this
    # one outranks it. If the rows never loaded, whether their differences vary is moot.
    if mean_diff is not None and effect_size is None:
        # Cohen's d is undefined exactly when stddev(diffs) == 0 — two arms differing by an
        # IDENTICAL amount on every row. The interval collapses to a point either way, so
        # `excludes_zero` and `favours_candidate` stop meaning what they read as. This is the
        # execution track's analogue of the activation track's discreteness refusal: a statement
        # about the sample, not about the candidate. It REPLACES the note that used to describe the
        # same condition — one message per finding.
        #
        # TWO messages, split exactly where `holm_promote`'s discreteness refusal splits its own
        # (`p_floor >= 1.0`) and for the same reason: at a constant difference of zero the arms
        # behaved IDENTICALLY, which is a finding about the candidate that no number of extra rows
        # can change — and `paired_t_test` reports p = 1.0 there rather than the 0.0 a non-zero
        # constant shift gives, so a single message would state a p the block below it contradicts.
        if mean_diff == 0.0:
            cause.record(
                "the two arms produced an identical per-row score on every one of the "
                + f"{comparison.task_count} paired row(s), so there is nothing for any test to "
                + "separate — the paired difference is exactly zero with zero variance, and the "
                + "paired t reports p = 1.0000 over a zero-width interval. That is a result about "
                + "this candidate rather than about the suite: adding rows cannot change it. Check "
                + "the candidate actually differs from the incumbent, and that both arms were wired "
                + "to the snapshots you think they were."
            )
        else:
            cause.record(
                f"the two arms differed by exactly {mean_diff:.3f} on every one of the "
                + f"{comparison.task_count} paired row(s), so the paired differences carry zero "
                + "variance. A paired t on a constant non-zero difference reports p = 0.0000 and a "
                + "zero-width interval whatever the effect actually is — every promotion condition "
                + "holds at once and none of them measured anything. This is not a result about the "
                + "candidate. Add rows whose difference the two arms do NOT agree on, or add "
                + "replicates so within-row spread can appear; a larger family or a smaller alpha "
                + "cannot help."
            )
    # The zero-variance case above is the LIMIT of a continuum, not an isolated point: two arms can
    # differ by ALMOST the same amount on every row, and the paired t is then almost as overconfident
    # — measured, 4 rows differing by 0.400, 0.400, 0.400, 0.401 report p = 5.4e-10. Two different
    # things go wrong there, and they need opposite responses, so they are two checks and only one
    # of them refuses.
    #
    # (1) A CONFIDENT CLAIM ABOUT AN EFFECT BELOW WHAT THE SUITE CAN RESOLVE. `mde` is the
    # half-width of a bootstrap interval on a NULL difference — the incumbent's own replicates split
    # against each other, where the true difference is zero by construction — so it is what this
    # suite's run-to-run noise actually is. A difference under it is indistinguishable from that
    # noise however small the p is.
    #
    # Conditioned on the interval EXCLUDING ZERO, and that conjunct is what keeps the refusal from
    # swallowing the commonest honest outcome. Under the null a candidate's difference is small, so
    # `abs(mean_diff) < mde` is true for nearly every candidate that simply does not work —
    # measured, 40 of 40 true-null candidates. Refusing all of them would retire NOT PROMOTED
    # almost entirely and send the reader to buy replicates for a candidate whose problem is that it
    # is null. An interval that CONTAINS zero is the data agreeing it is null: an ordinary negative
    # result, and it stays one. What is left here is the pathology — a confident claim, in either
    # direction, about an effect the instrument cannot see.
    excludes_zero_either_way = len(bounds) == 2 and (bounds[0] > 0.0 or bounds[1] < 0.0)
    if mde is not None and mde >= FLOOR_RESOLUTION and mean_diff is not None and abs(mean_diff) < mde:
        if excludes_zero_either_way:
            cause.record(
                f"the observed difference ({mean_diff:.3f}) is smaller than this suite's minimum "
                + f"detectable effect ({mde:.3f}) on weighted_score — the half-width of a null "
                + "comparison that split the incumbent's own replicates, where the true difference is "
                + "zero by construction — and yet the interval excludes zero. That is a confident "
                + "claim about an effect this suite cannot see, not a result about the candidate. "
                + "Lower the floor with more replicates or more rows, or find rows where the "
                + "candidate's effect is larger."
            )
        elif not refused_already and cause.reason is None:
            # Below the floor AND consistent with zero: the ordinary "it did not help" outcome.
            #
            # Guarded like the two advisories below it, and it was the ONE note in this ladder that
            # was not. It calls itself "an ordinary negative result", which is a negative-result
            # claim — so under a refusal it contradicted the headline directly above it. Reproduced
            # through the real gate: a zero-variance refusal ("there is nothing for any test to
            # separate") printed this sentence beneath `NOT A RESULT`, which is precisely the
            # defect the activation ladder's guard removed. `promoted` was unaffected (a refusal
            # forces False), so this was prose only — on the page a user pastes into a ledger.
            notes.append(
                f"the observed difference ({mean_diff:.3f}) is smaller than this suite's minimum "
                + f"detectable effect ({mde:.3f}), and the interval contains zero — so this is an "
                + "ordinary negative result and not a measurement problem. The suite could not have "
                + "resolved an effect this small either way; a candidate that does not help reads "
                + "exactly like this."
            )
    # And when there is NO floor, say so rather than skipping the check silently. Both refusals
    # above are inert without a positive `mde`, and a floor of exactly 0.000 is common: the null
    # split reduces to zero whenever every row carries the same replicate pattern, which two
    # replicates on a deterministic suite produce. A reader who is not told this reads "Minimum
    # detectable effect: 0.000" as "this suite can resolve anything", which is the opposite of what
    # an unmeasurable floor means. Advisory, and suppressed under a refusal that already explains
    # the block.
    #
    # Suppressed on `refused_already` OR a cause found above: inside this ladder "nothing has
    # refused yet" has to include what the zero-variance branch decided three lines up, or a
    # zero-variance verdict starts printing a floor note under its own refusal headline.
    if (
        not refused_already
        and cause.reason is None
        and mean_diff is not None
        and (mde is None or mde < FLOOR_RESOLUTION)
    ):
        notes.append(
            "this suite's minimum detectable effect came back "
            + (f"{mde:.3f}" if mde is not None else "unavailable")
            + ", so the difference above was NOT checked against a noise floor. A null split "
            + "measures zero only when every row's replicates agreed exactly — a deterministic "
            + "suite, or one whose rows all failed the same way — so read it as 'the floor could "
            + "not be priced', never as 'this suite can resolve anything'. Raise --repeats, and "
            + "check the rows actually ran, before treating a small difference here as an effect."
        )
    # (2) THE INTERVAL IS TIGHTER THAN THE FLOOR. A caveat, deliberately NOT a refusal: the paired
    # t's interval comes from the BETWEEN-ROW spread of the differences, which is tiny whenever the
    # arms differ by a similar amount on every row, while `mde` measures WITHIN-row noise the t
    # never sees. So a real, large, consistent win reports an absurd p — and refusing it would be
    # worse than the defect: measured, a genuine 8-row 0.30 win reports a half-width of 0.007, the
    # same shape as the 0.400-on-every-row case above. What is wrong there is the reported
    # PRECISION, not the decision, so the block says so and lets the decision stand.
    half_width = (bounds[1] - bounds[0]) / 2.0 if len(bounds) == 2 else None
    if (
        half_width is not None
        and mde is not None
        and mde >= FLOOR_RESOLUTION
        and half_width < mde
        and not refused_already
        and cause.reason is None
    ):
        notes.append(
            "the paired interval is tighter than this suite's own noise floor: a half-width of "
            + f"{half_width:.4f} against a minimum detectable effect of {mde:.3f}. The t-interval is "
            + "computed from the between-row spread of the differences, which is small whenever the "
            + "two arms differ by a SIMILAR amount on every row; the floor measures the run-to-run "
            + "noise the same suite actually has. Whichever way the difference went, it is larger "
            + "than the floor, so the DECISION above stands — but do not quote this p or this "
            + "interval as the precision of the result, and do not read a p far below the floor as "
            + "extra confidence."
        )

    return cause.reason, notes


def confirm_gate_execution(
    *,
    train_verdict: ExecutionGateVerdict,
    confirm_run_dir: Path,
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    engagement_criterion_index: int | None = 0,
    primary_criterion_index: int | None = None,
    materiality: float = MATERIALITY_FLOOR,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = GATE_RESAMPLES,
) -> ConfirmVerdict:
    """Stage C on the execution track: did the Stage B effect REPRODUCE on the held-out split?

    Runs this track's own gate on the confirm run directory and classifies the train -> test delta
    through :func:`~coder_eval.optimize.gate.classify_confirm`, the shared four-way rule. Stage C used
    to be prose — "report that block verbatim alongside the test numbers" — which left the reader to
    eyeball two intervals and decide whether one reproduced the other.

    **Exactly ONE candidate.** Confirming two would spend the held-out split on SELECTION, which is
    the failure the "never re-rolled" integrity rule exists to prevent, so a sequence raises rather
    than being iterated. State it in the signature and it is still worth the guard: a caller with a
    shortlist in hand will try.

    **A family of ONE, and that is correct rather than an oversight.** Only the Stage B winner is
    confirmed, so there is no multiplicity to correct — Holm is applied at ``m = 1`` purely so the
    carried block is a DECIDED one rather than rendering as ``UNDECIDED``.

    **The confirm run must have recorded ``--split test``.** Read through
    :func:`~coder_eval.optimize.load.read_split_provenance`, the existing reader — a second reader of
    ``run.json``'s ``row_selection.split`` is the duplication this repo removes on sight. The two
    non-``test`` cases differ and are treated differently: an UNRECORDED split is a NOTE (a run
    predating the field), while a recorded ``train`` is a REFUSAL — that is Stage C silently
    re-running the train rows, at full price, with no error anywhere.

    **``test_mde`` costs nothing here.** :func:`execution_gate` already measures the replicate noise
    floor unconditionally on whatever run dir it is handed, so the confirm split's own resolution is
    simply ``test_verdict.mde``. A second measurement would be a duplicate estimator — the
    CE037/CE040 defect class — and would double every confirm's bootstrap cost.

    **The margin is the confirm split's own MDE on the metric this track gates**, which is what makes
    it per-track without a second rule: the activation twin passes ITS gate's floor on ``f1.yes`` to
    the same classifier. Picking a different multiple on one track would be a second declaration of
    "how much shrinkage is real".

    Never raises on a wiring fault — like both gates, every such state becomes a refusal.
    """
    confirm_one_candidate(candidate_variant)

    notes: list[str] = []
    # The FIRST cause that makes this not a comparison — see `FirstCause`.
    cause = FirstCause()

    if (train_note := confirm_train_note(train_verdict.promoted)) is not None:
        notes.append(train_note)
    if (train_refusal := confirm_train_refusal(train_verdict.gate_refusal)) is not None:
        cause.record(train_refusal)

    split_refusal, split_note = confirm_split_check(read_split_provenance([confirm_run_dir]), [confirm_run_dir])
    if split_note is not None:
        notes.append(split_note)
    if split_refusal is not None:
        cause.record(split_refusal)

    test_verdict = holm_promote_execution(
        [
            execution_gate(
                run_dir=confirm_run_dir,
                incumbent_variant=incumbent_variant,
                candidate_variant=candidate_variant,
                suite_id=suite_id,
                engagement_criterion_index=engagement_criterion_index,
                primary_criterion_index=primary_criterion_index,
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
        # READ off the Stage B verdict, never recomputed, so the two numbers this block compares
        # cannot disagree with the blocks they were each reported in.
        train_effect=train_verdict.mean_diff,
        test_effect=test_verdict.mean_diff,
        test_mde=test_verdict.mde,
        test_verdict=test_verdict,
        confirm_refusal=cause.reason,
        notes=notes,
    )


def _execution_notes(
    verdict: ExecutionGateVerdict,
    *,
    p_value: float,
    rejected: bool,
    refusal: str | None,
    family_size: int,
    alpha: float,
) -> list[str]:
    """Every note :func:`holm_promote_execution` adds to a MEASURED verdict, in rendered order.

    The twin of :func:`~coder_eval.optimize.activation._activation_notes`, and a pure function of
    one verdict plus the family facts: nothing here decides anything, and ``promoted`` is not read.

    ``p_value`` is passed rather than read off the verdict, making the contract explicit — this
    ladder runs on the MEASURED branch only, so it needs no coercion that would quietly render
    ``p = 0.0000`` for a verdict that never had a p.

    ``refusal`` is the message rather than a bool, mirroring ``_activation_notes`` exactly. Neither
    ladder RENDERS it — both use it as a presence check — and the two taking the same parameter is
    what lets a reader carry one habit between the tracks. This track's refusal happens to be read
    off the verdict rather than computed, which is the hook's business and not the ladder's.

    Every rung here is a NEGATIVE-RESULT claim and every one is suppressed under a refusal: a
    refusal says the comparison decided nothing, and a claim beneath it is a second, contradictory
    one. The two trailing notes are not such claims and belong to ``decide_family``, which appends
    them for both tracks. The activation ladder draws the line in the same place.
    """
    notes: list[str] = []
    # Kept on the two COMPONENTS rather than on `separated`, so a candidate that merely lost still
    # reads "the paired difference favours the incumbent" instead of the blunter "did not separate"
    # — which of the two failed is the whole content of the note.
    favours_candidate = verdict.mean_diff is not None and verdict.mean_diff > 0.0
    excludes_zero = verdict.ci_low is not None and verdict.ci_low > 0.0
    if refusal is None:
        if rejected and not favours_candidate:
            notes.append(
                "not promoted: the paired difference favours the incumbent. (The sign is already "
                + "resolved as candidate - incumbent, so this reads the way it looks.)"
            )
        elif rejected and not excludes_zero:
            notes.append(NOTE_CI_CONTAINS_ZERO)
        elif not rejected:
            notes.append(note_ordinary_negative(p_value, family_size, alpha))
        # Guarded on exactly what makes the sentence true — the three conjuncts the BLOCKED
        # headline is keyed on, and the same guard the activation twin applies. Under a refusal the
        # headline is NOT A RESULT; on a candidate that merely LOST the check forced nothing, since
        # `promoted` was already False without it. Both states used to print a note claiming a veto
        # and citing a headline the block does not carry. The failed check is still visible in the
        # rendered Integrity checks / Guardrails lists on every path — only the CLAIM is withheld.
        if rejected and verdict.separated:
            notes += [note_check_failed(name) for name in verdict.failed_vetoes]
    return notes


def holm_promote_execution(
    verdicts: list[ExecutionGateVerdict], alpha: float = DEFAULT_ALPHA
) -> list[ExecutionGateVerdict]:
    """Decide a whole execution-track family at once — the second, and only other, Holm call site.

    A thin wrapper over :func:`~coder_eval.optimize.gate.decide_family`, which owns the Holm loop,
    the ``promoted`` conjunction and the two trailing notes for BOTH tracks. What is left here is
    this track's note ladder and the fact that its refusal is READ rather than computed.

    Gating candidates one at a time against the same incumbent on the same train rows IS a family,
    even though each gate is its own two-variant run directory. So the correction is applied once,
    across every verdict a round predeclared it would gate, exactly as :func:`holm_promote` does on
    the other track — literally the same loop now. Correcting per candidate would degenerate to an
    uncorrected ``p <= alpha``.

    **``promoted`` is Holm rejecting AND ``verdict.separated`` AND no refusal AND no failed
    integrity check or guardrail** — one expression in ``decide_family``, applied to both tracks.
    The veto lives in the DECISION, not only in the render: a
    caller reading ``promoted`` must not be able to promote a candidate whose rendered block says
    BLOCKED. Folding it in is only safe because the STATISTICAL half has its own name —
    ``ExecutionGateVerdict.separated``, the property :func:`render_execution_markdown` keys its
    BLOCKED headline on. Read ``promoted`` there instead and that headline becomes unreachable the
    moment this fold lands, silently degrading a blocked winner to the ordinary NOT PROMOTED rung.

    **Both tracks now mean the same thing by ``promoted``**, and every check list on either one
    vetoes: :func:`holm_promote` folds in its ``sibling_checks`` AND its cost/latency
    ``guardrails``, this one folds in its ``integrity_checks`` AND the same ``guardrails``. The
    only remaining difference is which lists each track HAS — ``integrity_checks`` are engagement /
    completion-rate reads and exist only here, because a promotion cannot be correct in spite of a
    sample that did not engage the thing under test. The refusal conjunct is not of either kind —
    see below.

    A verdict with no ``p_value`` is outside the family and comes back ``promoted=False``.

    **There is a refusal state, and it is a different condition from the activation track's.** The
    paired *t* is continuous, so this track has no discreteness floor to refuse against — but it
    does have several degenerate states — no comparison to make at all, an arm that loaded no rows,
    too few rows paired, zero-variance paired differences, a confident claim about a difference
    below the suite's own MDE. ``execution_gate``
    detects each where it is already computed and sets ``gate_refusal``; this function only READS
    it, forcing ``promoted=False`` and suppressing the negative-result notes, which would otherwise
    contradict the refusal headline.

    **A refused verdict with a real p STAYS in the family**, and the membership rule is
    ``p_value is not None`` and nothing else. Holm corrects for the hypotheses actually tested, and
    a candidate that was gated and measured was tested however degenerate its sample turned out to
    be — dropping it shrinks ``m`` and LOOSENS ``alpha/m`` for its siblings, which is the
    uncorrected-``p <= alpha`` degeneration from the other direction (measured: three gate runs
    with two below-MDE refusals promoted a p = 0.027 sibling that a family of three rejects).
    A refusal whose cause is that there was no comparison at ALL already has no p, so it is outside
    the family by the same rule, without a second one. The cost of keeping the others in is a
    genuine candidate reading NOT PROMOTED because a sibling's sample was degenerate — which is the
    multiplicity that was actually incurred, and the conservative direction.
    """

    def decide(verdict: ExecutionGateVerdict, facts: FamilyFacts) -> TrackDecision:
        # `p_value` is not None on this branch — `decide_family` returns before calling the hook
        # otherwise — and the ladder depends on that, so it is narrowed here rather than coerced
        # inside a ladder that would render `p = 0.0000` for a verdict that never had a p at all.
        assert verdict.p_value is not None, "decide_family calls the hook on the measured branch only"
        # READ, never re-derived: `execution_gate` sets this where each condition is already
        # computed. It is LOAD-BEARING rather than belt-and-braces — a zero-variance verdict has
        # p = 0.0000 and a zero-width interval, so `separated` holds on it too.
        return TrackDecision(
            verdict.gate_refusal,
            _execution_notes(
                verdict,
                p_value=verdict.p_value,
                rejected=facts.rejected,
                refusal=verdict.gate_refusal,
                family_size=facts.family_size,
                alpha=facts.alpha,
            ),
        )

    return decide_family(verdicts, alpha, decide=decide)
