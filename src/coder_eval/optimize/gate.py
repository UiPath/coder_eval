"""The decision primitives BOTH optimize-skill tracks share.

Rank 1 of the optimize family: it imports from :mod:`coder_eval.optimize.load` and from nothing
else in the family, and both track modules import from it. What lives here is what is neither
track's alone — the gate-wide constants, the notes both Holm wrappers emit verbatim, the
noise-floor refusal channel and the cluster half both floors share, the cost/latency guardrails
both gates run, :func:`holm_family`, the ONE
:func:`~coder_eval.reports_stats.holm_rejections` call site, and :func:`decide_family`, the ONE
promotion loop — which is where the single ``promoted`` conjunction lives, and therefore the single
answer to "what does this gate promote on".

The two gates themselves live one rank up, in :mod:`coder_eval.optimize.activation` and
:mod:`coder_eval.optimize.execution`. The split is BY TRACK because the activation/execution pair
is this feature's dominant organising principle — almost every concept in it is a two-track pair —
and because the module it was carved out of had grown to 3,500 lines with an E-grade function.

**A library, not a CLI**, like every module in the family: no typer, no rich, no reach into the
CLI package. The skill drives these functions from a short inline ``python`` snippet.

**F1 is never recomputed here.** Every metric comes from
:func:`coder_eval.criteria._classification_aggregate.classification_metrics`, the criterion layer's
own routine (CE037), so the gate cannot disagree with the numbers the run reported.

**Four of the constants below are WATCHED** by the estimator-change protocol
(``tests/lint/estimator_ledger.py``), and so is :data:`FLOOR_RESOLUTION`, which moved here from the
execution track so the shared confirm classifier could apply it rather than the activation side
declaring a second copy. Touching a watched constant — including moving the file it is declared in —
obliges a row in ``docs/REPORT_SCHEMA.md``'s ``## Estimator changes`` table, because a rendered
statistic can step for identical data and nothing in a run artifact tells that apart from a real
change in the thing being measured.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from coder_eval.criteria._classification_aggregate import classification_metrics
from coder_eval.models import (
    ActivationGateVerdict,
    ConfirmVerdict,
    EvaluationResult,
    ExecutionGateVerdict,
    GateVerdictBase,
    GuardrailCheck,
    NoiseFloor,
    OptimizeMeasurements,
    copy_with,
)
from coder_eval.optimize.load import SplitProvenance, balance_pair, row_cost_levels, row_costs
from coder_eval.optimize.store import UNRESOLVED_MODEL, lookup_noise_floor
from coder_eval.reports_stats import (
    DEFAULT_ALPHA,
    cluster_bootstrap_diff_ci,
    holm_rejections,
    mean,
    median_or_none,
)


logger = logging.getLogger(__name__)

# How large a measured cost/latency increase has to be, relative to the incumbent's median, before
# it is worth reporting as a breach.
#
# It is a SUPPRESSION threshold, not a firing one, and the distinction is the whole design. The
# guardrails fire off a bootstrap interval, so they already refuse to call noise a regression; this
# floor only stops a statistically-real 3% increase from being announced as one. Raising or
# lowering it can therefore make the guardrail quieter or chattier — never turn noise into a veto.
#
# One constant, not one per measurement: the per-row coefficients of variation measured across this
# repo's runs/ tree (cost 0.27, duration 0.23) are close enough that two numbers would be false
# precision, and the bootstrap already handles the difference in spread.
MATERIALITY_FLOOR = 0.25

# How precisely the bootstrap must resolve the threshold it decides against: the Monte-Carlo
# standard error of the p may be at most this fraction of that threshold.
GATE_P_PRECISION = 0.10

# The survivor count the gate is sized to decide. Holm's strictest threshold is alpha/S, so this
# is what sets the resolution requirement. Five is a full Stage A shortlist.
GATE_MAX_FAMILY = 5

# DERIVED, not picked. The bootstrap p is 2*Bin(m, p/2)/m, so SE(p_hat) ~= sqrt(2p/m); requiring
# that error to be at most a fraction k of the threshold it is decided against gives
# m >= 2 / (k^2 * p), evaluated at the strictest Holm threshold p = alpha / S.
#
# Measured against the real machinery on an 8-row fixture (true p ~ 0.0078, 20 seeds): predicted
# SE 0.00088 at m=20,000 against an observed 0.00101 — the closed form is accurate to ~15% and
# errs high, which is the safe direction. At m=2,000 the same fixture's p ranges 0.005-0.019
# across seeds, straddling the alpha/4 threshold it is compared against.
#
# Separate from reports_stats.BOOTSTRAP_RESAMPLES because a GATE decides on the p while a REPORT
# renders it: everything here that feeds a promotion decision uses this count.
GATE_RESAMPLES = math.ceil(2.0 / (GATE_P_PRECISION**2 * (DEFAULT_ALPHA / GATE_MAX_FAMILY)))


# Below this, a measured noise floor is floating-point residue rather than a measurement, and
# EVERY consumer treats it as no floor at all.
#
# At rank 1 rather than on the execution track, where it used to live: `classify_confirm` needs it
# too, and the two rank-2 track modules may not import each other — so keeping it there would
# have meant a second declaration of the same number on the activation side, which is the
# CE037/CE040 defect class. It is not a property of `weighted_score` in any case: both tracks'
# metrics are bounded [0, 1] and reported to three decimals, so a floor nine orders below that is
# residue on either one.
#
# Not a tolerance anyone chose. It exists because a null split over a CONSTANT per-row difference
# returns something like 2.8e-17 rather than exactly 0.0 — measured on this repo's own winning
# fixture — which is not zero, so an `mde == 0.0` test misses it while `abs(diff) < mde` can never
# fire. Every floor-based check then goes silently inert on exactly the degenerate suites it exists
# for, which is why `classify_confirm` compares against this rather than against zero.
FLOOR_RESOLUTION = 1e-9


def classification_metric(pairs: list[tuple[str, str]], name: str) -> float:
    """A classification metric over ``pairs``, through the criterion layer's own routine.

    Empty pairs and an absent metric name both read 0.0, and both conventions are declared in
    ``_classification_aggregate`` rather than here — restating either at this call site would be
    the second declaration CE037 exists to prevent.
    """
    cm = classification_metrics(pairs)
    return cm.metric(name) if cm is not None else 0.0


def _all_finite(values: list[float]) -> bool:
    """Whether every measurement in one row's cluster is usable."""
    return all(math.isfinite(v) for v in values)


