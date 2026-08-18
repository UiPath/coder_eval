"""The decision primitives BOTH optimize-skill tracks share.

Rank 1 of the optimize family: it imports from :mod:`coder_eval.optimize_load` and from nothing
else in the family, and both track modules import from it. What lives here is what is neither
track's alone — the gate-wide constants, the notes both Holm wrappers emit verbatim, the
noise-floor refusal channel and the cluster half both floors share, the cost/latency guardrails
both gates run, and :func:`_holm_family`, the ONE
:func:`~coder_eval.reports_stats.holm_rejections` call site.

The two gates themselves live one rank up, in :mod:`coder_eval.optimize_activation` and
:mod:`coder_eval.optimize_execution`. The split is BY TRACK because the activation/execution pair
is this feature's dominant organising principle — almost every concept in it is a two-track pair —
and because the module it was carved out of had grown to 3,500 lines with an E-grade function.

**A library, not a CLI**, like every module in the family: no typer, no rich, no reach into the
CLI package. The skill drives these functions from a short inline ``python`` snippet.

**F1 is never recomputed here.** Every metric comes from
:func:`coder_eval.criteria._classification_aggregate.classification_metrics`, the criterion layer's
own routine (CE037), so the gate cannot disagree with the numbers the run reported.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from typing import NamedTuple

from coder_eval.criteria._classification_aggregate import classification_metrics
from coder_eval.models import (
    ActivationGateVerdict,
    EvaluationResult,
    ExecutionGateVerdict,
    GuardrailCheck,
    NoiseFloor,
    OptimizeMeasurements,
    copy_with,
)
from coder_eval.optimize_load import _balance_pair, _median, _row_cost_levels, _row_costs
from coder_eval.optimize_store import UNRESOLVED_MODEL, lookup_noise_floor
from coder_eval.reports_stats import (
    DEFAULT_ALPHA,
    cluster_bootstrap_diff_ci,
    holm_rejections,
    mean,
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


def _metric(pairs: list[tuple[str, str]], name: str) -> float:
    """A classification metric over ``pairs``, through the criterion layer's own routine.

    Empty pairs and an absent metric name both read 0.0, and both conventions are declared in
    ``_classification_aggregate`` rather than here — restating either at this call site would be
    the second declaration CE037 exists to prevent.
    """
    cm = classification_metrics(pairs)
    return cm.metric(name) if cm is not None else 0.0


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

    for name, extract in (("cost (USD/row)", _row_costs), ("latency (seconds/row)", _row_durations)):
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
        comparable = [_balance_pair(inc, cand) for inc, cand in paired]
        # A different rule, applied AFTER the trim: drop rows one arm did not measure at all.
        comparable = [(inc, cand) for inc, cand in comparable if inc and cand]

        incumbent_clusters = [inc for inc, _c in comparable]
        candidate_clusters = [cand for _i, cand in comparable]
        incumbent_median = _median(_row_cost_levels(incumbent_clusters))
        candidate_median = _median(_row_cost_levels(candidate_clusters))
        # The floor scales by the incumbent's MEAN, because the interval it is compared against is
        # an interval on the difference of means. Scaling a mean-difference by a median is a unit
        # mismatch on any skewed distribution — and per-row cost is strongly right-skewed, so a
        # uniform 10% increase measured as FAIL against a 25% floor. The medians stay as the
        # reported level, which is the robust thing to READ; the mean is what is being tested.
        incumbent_mean = mean(_row_cost_levels(incumbent_clusters)) if incumbent_clusters else 0.0

        if incumbent_median is None or candidate_median is None:
            checks.append(
                GuardrailCheck(
                    name=name,
                    incumbent=incumbent_median,
                    candidate=candidate_median,
                    relative_change=None,
                    tolerance=materiality,
                    passed=True,
                    note=f"no {name.split(' ')[0]} recorded on at least one arm — guardrail not evaluated",
                )
            )
            continue

        notes: list[str] = []
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


def _no_floor(reason: str, *, reasons: list[str] | None = None) -> None:
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


def _floor_from_clusters[T](
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
        return _no_floor(
            f"the bootstrap declined on {len(clusters_a)} cluster(s) for {probe.suite_id!r} — it needs 2",
            reasons=reasons,
        )
    _diff, ci_low, ci_high, _p = bootstrap
    return copy_with(probe, mde=(ci_high - ci_low) / 2.0)


# The four notes both Holm wrappers emit verbatim. They were byte-identical copies in two functions
# 600 lines apart; a wording fix applied to one of them would have left the two tracks describing
# the same decision differently in a ledger read back weeks later.
#
# Deliberately NOT collapsed with them: the zero-row and below-MDE notes. Those diverged on purpose
# — the execution track's zero-row case became a `gate_refusal` with different text, and its MDE
# note names `weighted_score` because that is the statistic its gate reads. Two tracks saying
# different things there is the finding, not drift.
_NOTE_OUTSIDE_FAMILY = "not promoted: the sample could not support a p-value, so this arm is outside the family."
_NOTE_CI_CONTAINS_ZERO = (
    "not promoted: the Holm-corrected test rejects but the confidence interval still "
    "contains zero, so the effect is not separated at the reported interval width."
)


def _note_ordinary_negative(p_value: float, family_size: int, alpha: float) -> str:
    return (
        f"not promoted: p = {p_value:.4f} did not clear the Holm threshold for its rank in a "
        f"family of {family_size} (alpha={alpha}). This is the ordinary negative result — the "
        "interval and the effect size above are what to report."
    )


def _note_holm_family(family_size: int, alpha: float) -> str:
    return f"Holm applied across a family of {family_size} at alpha={alpha}."


def _note_resolution_degraded(family_size: int, n_resamples: int, alpha: float) -> str | None:
    """What resolution the gate ACTUALLY achieved on a family larger than it is sized for.

    :data:`GATE_RESAMPLES` is derived from :data:`GATE_P_PRECISION` at the strictest Holm threshold
    for :data:`GATE_MAX_FAMILY` survivors — ``m >= 2 / (k^2 * alpha/S)``. Above that S the threshold
    tightens while the draw count does not, so the Monte-Carlo error of the p stops being the
    declared fraction of the threshold it is compared against, and nothing said so: the block still
    printed the family size and the declared precision was documented on a constant nobody reads at
    that moment.

    **``str | None``, unlike :func:`_note_holm_family` beside it, which is unconditional.** Returning
    ``None`` below the threshold puts the condition in ONE place. The alternative — a ``str`` return
    plus an ``if`` duplicated at the two call sites — is exactly the shape that lets the two tracks
    drift, which is the whole reason these notes are shared.

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