def _row_durations(results: list[EvaluationResult]) -> list[float]:
    return [result.duration_seconds for result in results]


def cost_latency_guardrails(
    *,
    incumbent_rows: dict[str, list[EvaluationResult]],
    candidate_rows: dict[str, list[EvaluationResult]],
    row_ids: Sequence[str] | None = None,
    materiality: float = MATERIALITY_FLOOR,
    seed: int = 0,
    confidence: float = 0.95,
    n_resamples: int = GATE_RESAMPLES,
) -> list[GuardrailCheck]:
    """Cost and latency guardrails, derived from the measured spread rather than a fixed percentage.

    A fixed tolerance would veto real wins on noise: the measured per-row coefficient of variation is
    ≈0.25, so the standard error of a median over 12 rows is ≈0.09 and a 15% rule sits about 1.5
    standard errors out. So each guardrail runs the SAME paired cluster bootstrap the F1 gate uses,
    over per-row cost / duration, and fails only when even the optimistic end of the interval
    (``ci_low``) is still a material increase — ``ci_low > materiality * incumbent MEAN``. The mean,
    not the median reported as the level: the interval is on a difference of means, and scaling it
    by a median is a unit mismatch on a right-skewed distribution (see below).

    ``row_ids`` restricts the comparison to a given row set; the gate passes the rows its F1
    comparison actually used, so the guardrail cannot be computed over a different sample than the
    number it is guarding.

    A measurement that does not exist (no turn reported a cost) passes with a ``note`` and ``None``
    values — never a bare pass, which would read as a pass on the merits.
    """
    ids = sorted(set(incumbent_rows) & set(candidate_rows)) if row_ids is None else list(row_ids)
    checks: list[GuardrailCheck] = []

    for name, extract in (("cost (USD/row)", row_costs), ("latency (seconds/row)", _row_durations)):
        # Only rows BOTH arms measured, and balanced to the same observation count per row — the
        # same two rules the F1 gate applies. Without the first, a draw whose rows are all
        # cost-less on one arm pools to `[]`, `mean([])` reads 0.0, and the interval says that arm
        # cost nothing (measured: a 10x cost increase passing with ci_low = -0.1). Without the
        # second, an interrupted invocation reweights the comparison exactly as it does for F1.
        # `.get`, not `[]`: `row_ids` is caller-supplied and need not be the intersection of these
        # two maps. `execution_gate` passes the rows `paired_comparison` paired, which come from
        # `experiment.json` — a row named there but missing from the on-disk tree (a skipped
        # malformed `task.json`, or an unfanned single-task suite) used to raise a KeyError out of
        # the skill's inline snippet, discarding the carefully-composed wrong-path note above it.
        # A row with no measurement on one arm already falls through to the note-and-None path.
        paired = [(extract(incumbent_rows.get(rid, [])), extract(candidate_rows.get(rid, []))) for rid in ids]
        measured = sum(1 for inc, _c in paired if inc), sum(1 for _i, cand in paired if cand)
        comparable = [balance_pair(inc, cand) for inc, cand in paired]
        # A different rule, applied AFTER the trim: drop rows one arm did not measure at all.
        comparable = [(inc, cand) for inc, cand in comparable if inc and cand]
        # And a third: a row carrying a NON-FINITE figure on either arm measured nothing usable, so
        # it goes the same way an unmeasured row does. Dropping it HERE rather than downstream is
        # what makes the medians, the incumbent mean and the bootstrap interval see the SAME rows.
        # They did not: `row_cost_levels` filters non-finite values, so a corrupt row vanished from
        # the reported levels while the raw cluster still reached `cluster_bootstrap_diff_ci` — and
        # `nan <= materiality * mean` is False, which is a VETO. Measured on two IDENTICAL arms with
        # one corrupt row: the block rendered `1.000 -> 1.000`, a relative change of 0.000, and
        # blocked the promotion with nothing on the page saying why.
        #
        # Reachable from a CALLER, not from a run directory: pydantic accepts a non-finite
        # `total_cost_usd` in memory but serialises it as `null`, so a row loaded from `task.json`
        # arrives with no cost rather than a bad one. This function is public and takes the rows it
        # is given — the same reason `TestGuardrailsNeverRaiseOnACallerSuppliedRow` exists.
        usable = [(inc, cand) for inc, cand in comparable if _all_finite(inc) and _all_finite(cand)]
        discarded = len(comparable) - len(usable)
        comparable = usable

        incumbent_clusters = [inc for inc, _c in comparable]
        candidate_clusters = [cand for _i, cand in comparable]
        incumbent_median = median_or_none(row_cost_levels(incumbent_clusters))
        candidate_median = median_or_none(row_cost_levels(candidate_clusters))
        # The floor scales by the incumbent's MEAN, because the interval it is compared against is
        # an interval on the difference of means. Scaling a mean-difference by a median is a unit
        # mismatch on any skewed distribution — and per-row cost is strongly right-skewed, so a
        # uniform 10% increase measured as FAIL against a 25% floor. The medians stay as the
        # reported level, which is the robust thing to READ; the mean is what is being tested.
        incumbent_mean = mean(row_cost_levels(incumbent_clusters)) if incumbent_clusters else 0.0

        if incumbent_median is None or candidate_median is None:
            checks.append(
                GuardrailCheck(
                    name=name,
                    incumbent=incumbent_median,
                    candidate=candidate_median,
                    relative_change=None,
                    tolerance=materiality,
                    passed=True,
                    # The wording is UNCHANGED for the ordinary unmeasured arm, so every pinned
                    # render stays byte-identical; the discard is appended only when there was one.
                    note=(
                        f"no {name.split(' ')[0]} recorded on at least one arm — guardrail not evaluated"
                        + (f" ({discarded} row(s) discarded for a non-finite figure)" if discarded else "")
                    ),
                )
            )
            continue

        notes: list[str] = []
        if discarded:
            # NAMED, never silent: a narrowed comparison a reader cannot see is the same defect as a
            # wrong one, and this is the only place the discard is visible.
            notes.append(f"{discarded} row(s) discarded for a non-finite {name.split(' ')[0]} figure")
        if measured[0] != measured[1]:
            notes.append(f"measured on {measured[0]} incumbent row(s) vs {measured[1]} candidate row(s)")

        bootstrap = cluster_bootstrap_diff_ci(
            candidate_clusters,
            incumbent_clusters,
            mean,
            n_resamples=n_resamples,
            confidence=confidence,
            seed=seed,
        )
        if bootstrap is None:
            notes.append("fewer than 2 comparable rows — no interval, so nothing is claimed")
            passed, ci_low, ci_high = True, None, None
        elif incumbent_mean == 0.0:
            notes.append("the incumbent measured zero, so a relative change is undefined")
            passed, (_diff, ci_low, ci_high, _p) = True, bootstrap
        else:
            _diff, ci_low, ci_high, _p = bootstrap
            passed = ci_low <= materiality * incumbent_mean
            if passed and ci_low > 0.0:
                notes.append("a real increase, but below the materiality floor — reported, not vetoed")

        checks.append(
            GuardrailCheck(
                name=name,
                incumbent=incumbent_median,
                candidate=candidate_median,
                relative_change=(
                    (candidate_median - incumbent_median) / incumbent_median if incumbent_median else None
                ),
                tolerance=materiality,
                passed=passed,
                ci_low=ci_low,
                ci_high=ci_high,
                note="; ".join(notes) or None,
            )
        )
    return checks


def no_floor(reason: str, *, reasons: list[str] | None = None) -> None:
    """Log why a null comparison could not be made, optionally record it, and return None.

    ``reasons`` is an out-parameter SINK rather than a changed return type, and that is the whole
    reason it exists: :func:`noise_floor_mde` is public and imported by the skill's inline
    snippets, so widening its ``float | None`` would break a user's terminal. An optional list the
    caller passes down costs every existing caller nothing and lets ``activation_gate`` render the
    ACTUAL cause instead of guessing at one.

    **Only the ACTIVATION floor threads it today**, and that is stated rather than left to be
    discovered: :func:`measure_execution_noise_floor` calls this function four times and passes
    nothing, so ``_execution_diagnostics``' "the floor came back unavailable" advisory still names
    no cause — the same defect on the other track, recorded in ``.claude/harness-candidates.md``
    rather than half-fixed here.

    Both floor functions return ``None`` for several distinct reasons, and the caller is an agent
    about to decide whether to spend money. A silent ``None`` is indistinguishable from a floor of
    zero to anyone not reading the source — and it was silent: verified against the shipped code,
    ``noise_floor_mde`` with a mistyped run directory returned a bare ``None`` and printed nothing,
    on the one function whose job is to stop a user spending.

    An unconfigured ``logging.warning`` reaches stderr through Python's last-resort handler, so the
    agent driving the skill's inline snippet sees this without any logging setup.
    """
    if reasons is not None:
        reasons.append(reason)
    logger.warning("No noise floor could be computed: %s", reason)
    return None


def floor_from_clusters[T](
    clusters_a: list[list[T]],
    clusters_b: list[list[T]],
    statistic: Callable[[list[T]], float],
    probe: NoiseFloor,
    measurements: OptimizeMeasurements | None,
    *,
    reasons: list[str] | None = None,
) -> NoiseFloor | None:
    """The cache lookup, the bootstrap and the half-width — the half both floors share.

    Generic over the cluster element type because its two callers genuinely differ: the activation
    floor's clusters hold ``(expected, observed)`` label pairs reduced by ``_f1_yes``, the execution
    floor's hold ``weighted_score`` floats reduced by ``mean``. Parameterized exactly the way
    :func:`reports_stats.cluster_bootstrap_diff_ci` already is, rather than unified behind a
    ``split=`` flag — the split axes (invocations vs. replicates) are different computations, and a
    boolean would hide that.
    """
    # An unresolved model must never hit the cache: borrowing a floor measured on another model
    # would be worse than the bootstrap it saves.
    if measurements is not None and probe.model != UNRESOLVED_MODEL:
        cached = lookup_noise_floor(measurements, probe)
        if cached is not None:
            return cached

    bootstrap = cluster_bootstrap_diff_ci(
        clusters_a,
        clusters_b,
        statistic,
        n_resamples=probe.n_resamples,
        confidence=probe.confidence,
        seed=probe.seed,
    )
    if bootstrap is None:
        return no_floor(
            f"the bootstrap declined on {len(clusters_a)} cluster(s) for {probe.suite_id!r} — it needs 2",
            reasons=reasons,
        )
    _diff, ci_low, ci_high, _p = bootstrap
    return copy_with(probe, mde=(ci_high - ci_low) / 2.0)