def _holm_family(verdicts: Sequence[ActivationGateVerdict | ExecutionGateVerdict], alpha: float) -> _HolmFamily:
    """The one place :func:`~coder_eval.reports_stats.holm_rejections` is called.

    Both wrappers spelled these three lines identically, 700 lines apart. Holm corrects a FAMILY,
    so a call site that sees one candidate at a time degenerates to an uncorrected ``p <= alpha``
    while still looking like a correction — and ``TestHolmRejectionsIsConfined`` asserts the SET of
    enclosing function names, so one declaration is stricter to audit than two.

    Membership is ``p_value is not None`` and nothing else. A refused verdict that still measured a
    p stays IN: it was tested however degenerate its sample turned out to be, and dropping it would
    shrink ``m`` and loosen ``alpha/m`` for its siblings — the uncorrected-``p <= alpha``
    degeneration approached from the other side.

    Structurally typed over the two verdict models rather than behind a ``Protocol``: both expose
    ``p_value``, the union is two concrete classes this module already imports, and a protocol here
    would be a third declaration of "has a p_value" for no reader's benefit.
    """
    members = [(i, v.p_value) for i, v in enumerate(verdicts) if v.p_value is not None]
    rejections = holm_rejections([p for _i, p in members], alpha)
    return _HolmFamily(members, {i for (i, _p), reject in zip(members, rejections, strict=True) if reject})


def _note_check_failed(check_name: str) -> str:
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