# The four notes both tracks emit verbatim. They were byte-identical copies in two functions 600
# lines apart; a wording fix applied to one of them would have left the two tracks describing the
# same decision differently in a ledger read back weeks later. Two of them — `note_holm_family` and
# `note_resolution_degraded` — are now emitted by `decide_family` rather than by either track, so
# they have one call site as well as one declaration.
#
# Deliberately NOT collapsed with them: the zero-row and below-MDE notes. Those diverged on purpose
# — the execution track's zero-row case became a `gate_refusal` with different text, and its MDE
# note names `weighted_score` because that is the statistic its gate reads. Two tracks saying
# different things there is the finding, not drift.
NOTE_OUTSIDE_FAMILY = "not promoted: the sample could not support a p-value, so this arm is outside the family."
NOTE_CI_CONTAINS_ZERO = (
    "not promoted: the Holm-corrected test rejects but the confidence interval still "
    "contains zero, so the effect is not separated at the reported interval width."
)


def note_ordinary_negative(p_value: float, family_size: int, alpha: float) -> str:
    return (
        f"not promoted: p = {p_value:.4f} did not clear the Holm threshold for its rank in a "
        f"family of {family_size} (alpha={alpha}). This is the ordinary negative result — the "
        "interval and the effect size above are what to report."
    )


def note_holm_family(family_size: int, alpha: float) -> str:
    return f"Holm applied across a family of {family_size} at alpha={alpha}."


def note_resolution_degraded(family_size: int, n_resamples: int, alpha: float) -> str | None:
    """What resolution the gate ACTUALLY achieved on a family larger than it is sized for.

    :data:`GATE_RESAMPLES` is derived from :data:`GATE_P_PRECISION` at the strictest Holm threshold
    for :data:`GATE_MAX_FAMILY` survivors — ``m >= 2 / (k^2 * alpha/S)``. Above that S the threshold
    tightens while the draw count does not, so the Monte-Carlo error of the p stops being the
    declared fraction of the threshold it is compared against, and nothing said so: the block still
    printed the family size and the declared precision was documented on a constant nobody reads at
    that moment.

    **``str | None``, unlike :func:`note_holm_family` beside it, which is unconditional.** Returning
    ``None`` below the threshold puts the condition in ONE place. It was written when the two tracks
    each called this themselves and a ``str`` return would have duplicated the ``if`` — the shape
    that lets two tracks drift. :func:`decide_family` is the single call site now, so the shape
    earns its keep for a smaller reason: the caller appends what it gets and never asks whether to.

    ``n_resamples`` is the family's SMALLEST, not the constant: a caller may pass a custom count, and
    the coarsest member is what bounds the family's resolution. It is not a refusal — the gate still
    decides, this says how precisely.
    """
    if family_size <= GATE_MAX_FAMILY or family_size <= 0 or n_resamples < 1:
        return None
    threshold = alpha / family_size
    achieved = math.sqrt(2.0 / (n_resamples * threshold))
    needed = math.ceil(2.0 / (GATE_P_PRECISION**2 * threshold))
    return (
        f"resolution: this family of {family_size} is larger than the {GATE_MAX_FAMILY} the gate is "
        + f"sized for, so its strictest Holm threshold is alpha/{family_size} = {threshold:.5f} and "
        + f"the bootstrap resolves it to about {achieved:.4f} of itself at {n_resamples} draws, "
        + f"against the {GATE_P_PRECISION:.2f} this gate declares. Re-run at n_resamples="
        + f"{needed} to restore the declared precision, or gate fewer candidates per round. The "
        + "decision above stands; what is degraded is how finely it was measured."
    )


# What Stage C is asking, and the ONE declaration of the four answers on both tracks.
#
# It lives HERE rather than on either track's module because both need it and the layering forbids
# the two rank-2 modules from importing each other — so per-track would mean two copies of
# promotion-relevant arithmetic, which is the CE037/CE040 defect class exactly.
_CONFIRM_REVERSED = "reversed"
_CONFIRM_SHRANK = "shrank"
_CONFIRM_REPRODUCED = "reproduced"
_CONFIRM_UNDECIDED = "undecided"


def classify_confirm(train_effect: float | None, test_effect: float | None, test_mde: float | None) -> tuple[str, str]:
    """``(outcome, note)`` for a train -> test comparison. See :class:`ConfirmVerdict.outcome`.

    **Stage C confirms a WIN**, and the classifier says so rather than accepting any pair of signs.
    A train effect that is zero or negative is not something a confirm can reproduce, and the
    arithmetic silently produced actively misleading answers for those inputs — measured on the
    shipped version: ``train=-0.08, test=-0.10`` read REPRODUCED (a candidate that lost on train and
    lost harder), ``train=-0.08, test=-0.02`` read SHRANK (a LOSS reading like a diminished win), and
    ``train=0.0, test=+0.06`` read REPRODUCED when nothing was reproduced — an effect APPEARED where
    Stage B measured none. All four are UNDECIDED now, and with ``train_effect > 0`` guaranteed the
    remaining sign test is the plain ``test_effect < 0`` rather than a product.

    The four answers and their precedence:

    - **UNDECIDED** — either effect is absent, the train effect is not a win, or the margin is
      undefined. The margin is
      ``test_mde``, and it is undefined for ``None`` and for anything below
      :data:`FLOOR_RESOLUTION` — not just for exactly ``0.0``, which is the too-narrow test the
      execution track already had to widen once: a null split over a constant per-row difference
      returns something like 2.8e-17 rather than 0.0, so an ``== 0.0`` check goes silently inert on
      exactly the degenerate suites it exists for (measured on this repo's own winning fixture).
      ``execution_gate`` documents such a floor as "the floor was not checked", never "this suite can
      resolve anything", and with a margin of zero every non-identical test effect would classify
      SHRANK. Both pinned fixtures render ``Minimum detectable effect: 0.000``, so this is the common
      case rather than a corner.
    - **REVERSED** — the signs oppose. It outranks the two below because a reversal is a headline:
      the effect the round was built on pointed the other way on held-out rows.
    - **SHRANK** — same sign, and the test effect is below the train effect by MORE than the margin.
      A test effect of exactly ``0.0`` lands here too: "same sign" is undefined at zero, and calling
      it reproduced would report no effect as a reproduced one.
    - **REPRODUCED** — same sign, within the margin.

    A pure function of three floats, so it decides nothing about a run and can be read on its own.
    The note is always non-empty: a bare outcome word in a ledger read back weeks later is not
    enough to reconstruct which comparison produced it.
    """
    if train_effect is None or test_effect is None:
        missing = "train" if train_effect is None else "test"
        return _CONFIRM_UNDECIDED, (
            f"undecided: the {missing} effect is absent, so there is no delta to classify. That is "
            "not an effect of zero — no comparison was made."
        )
    if test_mde is None or test_mde < FLOOR_RESOLUTION:
        return _CONFIRM_UNDECIDED, (
            "undecided: the confirm split's minimum detectable effect is "
            + ("unavailable" if test_mde is None else f"{test_mde:.3g}")
            + ", so the SHRANK/REPRODUCED margin has no operand. An unpriced floor means the floor "
            + "could not be MEASURED — a null split reduces to zero, or to floating-point residue "
            + "below FLOOR_RESOLUTION, when every row's replicates agreed exactly — and is never a "
            + "green light. Raise --repeats on the confirm run and re-read."
        )
    if train_effect <= 0.0:
        return _CONFIRM_UNDECIDED, (
            f"undecided: the train effect was {train_effect:+.3f}, which is not a win — Stage C "
            "confirms that a measured improvement holds on held-out rows, and there is no "
            "improvement here to hold. Only the Stage B WINNER is confirmed; check which verdict was "
            "passed as the train one."
        )
    if not (math.isfinite(train_effect) and math.isfinite(test_effect) and math.isfinite(test_mde)):
        # Practically unreachable through the models' [0,1] bounds, but a NaN compares False against
        # every threshold below and would fall through to REPRODUCED — the most permissive rung.
        return _CONFIRM_UNDECIDED, (
            "undecided: one of the effects or the margin is not a finite number, so no comparison "
            "can be made. That is a wiring fault rather than a measurement — check the verdicts."
        )
    delta = test_effect - train_effect
    if test_effect == 0.0:
        # Its OWN note. Folding it into the SHRANK sentence below made a false arithmetic claim:
        # a small train win against a zero test effect announced that its shortfall "exceeds the
        # confirm split's own resolution", quoting a margin LARGER than the shortfall. The OUTCOME is
        # right — "same sign" is undefined
        # at zero, and reporting no effect as a reproduced one is the reading a promotion would be
        # built on — but the reason had to be the real one.
        return _CONFIRM_SHRANK, (
            f"SHRANK: the train effect was {train_effect:+.3f} and the confirm run measured exactly "
            "0.000 — no difference at all on the held-out rows. Classified as a shrinkage rather than "
            "a reproduction because 'the same sign' is undefined at zero."
        )
    if test_effect < 0.0:
        return _CONFIRM_REVERSED, (
            f"REVERSED: the train effect was {train_effect:+.3f} and the confirm run measured "
            f"{test_effect:+.3f} — opposite signs. The effect the round was built on does not hold "
            "on held-out rows; do not promote on it."
        )
    # No `abs()`: the guards above leave `train_effect > 0` and `test_effect > 0`, so both calls
    # would be dead and would imply a generality those guards removed.
    if test_effect < train_effect - test_mde:
        return _CONFIRM_SHRANK, (
            f"SHRANK: the train effect was {train_effect:+.3f}, the confirm run measured "
            f"{test_effect:+.3f} (delta {delta:+.3f}), and the shortfall exceeds the confirm split's "
            f"own resolution of {test_mde:.3f}. The train number was optimistic — some of it was fit "
            "to the rows it was measured on."
        )
    return _CONFIRM_REPRODUCED, (
        f"REPRODUCED: the train effect was {train_effect:+.3f} and the confirm run measured "
        f"{test_effect:+.3f} (delta {delta:+.3f}), within the confirm split's own resolution of "
        f"{test_mde:.3f}."
    )


def confirm_one_candidate(candidate_variant: object) -> None:
    """Raise unless exactly ONE candidate was named. Shared by both confirm gates.

    ``TypeError`` rather than ``ValueError``: a list where a variant id belongs is a type mismatch,
    and the plan that specified ``ValueError`` did not distinguish them. Recorded as a deviation.

    Confirming a shortlist would spend the held-out split on SELECTION, which is the failure the
    "never re-rolled" integrity rule exists to prevent — so a sequence raises rather than being
    iterated.
    """
    if not isinstance(candidate_variant, str):
        raise TypeError(
            f"candidate_variant must be ONE variant id, got {type(candidate_variant).__name__}. "
            + "Confirming a shortlist would spend the held-out split on selection, which is the "
            + "failure the never-re-rolled rule exists to prevent — gate one winner."
        )


def confirm_split_check(provenance: SplitProvenance, run_dirs: Sequence[Path]) -> tuple[str | None, str | None]:
    """``(refusal, note)`` for a confirm run's recorded ``--split``. The ONE rule, both tracks.

    Stage C confirms on the held-out split and nothing else, so anything other than ``test`` is a
    refusal — with ONE exception: a run predating the ``run.json`` provenance field is a NOTE, since
    that is an expected input rather than a wiring fault.

    **A refusal and a note are returned INDEPENDENTLY, and that is the whole reason this is shared.**
    The activation track pools several run directories per arm, and :attr:`SplitProvenance.value`
    collapses to ``UNRECORDED_SPLIT`` when *any* one of them is unreadable — so an ``if unrecorded /
    elif value != "test"`` chain drops the refusal entirely for three dirs recording ``train`` beside
    one unreadable ``run.json``, and the confirm then classifies over train rows with only a
    "provenance is missing from 1 of 4" note. Reading ``recorded`` directly is what closes that: a
    recorded non-``test`` split refuses whether or not a sibling dir also failed to answer.

    The execution twin takes ONE run dir and could not reach that state, which is exactly why the two
    tracks must not each own a copy of this: the safe one would keep working while the other drifted.
    """
    # `recorded` may carry `None` (no `--split` was passed), which is itself an off-split value here.
    off_split = sorted((split for split in provenance.recorded if split != "test"), key=lambda split: split or "")
    note = None
    if provenance.unrecorded:
        note = (
            f"row-selection provenance is missing from {provenance.unrecorded} of the "
            + f"{len(run_dirs)} confirm run directory/ies (they predate the run.json `row_selection` "
            + "field, or could not be read), so whether this run scored the HELD-OUT rows is not "
            + "recorded there. Confirm it by hand before promoting on this block."
        )
    if not off_split:
        return None, note
    named = ", ".join(repr(split) for split in off_split)
    remedy = (
        "That is Stage C re-running the TRAIN rows the candidate was fitted to — at full price, "
        + "and it reproduces by construction. Re-run the confirm with --split test."
        if "train" in off_split
        else "Stage C confirms on the held-out split and nothing else; a full-suite or "
        + "differently-named selection includes the rows the candidate was proposed against."
    )
    where = ", ".join(str(d) for d in run_dirs)
    return f"the confirm run recorded --split {named}, not 'test', under {where}. {remedy}", note


def confirm_train_note(promoted: bool | None) -> str | None:
    """Why a train verdict that did not PROMOTE is worth naming. Shared, so it cannot drift.

    A NOTE, not a refusal: a reader may legitimately want to confirm a candidate that separated and
    was then vetoed by a guardrail. But Stage C is DEFINED as confirming the Stage B winner, and
    :func:`classify_confirm` will not classify a train effect that is not a win — so saying which
    verdict was passed is what stops the resulting UNDECIDED reading as a tooling failure.
    """
    if promoted is True:
        return None
    return (
        f"the train verdict's `promoted` is {promoted!r}, not True — Stage C confirms the Stage B "
        + "WINNER. Confirming anything else spends the held-out split on a candidate no correction "
        + "promoted."
    )


def confirm_train_refusal(gate_refusal: str | None) -> str | None:
    """The refusal a train verdict that is not a result propagates into Stage C. Shared."""
    if gate_refusal is None:
        return None
    return "the TRAIN verdict is not a result, so there is nothing to reproduce: " + gate_refusal


def build_confirm_verdict(
    *,
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    train_effect: float | None,
    test_effect: float | None,
    test_mde: float | None,
    test_verdict: ActivationGateVerdict | ExecutionGateVerdict,
    confirm_refusal: str | None,
    notes: list[str],
) -> ConfirmVerdict:
    """Assemble the Stage C record — the ONE construction site, shared by both tracks.

    Here rather than per track for the same reason :func:`classify_confirm` is: the two rank-2
    modules may not import each other, so per-track would mean two copies of a promotion-relevant
    record's assembly, and a field added to :class:`ConfirmVerdict` would become two coordinated
    edits that can drift.

    **A refusal forces UNDECIDED and takes the note.** The classification is a statement about two
    measurements; if one of them is not a measurement there is nothing to state, and printing a
    confident outcome beneath a refusal is two contradictory claims in one block — the same rule
    both gates already apply to their negative-result notes.

    Literal keywords, never a splat: this is a model whose whole job is to say what a promotion
    rests on, so a mistyped key must raise rather than land at a default (CE041).
    """
    if confirm_refusal is not None:
        # The refusal lives on `confirm_refusal` and NOT in `notes` — the same rule `holm_promote`
        # states for `gate_refusal`: notes is the "everything you need to distrust the numbers"
        # channel, a refusal is a headline, and duplicating it printed the same sentence twice in one
        # rendered block.
        outcome, note = _CONFIRM_UNDECIDED, None
    else:
        outcome, note = classify_confirm(train_effect, test_effect, test_mde)
    delta = None if train_effect is None or test_effect is None else test_effect - train_effect
    return ConfirmVerdict(
        incumbent_variant=incumbent_variant,
        candidate_variant=candidate_variant,
        suite_id=suite_id,
        train_effect=train_effect,
        test_effect=test_effect,
        test_mde=test_mde,
        delta=delta,
        # `outcome` is a `Literal`, so the model validates the four spellings for us — which is why
        # `classify_confirm` may return a plain `str`.
        outcome=outcome,  # type: ignore[arg-type]
        test_verdict=test_verdict,
        confirm_refusal=confirm_refusal,
        # Pydantic COPIES this list, so the note has to be in it before construction — the same
        # ordering rule both gates' `notes` follow.
        # `None` on a refused block, where the reason is the headline rather than a note.
        notes=[*notes, note] if note is not None else list(notes),
    )


class _HolmFamily(NamedTuple):
    """Who was in the family, and whom Holm rejected — by ORIGINAL index in both cases.

    ``members`` is ``[(original_index, p)]`` and ``rejected_at`` a set of ORIGINAL indices, not
    positions in the filtered vector. That mapping is the whole content of this helper: the family
    excludes every ``p_value is None`` verdict, so a naive ``enumerate`` over the filtered list
    shifts every index after the first exclusion and Holm's answer lands on the wrong candidate —
    silently, since both are plausible-looking ints.
    """

    members: list[tuple[int, float]]
    rejected_at: set[int]


def holm_family(verdicts: Sequence[GateVerdictBase], alpha: float) -> _HolmFamily:
    """The one place :func:`~coder_eval.reports_stats.holm_rejections` is called.

    Both wrappers spelled these three lines identically, 700 lines apart. Holm corrects a FAMILY,
    so a call site that sees one candidate at a time degenerates to an uncorrected ``p <= alpha``
    while still looking like a correction — and ``TestHolmRejectionsIsConfined`` asserts the SET of
    enclosing function names, so one declaration is stricter to audit than two.

    Membership is ``p_value is not None`` and nothing else. A refused verdict that still measured a
    p stays IN: it was tested however degenerate its sample turned out to be, and dropping it would
    shrink ``m`` and loosen ``alpha/m`` for its siblings — the uncorrected-``p <= alpha``
    degeneration approached from the other side.

    Typed on :class:`~coder_eval.models.GateVerdictBase`, never a ``Protocol``. It used to take the
    two-class union with the note that a protocol would be a third declaration of "has a
    ``p_value``" — and the base IS that one declaration now, so the union became the redundant
    spelling. The retype is not cosmetic: :func:`decide_family` is generic over the base, and
    passing its ``Sequence[V]`` into a two-class union parameter does not type.
    """
    members = [(i, v.p_value) for i, v in enumerate(verdicts) if v.p_value is not None]
    rejections = holm_rejections([p for _i, p in members], alpha)
    return _HolmFamily(members, {i for (i, _p), reject in zip(members, rejections, strict=True) if reject})


def resamples_for_family(verdicts: Sequence[GateVerdictBase], family: list[tuple[int, float]]) -> int:
    """The COARSEST draw count among the family's members — what bounds the family's resolution.

    Read off the verdicts rather than from :data:`GATE_RESAMPLES`, since a caller may pass a custom
    count, and over family MEMBERS only: a verdict with no p was not tested at any resolution.

    Shared because the line was byte-identical in both Holm wrappers, which is the duplication
    :func:`note_resolution_degraded` was put at this rank to avoid. Both wrappers have since
    collapsed into :func:`decide_family`, its one remaining caller — so what this now buys is a
    name for the rule ("the coarsest, over members only") rather than a second copy avoided.
    """
    return min((verdicts[i].n_resamples for i, _p in family), default=GATE_RESAMPLES)


def note_check_failed(check_name: str) -> str:
    """Which non-primary check vetoed a promotion the statistic had otherwise won.

    A fifth shared note beside the four above, and for the same reason they exist: both tracks now
    veto on their check lists, so both emit this sentence, and a wording fix applied to one copy
    would leave the two describing the same decision differently in a ledger read weeks later.

    It says ``separated`` unqualified rather than naming a verdict class. Both verdicts carry that
    property now, the rendered block's header already says which gate produced it, and spelling the
    class was the only thing that made the two copies differ.

    **Only emitted where it is TRUE** — the caller guards on the statistic having separated AND
    Holm having rejected. On a candidate that simply lost, both clauses here are false: nothing was
    forced (``promoted`` was already False) and the headline is NOT PROMOTED, not BLOCKED. Printing
    it there reintroduces the exact misdirection the BLOCKED rung's ``holm_rejected`` conjunct
    exists to remove — sending the reader to fix cost when the real problem is power.
    """
    return (
        f"{check_name} FAILED — this forces `promoted = False` even where the statistic separated. "
        + "It is named here so the block says WHICH check vetoed the promotion and why; the "
        + "rendered headline reports it as BLOCKED BY A GUARDRAIL, which `separated` is what keeps "
        + "distinguishable from an ordinary loss."
    )


class FamilyFacts(NamedTuple):
    """What :func:`decide_family` knows about the family, handed to a per-track hook.

    FAMILY facts only. ``rejected``, ``family_size`` and ``alpha`` are things no single verdict can
    see, which is exactly why the loop that sees the whole family computes them once. Track-local
    quantities — the activation track's rank threshold, the sibling binding its note ladder needs —
    are NOT here: they are derived inside the hook that wants them, from ``family_p_values``.

    **Carries no ``family_resamples``, deliberately.** The family's coarsest draw count is what
    bounds :func:`note_resolution_degraded`, and ``decide_family`` emits that note itself, so no
    hook has ever read the number. A field written on every iteration and read by nobody is the
    same speculative field this tuple already refuses a ``threshold`` for: it advertises a fact to
    hook authors that they must not use, and the ladder that looks like it wants one actually reads
    ``verdict.n_resamples``, a different number.
    """

    rejected: bool
    family_size: int
    alpha: float
    family_p_values: list[float]


class TrackDecision(NamedTuple):
    """What a per-track hook answers with: this verdict's refusal, and its notes in rendered order.

    ``refusal`` is RETURNED rather than read off the verdict because the two tracks disagree about
    where it comes from and agree about what it means. The activation track COMPUTES it from the
    family's rank-dependent threshold, which nothing outside the family can do; the execution track
    READS the one ``execution_gate`` already set. Returning it from both makes the ``promoted``
    conjunction one expression instead of two spellings of the same conjunction.
    """

    refusal: str | None
    notes: list[str]


def decide_family[V: GateVerdictBase](
    verdicts: Sequence[V], alpha: float, *, decide: Callable[[V, FamilyFacts], TrackDecision]
) -> list[V]:
    """Apply the Holm correction to a whole family and record the decision on each verdict.

    The ONE promotion loop for both tracks. It was written twice, 700 lines apart, and the two
    copies had already drifted in spelling: the refusal conjunct read ``refusal is None`` on one
    side and ``not refused`` on the other, and the two trailing notes were appended from different
    places.

    What it owns, and what a hook may therefore not re-do:

    - the :func:`holm_family` / :func:`resamples_for_family` calls;
    - the ``p_value is None`` branch — outside the family, ``promoted=False`` and
      ``holm_rejected=False`` (never ``None``, which would read as "Holm has not run" on a verdict
      it has), with :data:`NOTE_OUTSIDE_FAMILY` added only where there is no refusal to contradict;
    - the ONE ``promoted`` conjunction: Holm rejected AND ``separated`` AND no refusal AND no
      failed veto. ``failed_vetoes`` is the single declaration of which lists veto, so a failed
      guardrail FORCES False and the field alone is safe to ship on;
    - the two TRAILING notes, :func:`note_holm_family` and :func:`note_resolution_degraded`, for
      both tracks. A hook that appends either prints it twice;
    - both ``copy_with`` calls.

    It owns **no note text**. Every sentence about one candidate is the hook's, because which of
    the two statistical conjuncts failed — and what the remedy is — differs between a bootstrap
    over labels and an analytic paired *t*.

    The hook runs on the MEASURED branch only: the activation track's refusal needs the family's
    rank-dependent threshold, so calling it on a ``p_value is None`` verdict would hand a
    discreteness refusal to a verdict that cannot have one.

    Generic over ``V`` rather than typed to the union, so ``copy_with`` returns the caller's own
    verdict class and a track keeps its concrete type end to end.

    **One measurable difference from the two loops it replaces, stated because "no-op" was the
    claim.** The execution wrapper omitted ``gate_refusal`` from its ``copy_with``; this writes it on
    every measured path, from the hook, which returns the verdict's own value. The VALUE is
    therefore unchanged — verified over a 10,982-state differential against both old loops — but the
    key now enters ``__pydantic_fields_set__``, so ``model_dump(exclude_unset=True)`` includes it on
    an execution verdict built without it. Nothing reads a gate verdict that way today; the full
    ``model_dump()`` and every pinned fixture are byte-identical.
    """
    family, rejected_at = holm_family(verdicts, alpha)
    family_resamples = resamples_for_family(verdicts, family)
    family_p_values = [p for _i, p in family]

    decided: list[V] = []
    for i, verdict in enumerate(verdicts):
        notes = list(verdict.notes)
        if verdict.p_value is None:
            # Reachable on BOTH tracks with a refusal already set — the activation track's
            # cross-split preflight and every execution-track "there was no comparison to make"
            # cause land here. Unguarded, a refused block prints an ordinary negative-result note
            # directly under a refusal headline.
            if verdict.gate_refusal is None:
                notes.append(NOTE_OUTSIDE_FAMILY)
            decided.append(copy_with(verdict, promoted=False, holm_rejected=False, holm_alpha=alpha, notes=notes))
            continue

        rejected = i in rejected_at
        refusal, track_notes = decide(
            verdict,
            FamilyFacts(
                rejected=rejected,
                family_size=len(family),
                alpha=alpha,
                family_p_values=family_p_values,
            ),
        )
        # `refusal is None` is LOAD-BEARING on both tracks, not belt-and-braces. On activation,
        # `p_floor` bounds the p's EXPECTATION, so a realized p dips below it on roughly half of
        # all seeds; on execution, a zero-variance verdict has p = 0.0000 and a zero-width
        # interval, so `separated` holds on it too. Without this conjunct an undecidable
        # comparison promotes AND carries a refusal — two contradictory claims in one block.
        promoted = rejected and verdict.separated and refusal is None and not verdict.failed_vetoes
        notes += track_notes
        notes.append(note_holm_family(len(family), alpha))
        # Not a negative-result claim, so it sits outside every refusal guard: it is a statement
        # about the draw count, which stays true whatever the refusal says.
        if (degraded := note_resolution_degraded(len(family), family_resamples, alpha)) is not None:
            notes.append(degraded)
        # The refusal lives on `gate_refusal` and NOT in `notes`: notes is the "everything the
        # reader needs to distrust the numbers" channel, a refusal is a headline, and duplicating
        # it would print the same sentence twice in one block.
        decided.append(
            copy_with(
                verdict,
                promoted=promoted,
                holm_rejected=rejected,
                holm_alpha=alpha,
                gate_refusal=refusal,
                notes=notes,
            )
        )
    return decided
